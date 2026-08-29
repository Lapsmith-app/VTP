"""SPEC.md §5-8 -- what the device actually sends, and the bookkeeping under it.

The corpus proves a decoder handles a payload. These checks prove a device
produces one, and then prove the things only a sequence of them can show: that
the sequence number starts where §8.2 says, that the clock never goes backwards,
and that the three streams are on one clock rather than three.
"""
import asyncio
import statistics
import struct

from .. import refdec
from . import Fail, Observe, Skip, check

RECORD = {"gps": "gps_fix", "can": "can_batch", "imu": "imu_batch"}
SEQ_RECORD = {"gps": "gps_fix", "can": "can_header", "imu": "imu_header"}
#: Where each stream's device timestamp lives, once decoded.
TIMESTAMP = {
    "gps": lambda d: d["t_device"],
    "can": lambda d: d["header"]["t_base"],
    "imu": lambda d: d["header"]["t_base"],
}

#: Two clocks that differ by more than this are not one clock. Host scheduling
#: and BLE queueing are tens of milliseconds; a second is far outside them and
#: far inside the gap a per-sensor timer produces.
ONE_CLOCK_TOLERANCE_US = 1_000_000

PROBE_MASK_ALL = 0


def _log(s, stream):
    if not s.has(stream):
        raise Skip(f"this device does not declare the {stream} capability")
    log = s.streams[stream]
    if not len(log):
        raise Skip(f"no {stream} notification arrived")
    return log


def _decoded(s, stream):
    """Decode every notification once, and cache it. Failures are kept."""
    cache = s.state.setdefault("_decoded", {})
    log = s.streams[stream]
    entry = cache.get(stream)
    if entry is not None and entry[0] == len(log):
        return entry[1], entry[2]
    good, bad = [], []
    for item in log.items:
        try:
            good.append((item, refdec.decode(RECORD[stream], item.payload)))
        except refdec.Reject as exc:
            bad.append((item, str(exc)))
    cache[stream] = (len(log), good, bad)
    return good, bad


def _seq_of(stream, payload):
    return struct.unpack_from(
        "<H", payload, refdec.offset(SEQ_RECORD[stream], "seq"))[0]


# ---------------------------------------------------------------------------
# Before anything was asked for
# ---------------------------------------------------------------------------

@check(id="can.silent_until_asked", section="9.1", phase="info", severity="MUST",
       requires=("can",),
       title="No CAN frame arrives before a subscription is installed")
async def can_silent_until_asked(s):
    log = s.streams["can"]
    carrying = []
    for item in log.items:
        try:
            batch = refdec.decode("can_batch", item.payload)
        except refdec.Reject:
            continue
        if batch["records"]:
            carrying.append(item)
    if carrying:
        # §9.1 clears the table when the link drops, so a fresh connection
        # matches nothing. A device streaming here has invented consent, and a
        # client that trusted it would be recording a bus it never asked for.
        raise Fail(
            f"{len(carrying)} CAN notification(s) carrying frames arrived "
            f"before any subscription was installed. The table MUST be clear on "
            f"a new connection",
            first=carrying[0].payload.hex())


# ---------------------------------------------------------------------------
# Asking for traffic
# ---------------------------------------------------------------------------

@check(id="can.subscribe_for_observation", section="9.1", phase="observe",
       severity="OBSERVE", requires=("control", "can"),
       title="Subscribe so there is CAN traffic to judge")
async def can_subscribe_for_observation(s):
    c = s.control
    if c is None:
        raise Skip("no control plane")
    wanted = s.state.get("can_ids") or []
    if wanted:
        installed = {}
        for can_id in wanted:
            response = await c.subscribe_can(can_id)
            if response.ok:
                installed[can_id] = refdec.MASK_EXACT
        s.state["installed"] = installed
        s.state["can_catch_all"] = False
        if not installed:
            raise Fail("none of the requested identifiers could be subscribed",
                       requested=[hex(i) for i in wanted])
        raise Observe(f"subscribed to {', '.join(hex(i) for i in installed)}",
                      installed=list(installed))
    if not s.has("masked_subscriptions"):
        # Without masks there is no way to ask for "whatever is on the bus", and
        # the harness cannot guess identifiers. Say so rather than report the
        # CAN checks as passing on no data.
        raise Skip(
            "this device does not declare masked subscriptions, so the harness "
            "cannot ask for every frame and has no way to guess which "
            "identifiers your bus carries. Re-run with --can-id to name them")
    response = await c.request(
        refdec.OPCODE["CAN_SUBSCRIBE_MASK"],
        struct.pack("<IIBH", 0, PROBE_MASK_ALL, 0, 0))
    if not response.ok:
        raise Fail(f"a mask of zero, which §9.1 defines as covering every frame, "
                   f"was answered {response.status_name}",
                   response=response.raw.hex())
    s.state["installed"] = {0: 0}
    s.state["can_catch_all"] = True
    raise Observe("subscribed to every identifier with a mask of zero")


# ---------------------------------------------------------------------------
# The payloads themselves
# ---------------------------------------------------------------------------

def _decode_check(stream, section, title):
    async def fn(s):
        _log(s, stream)
        good, bad = _decoded(s, stream)
        if bad:
            item, reason = bad[0]
            raise Fail(
                f"{len(bad)} of {len(good) + len(bad)} {stream} notification(s) "
                f"were rejected by the reference decoder; the first failed "
                f"'{reason}'",
                payload=item.payload.hex(),
                reasons=sorted({r for _, r in bad}))
        raise Observe(f"{len(good)} notification(s), all decoded",
                      count=len(good))
    fn.__name__ = f"{stream}_decodes"
    return check(id=f"{stream}.decodes", section=section, phase="streams",
                 severity="MUST", requires=(stream,), title=title)(fn)


_decode_check("gps", "5", "Every GPS notification decodes")
_decode_check("can", "6", "Every CAN notification decodes")
_decode_check("imu", "7", "Every IMU notification decodes")


@check(id="gps.absent_fields_zero", section="5.1", phase="streams",
       severity="MUST", requires=("gps",),
       title="A field whose validity bit is clear is written as zero")
async def gps_absent_fields_zero(s):
    _log(s, "gps")
    good, _ = _decoded(s, "gps")
    for item, _ in good:
        stale = refdec.absent_but_nonzero("gps_fix", item.payload, "gps_validity")
        if stale:
            # The payload decodes perfectly, so no byte vector catches this. A
            # client that reads the field anyway gets a measurement with a
            # validity bit denying it exists.
            raise Fail(
                f"{len(stale)} field(s) whose validity bit is clear are "
                f"non-zero: "
                f"{', '.join(f'{n}={v}' for n, v in stale)}. §5.1 requires them "
                f"written as zero, so absence cannot be mistaken for a reading",
                payload=item.payload.hex())


@check(id="gps.reserved_bits", section="5", phase="streams", severity="MUST",
       requires=("gps",),
       title="Reserved validity and fix_flags bits are zero")
async def gps_reserved_bits(s):
    _log(s, "gps")
    good, _ = _decoded(s, "gps")
    validity_reserved = refdec.reserved_mask("gps_validity", 32)
    flags_reserved = refdec.reserved_mask("fix_flags", 8)
    for item, fix in good:
        if fix["validity"] & validity_reserved:
            raise Fail(f"gps_fix.validity has reserved bits set "
                       f"(0x{fix['validity']:08x}); Appendix A holds bits 12-31",
                       payload=item.payload.hex())
        if fix["fix_flags"] & flags_reserved:
            raise Fail(f"gps_fix.fix_flags has reserved bits set "
                       f"(0x{fix['fix_flags']:02x})",
                       payload=item.payload.hex())


@check(id="gps.fix_types", section="5.2", phase="streams", severity="OBSERVE",
       requires=("gps",),
       title="Fix types and solution quality seen")
async def gps_fix_types(s):
    _log(s, "gps")
    good, _ = _decoded(s, "gps")
    known = refdec.enum_values("fix_type")
    seen = {}
    unknown = set()
    for _, fix in good:
        name = known.get(fix["fix_type"])
        if name is None:
            unknown.add(fix["fix_type"])
            name = f"unknown({fix['fix_type']})"
        seen[name] = seen.get(name, 0) + 1
    if unknown:
        s.note(f"the device reported fix_type value(s) {sorted(unknown)}, which "
               f"this version does not define. A client MUST report them "
               f"unknown rather than coerce them to a default.")
    epoch_bit = 1 << refdec.bit("fix_flags", "solution_epoch")
    epoch = sum(1 for _, fix in good if fix["fix_flags"] & epoch_bit)
    raise Observe(
        ", ".join(f"{name} x{n}" for name, n in sorted(seen.items()))
        + f"; {epoch}/{len(good)} timestamped at the solution epoch (5.6)",
        fix_types=seen, solution_epoch=epoch)


@check(id="can.header_reserved", section="6", phase="streams", severity="MUST",
       requires=("can",),
       title="can_header.reserved is zero")
async def can_header_reserved(s):
    _log(s, "can")
    good, _ = _decoded(s, "can")
    for item, batch in good:
        if batch["header"]["reserved"] != 0:
            raise Fail(
                f"can_header.reserved is 0x{batch['header']['reserved']:04x}. "
                f"Appendix A earmarks its low byte for a bus index (6.9), so a "
                f"non-zero value today is a frame attributed to a bus that does "
                f"not exist tomorrow", payload=item.payload.hex())


@check(id="imu.reserved_bits", section="7", phase="streams", severity="MUST",
       requires=("imu",),
       title="imu_header reserved bytes and flag bits are zero")
async def imu_reserved_bits(s):
    _log(s, "imu")
    good, _ = _decoded(s, "imu")
    reserved_flags = 0xF8            # Appendix A: imu_header.flags bits 3-7
    for item, batch in good:
        header = batch["header"]
        if header["reserved"] != 0:
            raise Fail(f"imu_header.reserved is 0x{header['reserved']:04x}",
                       payload=item.payload.hex())
        if header["flags"] & reserved_flags:
            raise Fail(f"imu_header.flags has reserved bits set "
                       f"(0x{header['flags']:02x}); Appendix A holds bits 3-7 "
                       f"for sensor groups added later",
                       payload=item.payload.hex())


@check(id="imu.saturation", section="7.2", phase="streams", severity="OBSERVE",
       requires=("imu",),
       title="Whether any IMU batch reported saturation")
async def imu_saturation(s):
    _log(s, "imu")
    good, _ = _decoded(s, "imu")
    saturated = sum(1 for _, batch in good if batch["header"]["saturated"])
    if not saturated:
        raise Observe(f"no saturated batch in {len(good)}")
    raise Observe(
        f"{saturated} of {len(good)} batches reported saturation; those samples "
        f"are 'at least this much', not a measurement",
        saturated=saturated, total=len(good))


# ---------------------------------------------------------------------------
# SPEC.md §8 -- sequence, clock and loss
# ---------------------------------------------------------------------------

@check(id="seq.first_is_zero", section="8.2", phase="streams", severity="MUST",
       title="The first notification on each characteristic carries seq 0")
async def seq_first_is_zero(s):
    subscribed = [n for n in s.STREAMS if s.has(n) and len(s.streams[n])]
    if not subscribed:
        raise Skip("no stream produced a notification")
    problems = []
    for name in subscribed:
        first = s.streams[name].items[0]
        seq = _seq_of(name, first.payload)
        if seq != 0:
            problems.append(f"{name} started at seq {seq}")
    if problems:
        # The exact bug 8.2 was rewritten to describe: "restarts at 0" read as
        # zeroing the counter and then taking the next value, which puts 1 on
        # the wire -- and a device's own conformance check written to match, so
        # the test agreed with the bug it existed to catch.
        raise Fail(
            "; ".join(problems) + ". The first notification sent on a "
            "characteristic after a connection is established carries seq 0, "
            "and the second carries 1",
            streams=subscribed)


@check(id="seq.advances", section="8.2", phase="streams", severity="MUST",
       title="The sequence number increments by one and wraps at 65535")
async def seq_advances(s):
    subscribed = [n for n in s.STREAMS if s.has(n) and len(s.streams[n]) > 1]
    if not subscribed:
        raise Skip("no stream produced two notifications")
    problems, gaps = [], {}
    for name in subscribed:
        items = s.streams[name].items
        missed = 0
        for previous, current in zip(items, items[1:]):
            a, b = _seq_of(name, previous.payload), _seq_of(name, current.payload)
            step = (b - a) & 0xFFFF
            if step == 0:
                problems.append(f"{name} repeated seq {a}")
                break
            if step != 1:
                # A gap means notifications the device sent and this host did
                # not receive -- real, and not the device's fault. Counted and
                # reported rather than failed.
                missed += step - 1
        if missed:
            gaps[name] = missed
    if problems:
        raise Fail("; ".join(problems) + ". seq increments by exactly one per "
                   "notification on its own characteristic")
    s.state["seq_gaps"] = gaps


@check(id="seq.gaps", section="8.2", phase="streams", severity="OBSERVE",
       title="Notifications the host did not receive")
async def seq_gaps(s):
    gaps = s.state.get("seq_gaps")
    if gaps is None:
        raise Skip("the sequence check did not run")
    if not gaps:
        raise Observe("no sequence gaps: every notification the device sent "
                      "arrived")
    total = {name: len(s.streams[name]) for name in gaps}
    raise Observe(
        "; ".join(f"{name}: {n} missed against {total[name]} received"
                  for name, n in gaps.items())
        + ". A gap is the host's stack losing what the device sent, not the "
          "device losing what it measured",
        gaps=gaps)


@check(id="clock.monotonic", section="8.1", phase="streams", severity="MUST",
       title="The device clock never goes backwards")
async def clock_monotonic(s):
    subscribed = [n for n in s.STREAMS if s.has(n) and len(s.streams[n]) > 1]
    if not subscribed:
        raise Skip("no stream produced two notifications")
    for name in subscribed:
        good, _ = _decoded(s, name)
        stamps = [(item, TIMESTAMP[name](decoded)) for item, decoded in good]
        for (_, before), (item, after) in zip(stamps, stamps[1:]):
            if after < before:
                raise Fail(
                    f"{name} timestamps went backwards: {before} then {after} "
                    f"({(before - after) / 1000:.1f} ms). The clock MUST NOT "
                    f"jump backwards while connected, and a device disciplining "
                    f"to GNSS MUST apply corrections as a frequency adjustment "
                    f"rather than a step",
                    payload=item.payload.hex())


@check(id="clock.one_clock", section="8.1", phase="streams", severity="MUST",
       title="Every stream is timestamped against the same clock")
async def clock_one_clock(s):
    present = [n for n in s.STREAMS if s.has(n) and len(s.streams[n]) >= 3]
    if len(present) < 2:
        raise Skip("fewer than two streams produced enough notifications to "
                   "compare")
    offsets = {}
    for name in present:
        good, _ = _decoded(s, name)
        offsets[name] = statistics.median(
            TIMESTAMP[name](decoded) - item.t_host * 1e6 for item, decoded in good)
    spread = max(offsets.values()) - min(offsets.values())
    if spread > ONE_CLOCK_TOLERANCE_US:
        # One timer per sensor is the mistake this clause exists to forbid: each
        # stream is perfectly self-consistent and no two agree, so cross-channel
        # alignment -- the reason this protocol carries all three -- silently
        # produces a wrong answer instead of no answer.
        raise Fail(
            f"the streams differ by {spread / 1e6:.1f} s in their relationship "
            f"to this host's clock, so they are not timestamped against one "
            f"device clock: "
            + ", ".join(f"{n} {v / 1e6:+.3f}s" for n, v in sorted(offsets.items())),
            offsets_us={n: round(v) for n, v in offsets.items()})
    raise Observe(
        f"the {len(present)} streams agree to within {spread / 1000:.1f} ms",
        spread_us=round(spread))


@check(id="loss.dropped", section="8.3", phase="streams", severity="OBSERVE",
       title="Items the device accepted and then discarded")
async def loss_dropped(s):
    totals, saturated = {}, []
    for name in s.STREAMS:
        if not s.has(name) or not len(s.streams[name]):
            continue
        good, _ = _decoded(s, name)
        header = (lambda d: d) if name == "gps" else (lambda d: d["header"])
        values = [header(decoded)["dropped"] for _, decoded in good]
        totals[name] = sum(values)
        if any(v == 0xFFFF for v in values):
            saturated.append(name)
    if not totals:
        raise Skip("no stream produced a notification")
    if saturated:
        raise Observe(
            f"dropped saturated at 65535 on {', '.join(saturated)}; that reads "
            f"'at least 65535', never exactly that many",
            dropped=totals, saturated=saturated)
    if not any(totals.values()):
        raise Observe("no stream reported a discard", dropped=totals)
    raise Observe(
        "; ".join(f"{n}: {v}" for n, v in totals.items() if v)
        + ". These are items the device accepted and then discarded, not "
          "frames it filtered as instructed",
        dropped=totals)


@check(id="stream.rates", section="4", phase="streams", severity="OBSERVE",
       title="Observed notification rates against the declared ones")
async def stream_rates(s):
    lines, measured = [], {}
    for name in s.STREAMS:
        log = s.streams[name]
        if not s.has(name) or len(log) < 2 or log.duration_s <= 0:
            continue
        rate = (len(log) - 1) / log.duration_s
        measured[name] = round(rate, 2)
        lines.append(f"{name} {rate:.1f} notifications/s")
    if not lines:
        raise Skip("no stream produced enough notifications to measure")
    # Deliberately not a pass or fail. A host's scheduler and the central's
    # connection interval both sit between the device and this number, so a
    # shortfall here is a reason to look, not a verdict.
    raise Observe("; ".join(lines) + " (host-observed; the connection interval "
                  "and this machine's scheduler are both in the measurement)",
                  rates=measured)


# ---------------------------------------------------------------------------
# SPEC.md §6 -- what a subscription means
# ---------------------------------------------------------------------------

@check(id="can.matches_subscription", section="9.1", phase="streams",
       severity="MUST", requires=("control", "can"),
       title="Every forwarded frame matches an installed subscription")
async def can_matches_subscription(s):
    installed = s.state.get("installed")
    if not installed:
        raise Skip("no subscription was installed")
    if s.state.get("can_catch_all"):
        raise Skip("the harness subscribed with a mask of zero, which matches "
                   "every frame, so there is nothing this can exclude")
    _log(s, "can")
    good, _ = _decoded(s, "can")
    wanted = set(installed)
    for item, batch in good:
        for record in batch["records"]:
            if record["id"] not in wanted:
                raise Fail(
                    f"identifier 0x{record['id']:x} was forwarded and no "
                    f"subscription covers it (installed: "
                    f"{', '.join(hex(i) for i in sorted(wanted))})",
                    payload=item.payload.hex())


@check(id="can.forwarded_once", section="9.2", phase="streams", severity="MUST",
       requires=("control", "can", "masked_subscriptions"),
       title="A frame matching several subscriptions is forwarded at most once")
async def can_forwarded_once(s):
    c = s.control
    if c is None:
        raise Skip("no control plane")
    good, _ = _decoded(s, "can")
    seen_ids = {r["id"] for _, batch in good for r in batch["records"]}
    if not seen_ids:
        raise Skip("no CAN frame was observed, so there is nothing to overlap")
    target = sorted(seen_ids)[0]

    # Deliberately LESS specific than an exact subscription, and different from
    # the catch-all: clearing one arbitration bit still matches `target` and
    # cannot equal anything already installed. An exact mask here would be
    # identical to what --can-id installs, and SPEC.md §9.1 requires a device to
    # update that in place -- so no second subscription would exist, no frame
    # could be forwarded twice, and this would report a pass having never
    # created the condition it tests.
    overlap_mask = refdec.MASK_EXACT & ~0x1
    if s.state.get("installed", {}).get(target) == overlap_mask:
        raise Skip("the overlapping id and mask are already installed, so no "
                   "second subscription would exist")
    overlap = await c.subscribe_can(target, mask=overlap_mask)
    if not overlap.ok:
        raise Skip(f"could not install an overlapping subscription: "
                   f"{overlap.status_name}")
    mark = len(s.streams["can"])
    await asyncio.sleep(2.0)

    duplicates = []
    # ONE set for the whole window, not one per notification: a frame
    # forwarded twice keeps its bus-arrival timestamp, and nothing obliges
    # the second copy to share a batch with the first. A per-notification
    # set passed a device that split the copies across two batches.
    seen = set()
    for item in s.streams["can"].items[mark:]:
        try:
            batch = refdec.decode("can_batch", item.payload)
        except refdec.Reject:
            continue
        for record in batch["records"]:
            key = (record["id"], record["t_device_us"])
            if key in seen:
                duplicates.append((item, key))
            seen.add(key)
    if duplicates:
        item, (can_id, t) = duplicates[0]
        # Duplicate frames on one bus-arrival timestamp are indistinguishable
        # from a bus fault, which is why §9.2 makes forwarding once a MUST
        # rather than an optimisation.
        raise Fail(
            f"identifier 0x{can_id:x} appears twice at device time {t} with two "
            f"subscriptions matching it. A frame MUST be forwarded at most "
            f"once, governed by the most specific mask and then the earliest "
            f"installed", payload=item.payload.hex())


# ---------------------------------------------------------------------------
# SPEC.md §9.2 -- which subscription governs
# ---------------------------------------------------------------------------

async def _count_frames(s, target, seconds):
    """Frames carrying `target` arriving in the next `seconds`."""
    mark = len(s.streams["can"])
    await asyncio.sleep(seconds)
    count = 0
    for item in s.streams["can"].items[mark:]:
        try:
            batch = refdec.decode("can_batch", item.payload)
        except refdec.Reject:
            continue
        count += sum(1 for r in batch["records"] if r["id"] == target)
    return count


async def _restore_observation_table(s):
    """Put the table back the way can.subscribe_for_observation left it.

    These checks reprogram the table wholesale, and the reconnect phase
    probes for exactly what `installed` says this connection holds -- so the
    device and that record must agree again before this check ends.
    """
    c = s.control
    await c.request(refdec.OPCODE["CAN_RESET"])
    for can_id, mask in (s.state.get("installed") or {}).items():
        if mask == refdec.MASK_EXACT:
            await c.subscribe_can(can_id)
        else:
            await c.subscribe_can(can_id, mask=mask)


#: ISO 15765-4's 11-bit diagnostic response range, used only when no probe
#: result is available to say what this car actually answers on.
_DIAGNOSTIC_IDS_11BIT = range(0x7E8, 0x7F0)


def _diagnostic_ids(s):
    """Identifiers whose traffic exists only while a poll set is installed.

    Taken from the probe when there is one (SPEC.md 15.2 reports the response
    identifiers this car uses, whichever addressing it answered on) and from
    the 11-bit range otherwise. A hardcoded range covered 11-bit addressing
    only, so on a 29-bit car -- `0x18DAF1xx`, which SPEC.md 15.2 supports and
    the peripheral's own selftest exercises -- the exclusion missed and the
    defect it exists to prevent came back.

    Masked to bits 0-28 because that is what a decoded `can_record` carries;
    the probe's ids hold the format bit in bit 29 and comparing across that
    split never matches.
    """
    probe = s.state.get("obd_probe")
    if probe is None:
        return set(_DIAGNOSTIC_IDS_11BIT)
    return {e["id"] & 0x1FFFFFFF for e in probe.get("ecus", ())} \
        or set(_DIAGNOSTIC_IDS_11BIT)


def _busiest_id(s):
    good, _ = _decoded(s, "can")
    counts = {}
    for _, batch in good:
        for r in batch["records"]:
            counts[r["id"]] = counts.get(r["id"], 0) + 1
    # Every governing check below opens with CAN_RESET, which clears the poll
    # set along with the table (SPEC.md 15.7). Choosing a diagnostic response
    # identifier as the target measured a baseline of zero and skipped,
    # reporting "too little traffic" about an identifier the check had just
    # silenced itself. Broadcast traffic does not stop when the table does.
    diagnostic = _diagnostic_ids(s)
    broadcast = {cid: n for cid, n in counts.items() if cid not in diagnostic}
    if not broadcast:
        raise Skip("no broadcast CAN frame was observed, so there is no "
                   "identifier that survives the CAN_RESET this check opens "
                   "with (SPEC.md 15.7)")
    return max(broadcast, key=broadcast.get)


@check(id="can.most_specific_governs", section="9.2", phase="streams",
       severity="MUST", requires=("control", "can", "masked_subscriptions"),
       title="Of two overlapping subscriptions, the most specific mask governs")
async def can_most_specific_governs(s):
    c = s.control
    if c is None:
        raise Skip("no control plane")
    target = _busiest_id(s)
    try:
        # Baseline: the target alone, exact mask, every_frame.
        await c.request(refdec.OPCODE["CAN_RESET"])
        base_install = await c.subscribe_can(target)
        if not base_install.ok:
            raise Skip(f"could not install a baseline subscription: "
                       f"{base_install.status_name}")
        baseline = await _count_frames(s, target, 1.0)
        if baseline < 4:
            raise Skip(f"identifier 0x{target:x} produced only {baseline} "
                       f"frame(s) in a second; too little traffic to tell "
                       f"one forwarding mode from another")
        # The condition §9.2 decides: the same frame matched by an exact
        # every_frame subscription and a broader periodic one. The mode
        # difference is what makes the wrong answer VISIBLE -- with both
        # subscriptions in the same mode, a device forwarding under the
        # wrong governor produces the same stream as one that is right.
        broad = await c.subscribe_can(target, mask=refdec.MASK_EXACT & ~0x1,
                                      mode=1, arg=60_000)
        if not broad.ok:
            raise Skip(f"could not install the broad periodic subscription: "
                       f"{broad.status_name}")
        governed = await _count_frames(s, target, 1.0)
        if governed < max(2, baseline // 3):
            raise Fail(
                f"0x{target:x} arrived {governed} time(s) in a window that "
                f"carried {baseline} under the exact subscription alone. The "
                f"broad periodic subscription is governing the frame; §9.2 "
                f"gives it to the most specific mask, which is the exact "
                f"every-frame one")
    finally:
        await _restore_observation_table(s)
    s.state["can_rate_baseline"] = baseline


@check(id="can.earliest_installed_governs", section="9.2", phase="streams",
       severity="MUST", requires=("control", "can", "masked_subscriptions"),
       title="Equally specific overlapping subscriptions tie-break to the earliest")
async def can_earliest_installed_governs(s):
    c = s.control
    if c is None:
        raise Skip("no control plane")
    target = _busiest_id(s)
    baseline = s.state.get("can_rate_baseline")
    if baseline is None:
        raise Skip("no traffic baseline to compare against")
    # Two masks of EQUAL specificity -- 29 bits each, differing only in which
    # arbitration bit they ignore -- both matching the target. Only install
    # order separates them, and the modes differ so the answer is visible.
    mask_a = refdec.MASK_EXACT & ~0x1
    mask_b = refdec.MASK_EXACT & ~0x2
    try:
        await c.request(refdec.OPCODE["CAN_RESET"])
        await c.subscribe_can(target, mask=mask_a)                    # earliest
        await c.subscribe_can(target, mask=mask_b, mode=1, arg=60_000)
        forwarded = await _count_frames(s, target, 1.0)
        if forwarded < max(2, baseline // 3):
            raise Fail(
                f"0x{target:x} arrived {forwarded} time(s) against a baseline "
                f"of {baseline}: the periodic subscription installed SECOND "
                f"is governing a frame two equally specific masks match. "
                f"§9.2 tie-breaks to the earliest installed")
        # The same pair, installed in the other order, so a device that got
        # the first half right by accident -- insertion order, say -- has to
        # get this half right on purpose.
        await c.request(refdec.OPCODE["CAN_RESET"])
        await c.subscribe_can(target, mask=mask_b, mode=1, arg=60_000)  # earliest
        await c.subscribe_can(target, mask=mask_a)
        forwarded = await _count_frames(s, target, 1.0)
        ceiling = max(2, baseline // 8)
        if forwarded > ceiling:
            raise Fail(
                f"0x{target:x} arrived {forwarded} time(s) where the periodic "
                f"subscription installed FIRST allows at most ~{ceiling}: the "
                f"every-frame subscription installed second is governing. "
                f"§9.2 tie-breaks equally specific masks to the earliest "
                f"installed, whichever mode each carries")
    finally:
        await _restore_observation_table(s)


# ---------------------------------------------------------------------------
# SPEC.md §6.8 -- what a subscription's schedule is, and what disturbs it
# ---------------------------------------------------------------------------

#: A `periodic` interval longer than any window here, so exactly one frame is
#: owed for a whole check: §6.8's first one. Every scheduling check below is
#: built out of subscriptions that are silent after it, which is what makes a
#: single arriving frame mean something.
SLOW_MS = 60_000

#: Slow enough that most of a busy identifier's frames are declined by the
#: mode, fast enough that batches keep arriving to carry `dropped` out.
RATIONED_MS = 200

#: Long enough for anything a device accepted before a control request to reach
#: the host: `dt` spans at most 655.35 ms (§6.1), so a conforming batch cannot
#: hold a frame longer than that.
SETTLE_S = 0.7


def _decodes(item):
    """Whether a notification is a batch this harness can read at all."""
    try:
        refdec.decode("can_batch", item.payload)
    except refdec.Reject:
        return False
    return True


def _mark_can(s, target):
    """Where the CAN log ends, and the newest arrival of `target` in it.

    Both at once, and with no `await` between them, because they are two halves
    of one boundary: a frame is new if it arrived in a notification after the
    mark AND carries a bus-arrival time later than anything already seen. The
    index alone is not enough -- a batch is flushed on the device's own
    schedule (§6.2), so frames accepted before a control request are delivered
    after it, and a check counting by arrival window reads those as new. The
    timestamp alone is not enough either: it is the device's clock, and nothing
    here can name a moment on it except by having seen one.
    """
    items = s.streams["can"].items
    mark, latest = len(items), 0
    for item in items:
        try:
            batch = refdec.decode("can_batch", item.payload)
        except refdec.Reject:
            continue
        for record in batch["records"]:
            if record["id"] == target:
                latest = max(latest, record["t_device_us"])
    return mark, latest


def _count_after(s, marker, target, extended=None):
    """Frames of `target` the host holds that arrived after `marker`."""
    mark, since = marker
    count = 0
    for item in s.streams["can"].items[mark:]:
        try:
            batch = refdec.decode("can_batch", item.payload)
        except refdec.Reject:
            continue
        for record in batch["records"]:
            if record["id"] != target or record["t_device_us"] <= since:
                continue
            if extended is not None and record["extended"] != extended:
                continue
            count += 1
    return count


async def _frames_after(s, marker, target, seconds, extended=None):
    """Frames of `target` that arrived on the bus after `marker` was taken."""
    await asyncio.sleep(seconds)
    return _count_after(s, marker, target, extended=extended)


async def _wait_for_frame(s, marker, target, timeout, step=0.2):
    """Wait until a frame of `target` newer than `marker` has been delivered.

    A boundary a later measurement can trust has to be an observed frame and
    not a duration: nothing in §6.2 bounds how long a device may hold a batch
    carrying one record, so a fixed sleep marks the boundary before the frame
    it is waiting for and the next window counts that frame as something else.
    """
    waited = 0.0
    while waited < timeout:
        await asyncio.sleep(step)
        waited += step
        if _count_after(s, marker, target):
            return True
    return False


async def _quiet_table(s, target):
    """An empty table, and everything the device already accepted delivered.

    Returns the marker the checks below measure from. Without the drain, a
    frame accepted under the observation subscription arrives after the reset
    and is counted as the first frame of whatever is installed next.
    """
    await s.control.request(refdec.OPCODE["CAN_RESET"])
    await asyncio.sleep(SETTLE_S)
    return _mark_can(s, target)


@check(id="can.periodic_first_then_rations", section="6.8", phase="streams",
       severity="MUST", requires=("control", "can"),
       title="A periodic subscription forwards the first frame, then rations")
async def can_periodic_first_then_rations(s):
    """§6.8 — the first matching frame is forwarded in every mode, so a client
    that installs a subscription and waits for a value to display never waits
    for a second frame. Then the interval it asked for applies. The two halves
    are one contract and a device can fail either without failing the other:
    one leaves a display blank for a minute, the other ignores the rate limit
    entirely."""
    c = s.control
    if c is None:
        raise Skip("no control plane")
    target = _busiest_id(s)
    try:
        # The identifier came from the observation window, which is already
        # seconds old, and a signal can stop between the two: a gear-dependent
        # frame, an intermittent ECU, an ignition switched off. Establish that
        # it is still arriving under a subscription that owes every frame, so
        # that silence under the periodic one below means the device held the
        # first frame back rather than that there was none.
        marker = await _quiet_table(s, target)
        alive = await c.subscribe_can(target)
        if not alive.ok:
            raise Skip(f"could not install an every_frame subscription: "
                       f"{alive.status_name}")
        if await _frames_after(s, marker, target, 1.0) == 0:
            raise Skip(f"0x{target:x} was busiest during the observation "
                       f"window and is carrying nothing now; there is no first "
                       f"frame for a periodic subscription to owe")
        # A fresh install rather than a mode change on the live one, so this
        # rests on §6.8's first frame alone and not on the re-arming rule.
        marker = await _quiet_table(s, target)
        installed = await c.subscribe_can(target, mode=1, arg=SLOW_MS)
        if not installed.ok:
            raise Skip(f"could not install a periodic subscription: "
                       f"{installed.status_name}")
        forwarded = await _frames_after(s, marker, target, 1.5)
        if forwarded == 0:
            raise Fail(
                f"no frame of 0x{target:x} arrived in 1.5 s under a "
                f"{SLOW_MS} ms periodic subscription on a bus carrying it. "
                f"§6.8 forwards the first matching frame in every mode, so a "
                f"client installing a subscription to display a value does not "
                f"wait out the interval to see one")
        if forwarded > 2:
            raise Fail(
                f"{forwarded} frames of 0x{target:x} arrived in 1.5 s under a "
                f"{SLOW_MS} ms periodic subscription. §6.8 owes the first "
                f"matching frame and then at most one per `arg` ms; this is "
                f"the interval not being applied at all")
    finally:
        await _restore_observation_table(s)


@check(id="can.identical_reinstall_costs_nothing", section="9.4",
       phase="streams", severity="MUST", requires=("control", "can"),
       title="A byte-identical re-install forwards no frame of its own")
async def can_identical_reinstall_costs_nothing(s):
    """§9.4 makes every request here safe to retry, and §6.8 promises a first
    frame on install. A retry that changes neither `mode` nor `arg` is where
    the two meet, and the difference is a frame inside the client's own rate
    limit with nothing on the wire to explain it: a lost response and a
    delivered one are identical at the client, so the device has to make the
    two cases identical too. §6.8 does — an unchanged re-install changes
    nothing at all, the schedule included."""
    c = s.control
    if c is None:
        raise Skip("no control plane")
    target = _busiest_id(s)
    try:
        marker = await _quiet_table(s, target)
        installed = await c.subscribe_can(target, mode=1, arg=SLOW_MS)
        if not installed.ok:
            raise Skip(f"could not install a periodic subscription: "
                       f"{installed.status_name}")
        spent = await _frames_after(s, marker, target, 1.0)
        if spent == 0:
            raise Skip("the subscription forwarded no first frame, so there is "
                       "no spent schedule for a retry to disturb "
                       "(can.periodic_first_then_rations owns that rule)")
        marker = _mark_can(s, target)
        retry = await c.subscribe_can(target, mode=1, arg=SLOW_MS)
        if not retry.ok:
            raise Fail(f"re-installing an identical subscription was answered "
                       f"{retry.status_name}", response=retry.raw.hex())
        extra = await _frames_after(s, marker, target, 1.5)
        if extra:
            raise Fail(
                f"{extra} frame(s) of 0x{target:x} arrived after a re-install "
                f"that changed neither mode nor arg, a second into a "
                f"{SLOW_MS} ms interval. §9.4 tells a client to retry a request "
                f"whose response it did not receive; §6.8 makes that retry "
                f"free, and this device charged a frame for it")
    finally:
        await _restore_observation_table(s)


@check(id="can.displaced_schedule_survives", section="6.8", phase="streams",
       severity="MUST", requires=("control", "can", "masked_subscriptions"),
       title="A subscription displaced from governance keeps its rate limit")
async def can_displaced_schedule_survives(s):
    """§6.8 keys scheduling state per (subscription, identifier), and §9.2
    decides which subscription forwards a frame rather than which ones still
    apply. A device keying that state by the identifier alone hands it to
    whichever subscription matched last: the broad one's interval is destroyed
    on the way in, and removing the narrow one lets it forward immediately.
    Reported from a vehicle as a once-a-minute subscription delivering three
    frames in twenty milliseconds, every one of them well-formed.

    Both subscriptions here are `periodic` and slow, so the only frame either
    owes is its own first one. Anything arriving after the narrow one is
    removed is the broad one's schedule having been reset behind the client's
    back.
    """
    c = s.control
    if c is None:
        raise Skip("no control plane")
    target = _busiest_id(s)
    broad_mask = refdec.MASK_EXACT & ~0x1
    try:
        marker = await _quiet_table(s, target)
        broad = await c.subscribe_can(target, mask=broad_mask, mode=1,
                                      arg=SLOW_MS)
        if not broad.ok:
            raise Skip(f"could not install the broad periodic subscription: "
                       f"{broad.status_name}")
        if await _frames_after(s, marker, target, 1.0) == 0:
            raise Skip("the broad subscription forwarded no first frame, so "
                       "there is no spent schedule to displace "
                       "(can.periodic_first_then_rations owns that rule)")
        marker = _mark_can(s, target)
        exact = await c.subscribe_can(target, mode=1, arg=SLOW_MS)
        if not exact.ok:
            raise Skip(f"could not install the displacing subscription: "
                       f"{exact.status_name}")
        # Its own first frame is owed, and the window below has to open AFTER
        # it: waited for rather than slept through, because a device may hold
        # a one-record batch for as long as it likes and a frame arriving
        # after a fixed mark would be counted as the broad subscription
        # forwarding.
        if not await _wait_for_frame(s, marker, target, 5.0):
            raise Skip("the displacing subscription forwarded no first frame "
                       "within 5 s, so nothing marks the boundary this check "
                       "measures from")
        marker = _mark_can(s, target)
        removed = await c.unsubscribe_can(target)
        if not removed.ok:
            raise Skip(f"could not remove the displacing subscription: "
                       f"{removed.status_name}")
        after = await _frames_after(s, marker, target, 1.5)
        if after:
            raise Fail(
                f"{after} frame(s) of 0x{target:x} arrived as soon as the more "
                f"specific subscription was removed. The broad {SLOW_MS} ms "
                f"subscription was installed throughout and its interval had "
                f"not elapsed: §6.8 keeps a displaced subscription's schedule, "
                f"and §9.2 decides which subscription forwards a frame, not "
                f"which ones have stopped applying")
    finally:
        await _restore_observation_table(s)


@check(id="can.dropped_excludes_declined", section="6.3", phase="streams",
       severity="MUST", requires=("control", "can"),
       title="dropped counts accepted frames, not ones the mode declined")
async def can_dropped_excludes_declined(s):
    """§6.3 — `dropped` counts frames the device accepted and then discarded.
    A frame no subscription matched was never accepted, and neither was one the
    governing subscription's mode did not select (§6.8): counting either turns
    a working rate limit into a permanent loss report, and a client that
    surfaces `dropped` to its user tells them their bus is broken because they
    asked for one frame a minute.

    The interval is short enough that some frames are still forwarded, because
    `dropped` rides on a batch and a subscription that forwards nothing sends
    none: a device counting every declined frame would accumulate the evidence
    and never deliver it. The window opens a second after the install, so a
    count owed from the phase before this one — which rides on the next batch
    with content (§6.2) — is not read as this subscription's.
    """
    c = s.control
    if c is None:
        raise Skip("no control plane")
    target = _busiest_id(s)
    shedding = 1 << refdec.bit("can_flags", "shedding")
    try:
        await _quiet_table(s, target)
        pre = len(s.streams["can"].items)
        installed = await c.subscribe_can(target, mode=1, arg=RATIONED_MS)
        if not installed.ok:
            raise Skip(f"could not install a periodic subscription: "
                       f"{installed.status_name}")
        await asyncio.sleep(1.5)
        # A count owed from before this check rides on the next batch with
        # content (§6.2), and the harness's own catch-all is exactly what
        # makes a device shed. If no batch has arrived since the install, that
        # count is still owed and the window below would read it as this
        # subscription's.
        if not any(_decodes(item) for item in s.streams["can"].items[pre:]):
            raise Skip("no batch arrived between the install and the window, "
                       "so a `dropped` count owed from before this check "
                       "could still be riding on the next one")
        mark = len(s.streams["can"].items)
        await asyncio.sleep(2.0)
        counted, offender = 0, None
        for item in s.streams["can"].items[mark:]:
            try:
                batch = refdec.decode("can_batch", item.payload)
            except refdec.Reject:
                continue
            if batch["header"]["flags"] & shedding:
                raise Skip("the device reported it was shedding, which §6.3 "
                           "does count in dropped; nothing here can separate "
                           "that from the frames the mode declined")
            if batch["header"]["dropped"]:
                counted += batch["header"]["dropped"]
                offender = offender or item
        if counted:
            raise Fail(
                f"dropped counted {counted} frame(s) while one "
                f"{RATIONED_MS} ms periodic subscription was installed and not "
                f"shedding. Every frame in that window was forwarded, "
                f"declined by the mode, or matched by nothing at all, and "
                f"§6.3 counts none of the three: the two it excludes were "
                f"never accepted, and the first was not discarded",
                payload=offender.payload.hex())
    finally:
        await _restore_observation_table(s)


@check(id="can.format_bit_is_identity", section="9.1", phase="streams",
       severity="MUST", requires=("control", "can"),
       title="A subscription naming the other frame format matches nothing")
async def can_format_bit_is_identity(s):
    """§9.1 — standard 0x1A0 and extended 0x1A0 are two different frames from
    possibly two different ECUs, so bit 29 is matched like any other identity
    bit. The defect this catches is an integration one and easy to make: a
    controller that keeps the format in a flags word rather than in the
    identifier (Zephyr's `can_frame.flags` among them) hands the device an id
    with bit 29 clear for every frame on the bus. Extended subscriptions then
    never match anything, and a standard subscription on the same number
    quietly delivers another ECU's traffic — a correct-looking identifier with
    the wrong payload behind it."""
    c = s.control
    if c is None:
        raise Skip("no control plane")
    # _busiest_id, and not the busiest (id, format) pair: the pair would
    # choose a diagnostic response identifier on an OBD device, whose traffic
    # this check's own CAN_RESET stops (SPEC.md §15.7). Nothing then arrives,
    # and the check passes having never created the condition it tests.
    target = _busiest_id(s)
    good, _ = _decoded(s, "can")
    formats = {}
    for _, batch in good:
        for record in batch["records"]:
            if record["id"] == target:
                formats[record["extended"]] = formats.get(record["extended"], 0) + 1
    if not formats:
        raise Skip(f"no frame of 0x{target:x} was decoded, so its format is "
                   f"not known")
    extended = max(formats, key=formats.get)
    # The same number under the other format is a different frame, which this
    # bus may or may not also carry. Either way the frames observed above MUST
    # NOT arrive under a subscription naming it.
    other = target if extended else target | (1 << 29)
    try:
        marker = await _quiet_table(s, target)
        installed = await c.subscribe_can(other)
        if not installed.ok:
            raise Skip(f"could not install the other-format subscription: "
                       f"{installed.status_name}")
        wrong = await _frames_after(s, marker, target, 2.0, extended=extended)
        if wrong:
            named = "extended" if extended else "standard"
            asked = "standard" if extended else "extended"
            raise Fail(
                f"{wrong} {named} frame(s) of 0x{target:x} arrived under a "
                f"subscription naming the {asked} form of that identifier. "
                f"§9.1 matches over bits 0-29 and bit 29 is the format bit: "
                f"these are two different frames from possibly two different "
                f"senders, and a client reading one as the other decodes the "
                f"wrong payload behind a right-looking identifier")
    finally:
        await _restore_observation_table(s)
