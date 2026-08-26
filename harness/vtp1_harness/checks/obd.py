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


def _schedule(pids, groups=None, min_ms=0):
    """SPEC.md 15.4.1's layout: PID bytes with `more` set on all but the last
    of a group, then that group's u16 minimum interval (SPEC.md 15.4.2).

    `pids` alone makes every PID its own group with no minimum, which is what
    every check that does not care about rate limiting wants.
    """
    out = bytearray()
    for group in (groups if groups is not None else [(p,) for p in pids]):
        for i, pid in enumerate(group):
            out.append(pid | (0x80 if i < len(group) - 1 else 0))
        out += struct.pack("<H", min_ms)
    return bytes(out)


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
    """SPEC.md §15.2 -- a probe is up to three requests, each spaced by the
    50 ms collection window, plus whatever the car takes to answer. Generous,
    because the fixed control timeout would report a slow but conforming probe
    unanswered and there is no longer a declared floor to size against."""
    return 5.0


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
    interval = 25
    r = await c.request(refdec.OPCODE["OBD_POLL_SET"],
                        struct.pack("<HB", interval, 1) + _schedule([0x0C]))
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
    zero = [name for name in ("obd_poll_slots",)
            if not s.info[name]]
    if zero:
        raise Fail(
            f"bit 10 is set and {', '.join(zero)} "
            f"{'is' if len(zero) == 1 else 'are'} zero. §15 -- a poll set "
            f"nothing fits in describes a role no conforming exchange can "
            f"use, exactly as §9.7 forbids declaring `power` and answering "
            f"with nothing valid")
    raise Observe(f"up to {s.info['obd_poll_slots']} PIDs per set; polling is "
                  f"response-paced, so there is no declared rate")


@check(id="obd.poll_refusals", section="15.4", phase="control", severity="MUST",
       requires=("obd",), adversarial=True,
       title="OBD_POLL_SET refuses what §15.4 says it must")
async def obd_poll_refusals(s):
    c = _control(s)
    probe = _probe(s)
    if s.info is None:
        raise Skip("Info did not decode")
    slots = s.info["obd_poll_slots"]
    interval = 25
    problems = []

    async def poll_set(interval_ms, pids):
        return await c.request(
            refdec.OPCODE["OBD_POLL_SET"],
            struct.pack("<HB", interval_ms, len(pids)) + _schedule(pids))

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
    # SPEC.md 15.4 -- interval_ms 0 is ACCEPTED now, and that reversal is
    # worth asserting rather than merely not testing. Zero used to mean
    # unbounded generation; under response pacing the device waits for the
    # answer before transmitting, so zero means "the client imposes no
    # throttle" and the car is the bound.
    zero_pid = next((pid for pid in range(0x01, 0x61)
                     if probe["probe"]["validity"] & RESPONDED
                     and _union_bit(probe, pid)), None)
    if zero_pid is not None:
        r = await poll_set(0, [zero_pid])
        if not r.ok:
            problems.append(
                f"a non-empty set with interval_ms 0 (PID "
                f"0x{zero_pid:02X}, which the probe's union claims) was "
                f"answered {r.status_name}; §15.4 -- pacing makes zero "
                f"bounded by the car, so it is a client declining to "
                f"throttle rather than a request for unbounded traffic")
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
    interval = 25
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
            struct.pack("<HB", interval, 1) + _schedule([supported]))
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
            # car may have gone quiet since the probe, or this one PID may
            # simply never be answered. A fresh probe tells a dead bus from
            # a quiet PID -- §15.2 makes each one a fresh measurement (and
            # clears the poll set, so the second look below re-arms it).
            try:
                again = await c.request(refdec.OPCODE["OBD_INFO"],
                                        timeout=_probe_timeout(s))
            except ControlTimeout:
                raise Fail("OBD_INFO went unanswered while diagnosing a "
                           "silent poll; §9 requires a response to every "
                           "request") from None
            if not again.ok:
                raise Fail(
                    f"the diagnostic re-probe was answered "
                    f"{again.status_name}; §15.2 -- a device that declares "
                    f"`obd` answers OBD_INFO on every request, and refusing "
                    f"it mid-connection leaves a silent poll undiagnosable")
            try:
                fresh = refdec.decode("obd_info", again.detail)
            except refdec.Reject as exc:
                raise Fail(f"the re-probe's detail did not decode: "
                           f"{exc}", detail=again.detail.hex()) from None
            if not fresh["probe"]["validity"] & RESPONDED:
                raise Skip(
                    "the car stopped answering between the probe and the "
                    "poll -- nothing was delivered because nothing was on "
                    "the bus, which §15.4 makes the honest outcome")
            # The car still answers a probe, yet the poll delivered nothing.
            # §15.4 lets a device abandon every request the bus does not
            # answer, so the silence alone proves nothing: failing here
            # needs independent evidence that answers exist and are being
            # discarded. Second look: subscribe the fresh probe's response
            # identifiers and re-arm the set (the re-probe cleared it,
            # §15.2). Frames arriving NOW are answers the fallback withheld.
            fresh_pid = next((pid for pid in range(0x01, 0x61)
                              if _union_bit(fresh, pid)), None)
            if fresh_pid is None:
                raise Skip("the re-probe's union claims no PID at all, so "
                           "no request can be polled for evidence")
            fresh_ids = {e["id"] for e in fresh["ecus"]}
            for cid in sorted(fresh_ids):
                r2 = await c.request(refdec.OPCODE["CAN_SUBSCRIBE"],
                                     struct.pack("<IBH", cid, 0, 0))
                if not r2.ok:
                    raise Fail(f"subscribing reported response identifier "
                               f"0x{cid:X} was answered {r2.status_name} "
                               f"while diagnosing a silent poll")
            r2 = await c.request(
                refdec.OPCODE["OBD_POLL_SET"],
                struct.pack("<HB", interval, 1) + _schedule([fresh_pid]))
            if not r2.ok:
                raise Fail(f"re-arming the poll set against the fresh probe "
                           f"result was answered {r2.status_name}; §15.2 "
                           f"left that result standing")
            t_second = time.monotonic()
            await asyncio.sleep(settle)
            evidence = [rec for _, b in batches_since(t_second)
                        for rec in b["records"]
                        if _identity(rec) in fresh_ids]
            if evidence:
                raise Fail(
                    f"{len(evidence)} response frame(s) arrived once the "
                    f"probe's reported identifiers were subscribed, and "
                    f"none arrived with nothing subscribed; §15.5 -- the "
                    f"accepted poll set alone is the delivery path, and "
                    f"this device delivers the answers only through the "
                    f"table")
            raise Skip(
                f"the poll went unanswered with and without subscriptions "
                f"installed (PID 0x{supported:02X}, then "
                f"0x{fresh_pid:02X}); §15.4 lets a device abandon every "
                f"unanswered request, so nothing observable separates a "
                f"quiet PID from a discarded answer")
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
    interval = 25
    log = s.streams["can"]
    try:
        # §15.7 -- the probe result survives the previous check's CAN_RESET,
        # so a poll set re-arms without a second probe.
        r = await c.request(
            refdec.OPCODE["OBD_POLL_SET"],
            struct.pack("<HB", interval, 1) + _schedule([supported]))
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


# --- SPEC.md 15.4.1: PID grouping ------------------------------------------
#
# Bit 11 is advertised to clients, so a device claiming it must be held to it
# by the tool third parties actually run. Without these a device could
# declare grouping, refuse every grouped request, and pass the suite -- which
# would make the bit worth nothing, exactly the failure RATIONALE 11.2 says
# the capability bit exists to prevent.

MORE = 0x80


def _supported_pids(probe, n):
    """The first `n` PIDs the probe's union claims, or fewer."""
    return [pid for pid in range(0x01, 0x61) if _union_bit(probe, pid)][:n]


@check(id="obd.grouping_refusals", section="15.4.1", phase="control",
       severity="MUST", requires=("obd",), adversarial=True,
       title="The schedule layout refuses what §15.4.1 and §15.4.2 say it must")
async def obd_grouping_refusals(s):
    c = _control(s)
    probe = _probe(s)
    if s.info is None:
        raise Skip("Info did not decode")
    if not probe["probe"]["validity"] & RESPONDED:
        raise Skip("nothing answered the probe, so nothing is pollable")
    interval = 25
    pids = _supported_pids(probe, 7)
    if len(pids) < 2:
        raise Skip("the probe's union claims too few PIDs to group")
    problems = []

    async def poll_set(payload, min_ms=0):
        """`payload` is PID bytes with `more` already set where wanted."""
        out, n = bytearray(), 0
        for b in payload:
            out.append(b)
            n += 1
            if not b & MORE:
                out += struct.pack("<H", min_ms)
        return await c.request(
            refdec.OPCODE["OBD_POLL_SET"],
            struct.pack("<HB", interval, n) + bytes(out))

    # Rule 7 -- a group that continues past the end of the list.
    r = await poll_set([pids[0], pids[1] | MORE])
    if r.status != refdec.STATUS_VALUE["bad_params"]:
        problems.append(
            f"a set ending with bit 7 set (0x{pids[1] | MORE:02X}) was "
            f"answered {r.status_name}; §15.4.1 rule 7 -- a group that "
            f"continues into nothing is not a schedule")

    # Rule 6 -- seven PIDs do not fit `[1+g, 0x01, p1..pg]` in eight bytes.
    if len(pids) >= 7 and s.info["obd_poll_slots"] >= 7:
        r = await poll_set([p | MORE for p in pids[:6]] + [pids[6]])
        if r.status != refdec.STATUS_VALUE["bad_params"]:
            problems.append(
                f"a group of seven PIDs was answered {r.status_name}; "
                f"§15.4.1 rule 6 -- it does not fit the request frame")

    # ...and six, which does, must be accepted. A device refusing the
    # boundary declares a capacity it will not honour.
    if len(pids) >= 6 and s.info["obd_poll_slots"] >= 6:
        r = await poll_set([p | MORE for p in pids[:5]] + [pids[5]])
        if not r.ok:
            problems.append(
                f"a group of exactly six PIDs was answered {r.status_name}; "
                f"§15.4.1 rule 6 bounds groups at six, and six fits")

    # SPEC.md 15.4.2 -- 0 means "no minimum" and MUST be accepted, unlike a
    # ratio's zero which would name a group that never transmits.
    r = await poll_set([pids[0]], min_ms=0)
    if not r.ok:
        problems.append(
            f"a minimum interval of 0 was answered {r.status_name}; §15.4.2 "
            f"-- zero subtracts nothing and is how a client says `every pass`")
    # A truncated interval: the parse IS the length check.
    r = await c.request(refdec.OPCODE["OBD_POLL_SET"],
                        struct.pack("<HB", interval, 1)
                        + bytes([pids[0], 0]))
    if r.status != refdec.STATUS_VALUE["bad_params"]:
        problems.append(
            f"a schedule whose last group carries a one-byte minimum was "
            f"answered {r.status_name}; §15.4 rule 1")

    # Rule 4 still tests bits 0-6: an unverified PID inside a group is
    # refused for being unverified, not accepted for being flagged.
    unsupported = next((pid for pid in range(0x01, 0x61)
                        if not _union_bit(probe, pid)), None)
    if unsupported is not None:
        r = await poll_set([unsupported | MORE, pids[0]])
        if r.status != refdec.STATUS_VALUE["bad_params"]:
            problems.append(
                f"PID 0x{unsupported:02X}, which the probe's union does not "
                f"claim, was answered {r.status_name} when carried inside a "
                f"group; §15.4.1 -- rule 5 tests bits 0-6 and is unamended")

    await c.request(refdec.OPCODE["OBD_POLL_SET"], struct.pack("<HB", 0, 0))
    if problems:
        raise Fail("; ".join(problems))


@check(id="obd.grouping_is_one_request", section="15.4.1", phase="control",
       severity="MUST", requires=("obd",),
       title="A group is ONE request, not a schedule of its members")
async def obd_grouping_is_one_request(s):
    """The defect this exists for: a device that parses bit 7, answers `ok`,
    and then walks the PIDs individually anyway. Every refusal check above
    passes on such a device, and the client gets HALF the rate it asked for
    with nothing on the wire to say so.

    Observable without per-ECU knowledge, and without a J1979 size table.
    Every Mode 01 answer echoes the PIDs of the request that caused it in
    order, so the FIRST echoed PID names which request this is an answer to.
    With one group `(p0, p1)` at interval I, a conforming device asks for
    both every I, so p0 leads an answer at about T/I distinct bus instants.
    A device that scheduled them individually alternates, so p0 leads at
    about T/(2I). The factor of two is the whole assertion.

    Counted by distinct bus-arrival INSTANT and not by frame: functional
    addressing means several ECUs answer one request, and counting frames
    would multiply by however many this car has.
    """
    c = _control(s)
    probe = _probe(s)
    if s.info is None:
        raise Skip("Info did not decode")
    if not probe["probe"]["validity"] & RESPONDED:
        raise Skip("nothing answered the probe, so nothing is pollable")
    pids = _supported_pids(probe, 2)
    if len(pids) < 2:
        raise Skip("the probe's union claims fewer than two PIDs")
    ecu_ids = {e["id"] for e in probe["ecus"]}
    interval = 25
    window = max(20 * interval / 1000, 1.0)
    log = s.streams["can"]

    r = await c.request(refdec.OPCODE["CAN_RESET"])
    if not r.ok:
        raise Fail(f"CAN_RESET was answered {r.status_name}")
    try:
        r = await c.request(
            refdec.OPCODE["OBD_POLL_SET"],
            struct.pack("<HB", interval, 2) + _schedule(None, [(pids[0], pids[1])]))
        if not r.ok:
            raise Fail(f"a two-PID group of probed, supported PIDs at "
                       f"{interval} ms was answered {r.status_name}, on a "
                       f"device declaring bit 11")
        await asyncio.sleep(max(3 * interval / 1000, 0.35))
        t0 = time.monotonic()
        await asyncio.sleep(window)
        elapsed = time.monotonic() - t0

        leads = {pids[0]: set(), pids[1]: set()}
        answers = 0
        for n in log.since(t0):
            try:
                batch = refdec.decode("can_batch", n.payload)
            except refdec.Reject:
                continue
            for rec in batch["records"]:
                if _identity(rec) not in ecu_ids:
                    continue
                payload = bytes.fromhex(rec["payload"])
                if len(payload) < 3 or payload[0] >> 4 != 0 \
                        or payload[1] != 0x41:
                    continue        # a first frame, or not Mode 01 at all
                answers += 1
                if payload[2] in leads:
                    leads[payload[2]].add(rec["t_device_us"])
        if not answers:
            raise Skip("the car answered nothing during the window; §15.4 "
                       "makes an unanswered request the honest outcome and "
                       "obd.poll_and_flag owns that diagnosis")
        grouped = max(len(leads[pids[0]]), len(leads[pids[1]]))
        if not grouped:
            raise Skip(f"no answer led with 0x{pids[0]:02X} or "
                       f"0x{pids[1]:02X}, so which request each answers "
                       f"cannot be told apart")

        # Calibrate against THIS device on THIS car, by running the same two
        # PIDs as two separate groups. Comparing against `elapsed / interval`
        # assumed the fixed clock §15.4 replaced: under response pacing the
        # spacing is max(interval, the car's latency), so a conforming device
        # on a 50 ms car was failed for producing the 19 answers the car
        # allowed against the ~40 an interval of 25 implied -- while a
        # fixed-clock device, which is the actual defect, sailed through.
        # The ratio between grouped and split is latency-free.
        r = await c.request(
            refdec.OPCODE["OBD_POLL_SET"],
            struct.pack("<HB", interval, 2)
            + _schedule([pids[0], pids[1]]))
        if not r.ok:
            raise Fail(f"the same two PIDs as separate groups were answered "
                       f"{r.status_name}")
        await asyncio.sleep(max(3 * interval / 1000, 0.35))
        t1 = time.monotonic()
        await asyncio.sleep(window)

        # Distinct instants, exactly as the grouped run counts them: several
        # ECUs answer one request, so counting frames here and instants there
        # compared two different quantities and read 0.97 on a conforming
        # device.
        split_instants = set()
        for n in log.since(t1):
            try:
                batch = refdec.decode("can_batch", n.payload)
            except refdec.Reject:
                continue
            for rec in batch["records"]:
                if _identity(rec) not in ecu_ids:
                    continue
                payload = bytes.fromhex(rec["payload"])
                if len(payload) >= 3 and payload[0] >> 4 == 0 \
                        and payload[1] == 0x41 and payload[2] == pids[0]:
                    split_instants.add(rec["t_device_us"])
        split = len(split_instants)
        if not split:
            raise Skip("the split schedule produced no comparable answers")
        # Grouped asks for pids[0] every pass; split asks for it every other
        # pass. A device that ignores the grouping produces the same number
        # both ways, so anything at or above 1.4x is comfortably clear of 1.0
        # while tolerating a car whose latency wandered between the two runs.
        if grouped < 1.4 * split:
            raise Fail(
                f"0x{pids[0]:02X} was answered at {grouped} distinct instants "
                f"when grouped with 0x{pids[1]:02X} and {split} when the two "
                f"were separate groups, a ratio of {grouped / split:.2f}. "
                f"§15.4.1 -- a group is ONE request, so grouping the pair "
                f"should roughly double its rate; a device that answers `ok` "
                f"and then schedules the PIDs individually reads 1.0 and has "
                f"told the client nothing")
    finally:
        await c.request(refdec.OPCODE["OBD_POLL_SET"], struct.pack("<HB", 0, 0))


@check(id="obd.group_minimum_is_honoured", section="15.4.2", phase="control",
       severity="MUST", requires=("obd",),
       title="A group's minimum interval holds its rate, and only its group")
async def obd_group_minimum_is_honoured(s):
    """SPEC.md 15.4.2 -- the defect this exists for is a device that parses the
    minimum, answers `ok`, and transmits every group every pass anyway. The
    client then gets its slow channels at the fast rate: bus traffic it
    explicitly asked not to generate, and nothing in the stream says so.

    Asserted as an ABSOLUTE rate, because that is what the field means. A
    ratio would have to be measured against the achieved cycle, which under
    response pacing is the car's and not a constant.
    """
    c = _control(s)
    probe = _probe(s)
    if s.info is None:
        raise Skip("Info did not decode")
    if not probe["probe"]["validity"] & RESPONDED:
        raise Skip("nothing answered the probe, so nothing is pollable")
    pids = _supported_pids(probe, 2)
    if len(pids) < 2:
        raise Skip("the probe's union claims fewer than two PIDs")
    ecu_ids = {e["id"] for e in probe["ecus"]}
    min_ms, window = 500, 4.0
    log = s.streams["can"]

    r = await c.request(refdec.OPCODE["CAN_RESET"])
    if not r.ok:
        raise Fail(f"CAN_RESET was answered {r.status_name}")
    try:
        schedule = (bytes([pids[0]]) + struct.pack("<H", 0)
                    + bytes([pids[1]]) + struct.pack("<H", min_ms))
        r = await c.request(refdec.OPCODE["OBD_POLL_SET"],
                            struct.pack("<HB", 0, 2) + schedule)
        if not r.ok:
            raise Fail(f"a schedule mixing minimum intervals was answered "
                       f"{r.status_name}")
        await asyncio.sleep(0.4)
        t0 = time.monotonic()
        await asyncio.sleep(window)
        elapsed = time.monotonic() - t0

        leads = {pids[0]: set(), pids[1]: set()}
        for n in log.since(t0):
            try:
                batch = refdec.decode("can_batch", n.payload)
            except refdec.Reject:
                continue
            for rec in batch["records"]:
                if _identity(rec) not in ecu_ids:
                    continue
                payload = bytes.fromhex(rec["payload"])
                if len(payload) < 3 or payload[0] >> 4 != 0 \
                        or payload[1] != 0x41:
                    continue
                if payload[2] in leads:
                    leads[payload[2]].add(rec["t_device_us"])
        fast, slow = len(leads[pids[0]]), len(leads[pids[1]])
        if not fast:
            raise Skip("the car answered neither PID; §15.4 makes an "
                       "unanswered request the honest outcome")
        # Replies clustered into transmissions, then the GAP between them.
        # Functional addressing means several ECUs answer one request and
        # they need not answer together, so raw instants read one
        # transmission as several; a tolerance well above that stagger and
        # well below the minimum collapses them. Testing the gap rather than
        # a count is what makes this able to fail at all -- counting arrivals
        # per minimum-sized bucket can never exceed one bucket per minimum,
        # however fast the device actually transmits.
        tolerance = min_ms * 1000 // 4
        clusters = []
        for t in sorted(leads[pids[1]]):
            if not clusters or t - clusters[-1] > tolerance:
                clusters.append(t)
        gaps = [(b - a) / 1000.0 for a, b in zip(clusters, clusters[1:])]
        tight = [g for g in gaps if g < min_ms * 0.8]
        if tight:
            raise Fail(
                f"0x{pids[1]:02X} carries a {min_ms} ms minimum and was "
                f"transmitted {len(tight)} time(s) sooner than that, the "
                f"closest {min(tight):.0f} ms after the one before. "
                f"§15.4.2 -- the minimum is a rate the client is owed, and a "
                f"device ignoring it generates traffic that was declined")
        if not slow:
            # NOT a failure. §15.4 makes an unanswered PID legal and the
            # transmitter is not observable from the stream, so a car that
            # does not answer this PID and a device that never asked report
            # identically. Distinguishing them needs
            # instrumentation no client has.
            raise Observe(
                f"0x{pids[1]:02X} produced no answer, so its minimum "
                f"interval could not be verified: §15.4 makes an unanswered "
                f"PID legal and the stream cannot tell that from a group "
                f"never transmitted")
    finally:
        await c.request(refdec.OPCODE["OBD_POLL_SET"], struct.pack("<HB", 0, 0))
