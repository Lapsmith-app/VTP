"""SPEC.md §15 -- the OBD role: the one whose device transmits.

Everything here runs on the boundary the section draws: the poll set controls
the transmitter, subscriptions control the receiver, and the polling flag is
what makes the transmitter observable from the stream. The probe record's
layout rules are the corpus's job; what only a live device can answer -- does
the probe describe the car, does the poll loop stop when told, does the flag
tell the truth -- is this file's.
"""
import asyncio
import struct
import time

from .. import refdec
from ..session import ControlTimeout
from . import Fail, Observe, Skip, check

POLLING = 1 << refdec.bit("can_flags", "polling")
RESPONDED = 1 << refdec.bit("obd_validity", "responded")


def _identity(frame):
    """A decoded can_record's identifier over bits 0-29 (SPEC.md §15.5's
    comparison): the decoder splits the format bit out, the probe's ids
    carry it in bit 29, and comparing across that split silently never
    matches on a 29-bit-addressed car."""
    return frame["id"] | ((1 << 29) if frame["extended"] else 0)


def _control(s):
    if s.control is None:
        raise Skip("this device does not declare the control capability")
    return s.control


def _probe(s):
    probe = s.state.get("obd_probe")
    if probe is None:
        raise Skip("OBD_INFO returned nothing to check")
    return probe


def _probe_timeout(s):
    """SPEC.md §15.2 -- a probe is up to three requests, each spaced by at
    least the greater of the 50 ms collection window and the declared
    floor, which is a u16: a device with a 2000 ms floor legitimately takes
    ~6 s. The fixed control timeout would report that conforming probe
    unanswered."""
    floor = s.info["obd_min_interval_ms"] if s.info else 0
    return max(3.0, 3 * max(floor, 50) / 1000 + 2.0)


def _union_bit(probe, pid):
    window, bit = divmod(pid - 0x01, 32)
    field = ("supported_01_20", "supported_21_40", "supported_41_60")[window]
    return bool(probe["probe"][field] & (1 << bit))


@check(id="obd.poll_before_probe", section="15.4", phase="control",
       severity="MUST", requires=("obd",), adversarial=True,
       title="Nothing is pollable before a probe")
async def obd_poll_before_probe(s):
    c = _control(s)
    if s.info is None:
        raise Skip("Info did not decode")
    # Registration order puts this first in the module, and nothing earlier
    # in the control phase sends OBD_INFO, so no probe has answered on this
    # connection yet. The request is well-formed in every other respect --
    # legal interval, one PID inside 0x01-0x60, within the slot count -- so
    # the only rule left to refuse it is §15.4's probe gate.
    interval = max(s.info["obd_min_interval_ms"], 1)
    r = await c.request(refdec.OPCODE["OBD_POLL_SET"],
                        struct.pack("<HB", interval, 1) + bytes([0x0C]))
    if r.status != refdec.STATUS_VALUE["bad_params"]:
        raise Fail(
            f"a well-formed non-empty poll set before any OBD_INFO was "
            f"answered {r.status_name}; §15.4 -- with no probe result "
            f"nothing is pollable, and a device that transmits for an "
            f"unverified PID has skipped the verify step the role is built "
            f"on")


@check(id="obd.probe", section="15.2", phase="control", severity="MUST",
       requires=("obd",),
       title="OBD_INFO is answered with an obd_probe record")
async def obd_probe(s):
    c = _control(s)
    try:
        response = await c.request(refdec.OPCODE["OBD_INFO"],
                                   timeout=_probe_timeout(s))
    except ControlTimeout:
        raise Fail("OBD_INFO went unanswered. §9 requires a device to respond "
                   "to every request it applies -- and this one is the probe "
                   "a client must complete before anything is pollable") from None
    if not response.ok:
        raise Fail(
            f"OBD_INFO was answered {response.status_name} on a device that "
            f"declares capability bit {refdec.CAPABILITIES['obd']} (`obd`). "
            f"§9 makes the bit the opcode's owner: declaring it and refusing "
            f"the opcode leaves a client no way to tell which of the two "
            f"statements is true",
            response=response.raw.hex())
    try:
        s.state["obd_probe"] = response.detail_as("obd_info")
    except refdec.Reject as exc:
        raise Fail(f"the obd_probe detail did not decode: {exc}",
                   detail=response.detail.hex()) from None
    s.state["obd_probe_raw"] = response.detail
    probe = s.state["obd_probe"]
    if probe["probe"]["validity"] & RESPONDED:
        raise Observe(
            f"request id 0x{probe['probe']['request_id']:X}, "
            f"{probe['probe']['count']} ECU(s): "
            + ", ".join(f"0x{e['id']:X}" for e in probe["ecus"]))
    raise Observe("nothing answered the probe -- a gatewayed port, or no "
                  "J1979 stack on this bus")


@check(id="obd.count_agrees", section="15.2", phase="control", severity="MUST",
       requires=("obd",),
       title="The probe's count agrees with `responded`, and stays within 8")
async def obd_count_agrees(s):
    probe = _probe(s)
    responded = bool(probe["probe"]["validity"] & RESPONDED)
    count = probe["probe"]["count"]
    if responded and count == 0:
        raise Fail(
            "`responded` is set and count is 0: the probe says something "
            "answered and lists nothing that did (§15.2)",
            detail=s.state["obd_probe_raw"].hex())
    if count and not responded:
        raise Fail(
            f"count is {count} with `responded` clear: an ECU is listed on a "
            f"probe that says nothing answered (§15.2)",
            detail=s.state["obd_probe_raw"].hex())
    if count > 8:
        raise Fail(
            f"count is {count}; ISO 15765-4 caps the responders to a "
            f"functional request at eight (§15.2)",
            detail=s.state["obd_probe_raw"].hex())


@check(id="obd.entries_ascending", section="15.2", phase="control",
       severity="MUST", requires=("obd",),
       title="The probe's ECU entries are strictly ascending")
async def obd_entries_ascending(s):
    probe = _probe(s)
    ids = [e["id"] for e in probe["ecus"]]
    if any(b <= a for a, b in zip(ids, ids[1:])):
        raise Fail(
            f"entry ids are {[hex(i) for i in ids]}; §15.2 makes the list "
            f"strictly ascending, so one ECU cannot appear to be two and two "
            f"conforming devices probing one car produce identical bytes",
            detail=s.state["obd_probe_raw"].hex())


@check(id="obd.absent_fields_zero", section="15.2", phase="control",
       severity="MUST", requires=("obd",),
       title="Probe fields behind a cleared `responded` bit are zero")
async def obd_absent_fields_zero(s):
    _probe(s)
    stale = refdec.absent_but_nonzero(
        "obd_probe", s.state["obd_probe_raw"][:refdec.size("obd_probe")],
        "obd_validity")
    if stale:
        raise Fail(
            f"{', '.join(f'{n}={v}' for n, v in stale)} "
            f"{'is' if len(stale) == 1 else 'are'} non-zero with `responded` "
            f"clear -- the previous car's probe, left in the bytes. §15.2 "
            f"holds this record to §1.1's rule, and a client reading a "
            f"request identifier out of a silent probe polls a car that "
            f"never consented",
            detail=s.state["obd_probe_raw"].hex())


@check(id="obd.reserved", section="15.2", phase="control", severity="MUST",
       requires=("obd",),
       title="Reserved obd_validity bits and reserved_18 are zero")
async def obd_reserved(s):
    probe = _probe(s)
    reserved = refdec.reserved_mask("obd_validity", 8)
    raw = s.state["obd_probe_raw"]
    (reserved_18,) = struct.unpack_from(
        "<H", raw, refdec.offset("obd_probe", "reserved_18"))
    problems = []
    if probe["probe"]["validity"] & reserved:
        problems.append(f"obd_probe.validity has reserved bits set "
                        f"(0x{probe['probe']['validity']:02x})")
    if reserved_18:
        problems.append(f"obd_probe.reserved_18 is {reserved_18}; Appendix A "
                        f"holds bytes 18-19 for probe metadata")
    if problems:
        raise Fail("; ".join(problems) + ". A 1.0 device that writes them has "
                   "published a claim this version has not defined",
                   detail=raw.hex())


@check(id="obd.capacities", section="15", phase="control", severity="MUST",
       requires=("obd",),
       title="A device declaring `obd` declares both of its capacities")
async def obd_capacities(s):
    if s.info is None:
        raise Skip("Info did not decode")
    zero = [name for name in ("obd_poll_slots", "obd_min_interval_ms")
            if not s.info[name]]
    if zero:
        raise Fail(
            f"bit 10 is set and {', '.join(zero)} "
            f"{'is' if len(zero) == 1 else 'are'} zero. §15 -- a poll set "
            f"nothing fits in, or a floor of zero milliseconds, describes a "
            f"role no conforming exchange can use, exactly as §9.7 forbids "
            f"declaring `power` and answering with nothing valid")
    raise Observe(
        f"up to {s.info['obd_poll_slots']} PIDs per set, no interval below "
        f"{s.info['obd_min_interval_ms']} ms")


@check(id="obd.poll_refusals", section="15.4", phase="control", severity="MUST",
       requires=("obd",), adversarial=True,
       title="OBD_POLL_SET refuses what §15.4 says it must")
async def obd_poll_refusals(s):
    c = _control(s)
    probe = _probe(s)
    if s.info is None:
        raise Skip("Info did not decode")
    floor = s.info["obd_min_interval_ms"]
    slots = s.info["obd_poll_slots"]
    interval = max(floor, 1)
    problems = []

    async def poll_set(interval_ms, pids):
        return await c.request(
            refdec.OPCODE["OBD_POLL_SET"],
            struct.pack("<HB", interval_ms, len(pids)) + bytes(pids))

    # An unverified PID: one whose union bit is clear, which always exists --
    # the probe covers 96 PIDs and no petrol car on earth implements all of
    # them. With `responded` clear, every PID qualifies (§15.4: with no probe
    # result, nothing is pollable).
    unsupported = next((pid for pid in range(0x01, 0x61)
                        if not probe["probe"]["validity"] & RESPONDED
                        or not _union_bit(probe, pid)), None)
    if unsupported is not None:
        r = await poll_set(interval, [unsupported])
        if r.status != refdec.STATUS_VALUE["bad_params"]:
            problems.append(
                f"PID 0x{unsupported:02X}, which the probe's union does not "
                f"claim, was answered {r.status_name} instead of bad_params")
    r = await poll_set(interval, [0x7F])
    if r.status != refdec.STATUS_VALUE["bad_params"]:
        problems.append(f"PID 0x7F is outside 0x01-0x60 and was answered "
                        f"{r.status_name} instead of bad_params")
    if floor > 1:
        supported = next((pid for pid in range(0x01, 0x61)
                          if _union_bit(probe, pid)), None)
        if supported is not None:
            r = await poll_set(floor - 1, [supported])
            if r.status != refdec.STATUS_VALUE["bad_params"]:
                problems.append(
                    f"an interval of {floor - 1} ms, below the declared floor "
                    f"of {floor}, was answered {r.status_name}")
    # interval_ms 0 with a non-empty set is its own MUST: zero is not "no
    # limit" here (§15.4) -- the device would be GENERATING unbounded
    # traffic -- and it is refused whatever PID rides beside it, so this
    # cannot be excused by the pid checks that follow.
    r = await poll_set(0, [0x0C])
    if r.status != refdec.STATUS_VALUE["bad_params"]:
        problems.append(
            f"a non-empty set with interval_ms 0 was answered "
            f"{r.status_name}; §15.4 refuses unbounded generation outright")
    if slots < 0xFF:
        r = await poll_set(interval, [0x01] * (slots + 1))
        if r.status != refdec.STATUS_VALUE["table_full"]:
            problems.append(
                f"{slots + 1} PIDs against a declared capacity of {slots} "
                f"was answered {r.status_name} instead of table_full")
    r = await poll_set(interval, [])
    if r.status != refdec.STATUS_VALUE["bad_params"]:
        problems.append(
            f"an empty set with interval_ms {interval} was answered "
            f"{r.status_name}; §15.4 requires the stop to carry interval 0")
    # ...and the stop itself is always available, whatever came before.
    r = await poll_set(0, [])
    if not r.ok:
        problems.append(f"the empty poll set MUST be accepted and was "
                        f"answered {r.status_name}")
    if problems:
        raise Fail("; ".join(problems))


@check(id="obd.poll_and_flag", section="15.6", phase="control", severity="MUST",
       requires=("obd",), adversarial=True,
       title="Polling delivers with nothing subscribed, is flagged, and stops")
async def obd_poll_and_flag(s):
    c = _control(s)
    probe = _probe(s)
    if s.info is None:
        raise Skip("Info did not decode")
    if not probe["probe"]["validity"] & RESPONDED:
        raise Skip("nothing answered the probe, so there is nothing to poll")
    supported = next((pid for pid in range(0x01, 0x61)
                      if _union_bit(probe, pid)), None)
    if supported is None:
        raise Skip("the probe's union claims no PID at all")
    ecu_ids = {e["id"] for e in probe["ecus"]}
    interval = max(s.info["obd_min_interval_ms"], 25)
    # §15.4 lets the set take effect within one interval, so a fixed window
    # fails a conforming device whose declared floor is large: every wait
    # here scales with the interval actually in use.
    settle = max(3 * interval / 1000, 0.35)
    log = s.streams["can"]

    def batches_since(t):
        out = []
        for n in log.since(t):
            try:
                out.append((n.t_host, refdec.decode("can_batch", n.payload)))
            except refdec.Reject:
                continue        # the decode checks own that finding
        return out

    # Deliberately NOTHING is subscribed: §15.5's fallback is the delivery
    # path, and an accepted poll set is the whole of what a client does to
    # receive the answers. A known state first -- CAN_RESET clears any table
    # entries earlier checks left, and leaves the probe result standing
    # (§15.7).
    r = await c.request(refdec.OPCODE["CAN_RESET"])
    if not r.ok:
        raise Fail(f"CAN_RESET was answered {r.status_name}")
    try:
        r = await c.request(
            refdec.OPCODE["OBD_POLL_SET"],
            struct.pack("<HB", interval, 1) + bytes([supported]))
        if not r.ok:
            raise Fail(f"a probed, supported PID (0x{supported:02X}) at "
                       f"{interval} ms was answered {r.status_name} -- and "
                       f"§15.7 leaves the probe result standing across the "
                       f"CAN_RESET this check just issued")
        t_started = time.monotonic()
        await asyncio.sleep(settle)

        running = batches_since(t_started)
        frames = [rec for _, b in running for rec in b["records"]]
        heard = [f for f in frames if _identity(f) in ecu_ids]
        if not heard:
            # §15.4 -- an unanswered request is abandoned and the gap IS the
            # truth, so silence alone must not fail a conforming device: the
            # car may have gone quiet since the probe. A fresh probe tells
            # the two apart -- §15.2 makes each one a fresh measurement (and
            # clears the poll set, which the cleanup below covers anyway).
            try:
                again = await c.request(refdec.OPCODE["OBD_INFO"],
                                        timeout=_probe_timeout(s))
            except ControlTimeout:
                raise Fail("OBD_INFO went unanswered while diagnosing a "
                           "silent poll; §9 requires a response to every "
                           "request") from None
            silent_car = True
            if again.ok:
                try:
                    fresh = refdec.decode("obd_info", again.detail)
                except refdec.Reject as exc:
                    raise Fail(f"the re-probe's detail did not decode: "
                               f"{exc}", detail=again.detail.hex()) from None
                silent_car = not fresh["probe"]["validity"] & RESPONDED
            if silent_car:
                raise Skip(
                    "the car stopped answering between the probe and the "
                    "poll -- nothing was delivered because nothing was on "
                    "the bus, which §15.4 makes the honest outcome")
            raise Fail(
                f"the car still answers a probe, yet no poll response was "
                f"delivered within {settle:.2f} s of an accepted poll set "
                f"(PID 0x{supported:02X}, {interval} ms) with nothing "
                f"subscribed; §15.5 -- the fallback delivers on the probe's "
                f"reported identifiers, so this device is either not "
                f"transmitting or discarding the answers it asked for")
        strays = [f for f in frames if _identity(f) not in ecu_ids]
        if strays:
            raise Fail(
                f"{len(strays)} frame(s) arrived on identifiers outside the "
                f"probe's reported list, with nothing subscribed; §15.5's "
                f"fallback delivers on the reported response identifiers "
                f"and nothing else",
                ids=sorted({hex(_identity(f)) for f in strays}))
        request_id = probe["probe"]["request_id"]
        if any(_identity(f) == request_id for f in frames):
            raise Fail(
                "the device's own request frames appear in the stream; "
                "§15.5 -- the CAN stream carries what the device hears, "
                "never what it says")
        # §15.6's rising edge, with the same grace the falling edge gets: a
        # batch flushed before the ok can be delivered after it over a real
        # link, and the loopback's synchrony must not hide that race.
        t_flag = t_started + max(2 * interval / 1000, 0.15)
        unflagged = [b for t, b in running
                     if t >= t_flag and not b["header"]["flags"] & POLLING]
        if unflagged:
            raise Fail(
                f"{len(unflagged)} batch(es) flushed while the poll set was "
                f"non-empty carry the polling flag clear; §15.6 puts the "
                f"flag on every one, because it is how anyone watching the "
                f"stream tells a transmitting dongle from a sniffer")

        # The stop, and its falling edge on the wire.
        r = await c.request(refdec.OPCODE["OBD_POLL_SET"],
                            struct.pack("<HB", 0, 0))
        if not r.ok:
            raise Fail(f"the empty poll set was answered {r.status_name}")
        # One interval of grace: a response already on the bus when the stop
        # arrived -- and the stop's own flush of the pending batch (§15.7) --
        # may still be delivered and is not a violation.
        await asyncio.sleep(max(interval / 1000 * 2, 0.15))
        t_settled = time.monotonic()
        await asyncio.sleep(max(3 * interval / 1000, 0.3))
        after = batches_since(t_settled)
        late = [rec for _, b in after for rec in b["records"]
                if _identity(rec) in ecu_ids]
        if late:
            raise Fail(
                f"{len(late)} response frame(s) arrived well after the empty "
                f"poll set was acknowledged; §15.7 -- the empty set stops "
                f"the transmitter, and transmit MUST NOT outlive the request "
                f"that turned it off")
        flagged = [b for _, b in after if b["header"]["flags"] & POLLING]
        if flagged:
            raise Fail(
                f"{len(flagged)} batch(es) flushed after the stop still "
                f"carry the polling flag; §15.6 -- the flag is set exactly "
                f"while the poll set is non-empty, and a flag that outlives "
                f"the stop hides the one transition a user most needs to see")
    finally:
        # Leave the device as this check found it: nothing polled, nothing
        # subscribed. CAN_RESET clears both tables (§15.7), and the observe
        # phase installs its own subscriptions afterwards.
        try:
            await c.request(refdec.OPCODE["CAN_RESET"])
        except ControlTimeout:
            pass


@check(id="obd.reset_stops", section="15.7", phase="control", severity="MUST",
       requires=("obd",), adversarial=True,
       title="CAN_RESET clears the poll set and silences the transmitter")
async def obd_reset_stops(s):
    c = _control(s)
    probe = _probe(s)
    if s.info is None:
        raise Skip("Info did not decode")
    if not probe["probe"]["validity"] & RESPONDED:
        raise Skip("nothing answered the probe, so there is nothing to poll")
    supported = next((pid for pid in range(0x01, 0x61)
                      if _union_bit(probe, pid)), None)
    if supported is None:
        raise Skip("the probe's union claims no PID at all")
    ecu_ids = {e["id"] for e in probe["ecus"]}
    interval = max(s.info["obd_min_interval_ms"], 25)
    log = s.streams["can"]
    try:
        # §15.7 -- the probe result survives the previous check's CAN_RESET,
        # so a poll set re-arms without a second probe.
        r = await c.request(
            refdec.OPCODE["OBD_POLL_SET"],
            struct.pack("<HB", interval, 1) + bytes([supported]))
        if not r.ok:
            raise Fail(f"re-arming the poll set after CAN_RESET was answered "
                       f"{r.status_name}; §15.7 -- the probe result is a "
                       f"fact about the car and survives the reset")
        await asyncio.sleep(max(2 * interval / 1000, 0.15))
        r = await c.request(refdec.OPCODE["CAN_RESET"])
        if not r.ok:
            raise Fail(f"CAN_RESET was answered {r.status_name}")
        # Grace for responses already in flight and the reset's own effects,
        # then a window in which nothing OBD may appear.
        await asyncio.sleep(max(2 * interval / 1000, 0.15))
        t_quiet = time.monotonic()
        await asyncio.sleep(max(3 * interval / 1000, 0.3))
        offending = []
        for t, n in ((n.t_host, n) for n in log.since(t_quiet)):
            try:
                batch = refdec.decode("can_batch", n.payload)
            except refdec.Reject:
                continue
            if any(_identity(rec) in ecu_ids for rec in batch["records"]) \
                    or batch["header"]["flags"] & POLLING:
                offending.append(batch)
        if offending:
            raise Fail(
                f"{len(offending)} batch(es) after CAN_RESET still carry OBD "
                f"responses or the polling flag; §15.7 -- the one opcode "
                f"that clears the receiver clears the transmitter with it, "
                f"and a device that keeps transmitting through it is exactly "
                f"the device an app cannot silence")
    finally:
        try:
            await c.request(refdec.OPCODE["OBD_POLL_SET"],
                            struct.pack("<HB", 0, 0))
        except ControlTimeout:
            pass
