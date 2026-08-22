#!/usr/bin/env python3
"""Drive the synthetic device and decode everything it emits.

This is the loop worth having: the peripheral is validated by the same decoder
that validates the conformance corpus. A device that emits something the
reference decoder rejects is not a conforming device, and nothing about that
depends on a radio — so this runs in CI on a machine with no Bluetooth adapter,
which is where the interesting bugs are anyway.

Beyond "does it decode", it asserts the properties a client actually relies on
and which no single-payload vector can express: that the three roles share one
monotonic clock, that timestamps advance across batches, that absence is
reported rather than zeroed, and that the control plane changes device
behaviour rather than merely answering.

Usage:
  python3 reference/peripheral/selftest.py
"""
import pathlib
import struct
import sys

# Leave no .pyc behind. Editing the device and re-running is the whole workflow
# here -- including seeding a deliberate fault to check this file can detect it
# -- and a same-length edit within the same second leaves Python's mtime+size
# cache check satisfied, so a stale module gets imported and the result is
# silently about code that is no longer on disk. That cost an hour once.
sys.dont_write_bytecode = True

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "reference" / "python"))
sys.path.insert(0, str(HERE))

import vtp1  # noqa: E402
import vtp_device as dev  # noqa: E402
import display as disp  # noqa: E402  (pure formatting; no GUI import at module level)
import serve  # noqa: E402  (bless is loaded lazily, so this needs no radio)

FAILURES = []


def check(condition, message):
    if not condition:
        FAILURES.append(message)


def decode(characteristic, payload):
    """Decode as the reference decoder would, or record a failure."""
    fn = {"gps": vtp1.decode_gps_fix, "can": vtp1.decode_can_batch,
          "imu": vtp1.decode_imu_batch}[characteristic]
    try:
        return fn(payload)
    except vtp1.Reject as exc:
        FAILURES.append(
            f"{characteristic}: the device emitted {len(payload)} bytes the "
            f"reference decoder rejects ({exc})")
        return None


def run(device, clock, seconds, step_us=5_000):
    """Advance the injected clock and collect everything the device emits.

    Every payload is stamped and committed, which models a transport that
    delivers all of them. SPEC.md §8.2 makes seq a fact about delivery, so a
    payload straight out of poll() carries only a placeholder and a device-only
    harness has to supply the other half. transport_selftest.py exercises the
    cases where delivery FAILS, which is where the number stops being trivial.
    """
    out = []
    for _ in range(int(seconds * 1_000_000 // step_us)):
        clock[0] += step_us
        for characteristic, payload in device.poll():
            out.append((characteristic,
                        device.stamp_seq(characteristic, payload)))
            device.commit_seq(characteristic)
    return out


def main():
    clock = [0]
    device = dev.VtpDevice(now_us=lambda: clock[0], mtu=247,
                           gps_hz=10, imu_hz=100)

    # ---- Info -----------------------------------------------------------
    info = vtp1.decode_info(device.info())
    check(info["protocol_major"] == 1, "info: protocol_major must be 1")
    check(info["max_notify_bytes"] == 244,
          f"info: max_notify_bytes {info['max_notify_bytes']} should be MTU-3")
    check(info["gps_rate_hz"] <= info["gps_max_rate_hz"],
          "info: current GPS rate exceeds the declared ceiling")
    check(info["imu_rate_hz"] <= info["imu_max_rate_hz"],
          "info: current IMU rate exceeds the declared ceiling")

    # ---- Control: nothing streams on CAN until asked --------------------
    quiet = run(device, clock, 1.0)
    check(not [c for c, _ in quiet if c == "can"],
          "CAN notified before any subscription was installed")

    resp = device.handle_control(bytes([dev.CAN_SUBSCRIBE, 0x42])
                                 + struct.pack("<IBH", 0x0C0,
                                               dev.SUB_EVERY_FRAME, 0))
    check(resp[:3] == bytes([dev.CAN_SUBSCRIBE, 0x42, dev.ST_OK]),
          "CAN_SUBSCRIBE was not accepted")
    check(resp[1] == 0x42, "control: the response MUST echo the request tag")
    check(len(resp) == 5, "CAN_SUBSCRIBE MUST return a handle")
    (handle,) = struct.unpack("<H", resp[3:])

    # SPEC.md §9.2 — the same (id, mask) updates in place and keeps its handle.
    again = device.handle_control(bytes([dev.CAN_SUBSCRIBE, 0x43])
                                  + struct.pack("<IBH", 0x0C0,
                                                dev.SUB_PERIODIC, 50))
    check(struct.unpack("<H", again[3:])[0] == handle,
          "re-subscribing the same id and mask MUST return the existing handle "
          "rather than consuming a second slot")
    page = vtp1.decode_can_list(
        device.handle_control(bytes([dev.CAN_LIST, 9]) + struct.pack("<H", 0))[3:])
    check(page["page"]["total"] == 1,
          f"re-subscribing consumed a slot: table holds {page['page']['total']}")
    check(page["entries"][0]["mode"] == dev.SUB_PERIODIC,
          "re-subscribing MUST update mode in place")

    # Put it back to every_frame for the streaming checks below.
    device.handle_control(bytes([dev.CAN_SUBSCRIBE, 0x44])
                          + struct.pack("<IBH", 0x0C0, dev.SUB_EVERY_FRAME, 0))

    # ---- Stream ---------------------------------------------------------
    emitted = run(device, clock, 6.0)
    check(len(emitted) > 0, "the device emitted nothing at all")

    gps, can, imu = [], [], []
    for characteristic, payload in emitted:
        check(len(payload) <= device.notify_bytes,
              f"{characteristic}: {len(payload)} bytes exceeds the "
              f"{device.notify_bytes}-byte notification budget")
        decoded = decode(characteristic, payload)
        if decoded is None:
            continue
        {"gps": gps, "can": can, "imu": imu}[characteristic].append(decoded)

    check(len(gps) >= 50, f"expected ~60 fixes at 10 Hz over 6 s, got {len(gps)}")
    check(len(imu) > 0, "no IMU batches were emitted")
    check(len(can) > 0, "no CAN batches were emitted after subscribing")

    # ---- One clock, shared (SPEC.md §8) ---------------------------------
    gps_t = [f["t_device"] for f in gps]
    check(gps_t == sorted(gps_t), "GPS t_device went backwards")
    check(len(set(gps_t)) == len(gps_t), "two GPS fixes share a timestamp")

    imu_t = [s["t_device_us"] for b in imu for s in b["samples"]]
    check(imu_t == sorted(imu_t), "IMU sample timestamps went backwards")

    can_t = [r["t_device_us"] for b in can for r in b["records"]]
    check(can_t == sorted(can_t), "CAN frame timestamps went backwards")

    # The point of the shared clock: the channels interleave on one timeline.
    if gps_t and imu_t:
        overlap = min(gps_t[-1], imu_t[-1]) - max(gps_t[0], imu_t[0])
        check(overlap > 0,
              "GPS and IMU timestamps do not overlap, so they cannot be "
              "aligned — the whole point of the single clock")

    # ---- Sequence numbers -----------------------------------------------
    seqs = [f["seq"] for f in gps]
    check(all((b - a) % 0x10000 == 1 for a, b in zip(seqs, seqs[1:])),
          "GPS seq is not incrementing by one per fix")

    # ---- Absence is reported, not zeroed (SPEC.md §1.1) -----------------
    for f in gps:
        check("alt_ellipsoid" in f["absent"],
              "gps: alt_ellipsoid has no validity bit set, so it MUST be "
              "reported absent rather than as 0 mm")
        check(f["lat"] != 0 and f["lon"] != 0,
              "gps: position is valid but reads zero")
    for b in imu:
        for s in b["samples"]:
            check(s["absent"] == [],
                  "imu: both sensor groups are present, so nothing is absent")

    # ---- Batching respects the wire rules -------------------------------
    for b in imu:
        check(b["header"]["period"] == 10_000,
              f"imu: period {b['header']['period']} us should be 100 Hz")
        ts = [s["t_device_us"] for s in b["samples"]]
        gaps = {b_ - a for a, b_ in zip(ts, ts[1:])}
        check(gaps <= {b["header"]["period"]},
              "imu: samples in a batch are not evenly spaced by period")
    for b in can:
        check(all(r["dt"] <= 0xFFFF for r in b["records"]),
              "can: dt exceeded the 655.35 ms batch window")
        check(all(r["id"] == 0x0C0 for r in b["records"]),
              "can: a frame appeared for an id that was never subscribed")

    # ---- Control changes behaviour, not just the reply ------------------
    # SPEC.md §9.5 — the table is reported exactly as installed.
    listing = device.handle_control(bytes([dev.CAN_LIST, 8])
                                    + struct.pack("<H", 0))
    check(listing[2] == dev.ST_OK, "CAN_LIST was refused")
    table = vtp1.decode_can_list(listing[3:])
    check(table["page"]["total"] == 1,
          f"table should hold one subscription, holds {table['page']['total']}")
    check(table["entries"][0]["handle"] == handle,
          "CAN_LIST reports a different handle than the install returned")
    check(table["entries"][0]["mask"] == 0x3FFFFFFF,
          "CAN_SUBSCRIBE MUST be recorded as a mask of 0x3FFFFFFF")

    # ---- Subscription identity includes the frame format ----------------
    # SPEC.md §9.2 — standard 0x0C0 and extended 0x0C0 are different frames, so
    # an exact subscription to one MUST NOT match the other. A mask that
    # stopped at 0x1FFFFFFF could not tell them apart, and a client would have
    # decoded the wrong payload behind a correct-looking identifier.
    check(device._governing(0x0C0)[0] == handle,
          "an exact subscription to standard 0x0C0 no longer matches it")
    check(device._governing(0x0C0 | (1 << 29))[0] is None,
          "an exact subscription to standard 0x0C0 also matches extended "
          "0x0C0; the format bit is part of a frame's identity")

    # A client that genuinely wants both formats clears bit 29 in its mask, and
    # the device can honour that because it can see it was asked.
    both = device.handle_control(
        bytes([dev.CAN_SUBSCRIBE_MASK, 30])
        + struct.pack("<IIBH", 0x1A0, 0x1FFFFFFF, dev.SUB_EVERY_FRAME, 0))
    check(both[2] == dev.ST_OK, "a both-formats subscription was refused")
    h_both = struct.unpack("<H", both[3:])[0]
    check(device._governing(0x1A0)[0] == h_both
          and device._governing(0x1A0 | (1 << 29))[0] == h_both,
          "a mask clearing bit 29 MUST match both formats of the identifier")
    device.handle_control(bytes([dev.CAN_UNSUBSCRIBE, 31])
                          + struct.pack("<H", h_both))

    # ---- A masked subscription forwards every identifier it matches ------
    # SPEC.md §6.8 — mode state is per matching identifier. Shared state let
    # whichever frame arrived first consume the interval, so a mask covering
    # three identifiers delivered exactly one of them and the other two looked
    # like a quiet bus.
    # Its own clock, so advancing it cannot disturb the device under test above.
    mclock = [0]
    masked = dev.VtpDevice(now_us=lambda: mclock[0], gps_hz=0, imu_hz=0)
    masked.on_connect()
    masked.handle_control(
        bytes([dev.CAN_SUBSCRIBE_MASK, 32])
        + struct.pack("<IIBH", 0x000, 0x1FFFF000, dev.SUB_EVERY_FRAME, 0))
    matched = set()
    for _ in range(400):
        mclock[0] += 5_000
        for characteristic, payload in masked.poll():
            masked.commit_seq(characteristic)
            if characteristic == "can":
                for r in vtp1.decode_can_batch(payload)["records"]:
                    matched.add(r["id"])
    check(matched == {0x0C0, 0x1A0, 0x2E0},
          f"a mask matching three identifiers forwarded {sorted(matched)}; "
          f"every matching identifier keeps its own mode state")

    # ---- The request lifecycle ------------------------------------------
    # SPEC.md §9.6 — admission is decided before dispatch, so these are the
    # rules that stop a device applying a request it cannot answer. Exercised
    # against the transport's queue directly: it holds no Bluetooth state
    # precisely so that this is testable without a radio.
    q = serve.ControlQueue()
    check(q.depth == serve.CONTROL_OUTSTANDING == 1,
          f"SPEC.md 9 allows ONE outstanding request; the queue declares "
          f"{q.depth}")

    check(q.admit(7) == "apply", "the first request MUST be admitted")
    q.hold(7, bytes([0x02, 7, 0]))

    # A client that pipelines anyway. It gets `busy` -- which is why the status
    # survived the removal of the queue it used to describe: the alternatives
    # are silence and applying a request the device cannot answer, and §9.6
    # forbids both.
    check(q.admit(8) == "busy",
          "a second request written before the first was answered MUST be "
          "answered busy, not silently discarded and not applied")

    # ...and `busy` is a response, so it needs somewhere to sit. A device whose
    # only slot holds the answer it already owes has nothing left to say busy
    # with, which is why the queue is one deeper than the rule.
    q.hold(8, bytes([0x02, 8, 5]))
    check(len(q) == 2,
          f"the busy refusal must be queued behind the response it was "
          f"refused for, not dropped; queue holds {len(q)}")

    # SPEC.md §9 — with one request outstanding, tag ambiguity is not
    # prevented, it is impossible: a second request written before the first is
    # answered is refused whatever tag it carries, and one written after cannot
    # collide with anything. There is no duplicate-tag verdict to test, and a
    # device needs no tag table.
    q.delivered()
    q.delivered()
    check(q.admit(9) == "apply", "the queue should be empty again")
    q.hold(9, bytes([0x02, 9, 0]))
    check(q.admit(9) == "busy",
          "a request reusing the outstanding tag is refused for the same "
          "reason any other concurrent request is: the answer is still owed")
    q.delivered()
    check(q.admit(9) == "apply",
          "a tag MUST become reusable once its response has been delivered; "
          "the rule is `not while outstanding`, not `never twice`")

    # Nothing is owed to a client that has gone.
    q.hold(0, bytes([0x02, 0, 0]))
    q.discard_all()
    check(len(q) == 0 and q.dropped > 0,
          "a dropped link MUST clear the queue and count what never arrived")

    # ---- Encryption postures --------------------------------------------
    # SPEC.md §10 — a device MAY require an encrypted link on any
    # characteristic, all of them, or none; a client MUST support each. The
    # peripheral can present all three, and the mapping is checked here rather
    # than only at startup: a posture that leaves something unencrypted took a
    # different path through the permission arithmetic than one that does not,
    # and only the encrypt-everything path had been exercised.
    streams = {"gps", "can", "imu", "monitor_values"}
    postures = {p: serve.encrypted_characteristics(p)
                for p in serve.ENCRYPTION_POSTURES}
    check(postures["none"] == set(),
          f"the none posture MUST protect nothing, protects {postures['none']}")
    check(postures["control"] == {"control"},
          f"the control posture MUST protect Control alone, protects "
          f"{postures['control']}")
    check(postures["all"] == streams | {"control"},
          f"the all posture MUST protect every stream and Control, protects "
          f"{postures['all']}")
    for name, protected in postures.items():
        # SPEC.md §10.2 — Info stays readable in every posture, so a client
        # that cannot pair can still identify what it found.
        check("info" not in protected,
              f"the {name} posture encrypts Info; §10.2 says to leave it "
              f"readable so an unpaired client can still identify the device")
    # ---- Semantic constraints (SPEC.md §5.4, §7.1, §7.2) -----------------
    # Every fix this device emits must be somewhere on earth, and every batch
    # must carry a period a client can divide by.
    for f in gps:
        check(abs(f["lat"]) <= 900_000_000 and abs(f["lon"]) <= 1_800_000_000,
              f"gps: lat/lon outside the earth ({f['lat']}, {f['lon']})")
        if "head_mot" not in f["absent"]:
            check(0 <= f["head_mot"] < 36_000_000,
                  f"gps: head_mot {f['head_mot']} outside 0..360 degrees")
    for b in imu:
        check(b["header"]["period"] > 0,
              "imu: a period of zero says every sample was taken at one "
              "instant, which describes no measurement")
        check(b["header"]["saturated"] is False,
              "imu: this synthetic vehicle stays well inside the sensor range, "
              "so nothing should be flagged saturated")

    # SPEC.md §7.1 — specific force, not acceleration. A level car at rest
    # reads +1 g on the axis that points UP. Both signs are in use in the
    # wild, and a client that assumes the wrong one sees a car braking when it
    # is accelerating, with every magnitude still looking right.
    level = [s for b in imu for s in b["samples"]]
    check(level and all(s["az"] > 0 for s in level),
          "imu: a level vehicle MUST report a POSITIVE az under the specific-"
          "force convention; a negative one is the gravity-vector convention "
          "and means every client reading this device has its signs inverted")

    # SPEC.md §7.2 — the flag is computed, not hardcoded clear.
    sat = dev.VtpDevice(now_us=lambda: 0, gps_hz=0, imu_hz=100)
    sat._imu_pending = [dict(ax=32767, ay=0, az=1000, gx=0, gy=0, gz=0)]
    check(sat._imu_saturation() == dev.IMU_SATURATED,
          "a sample at the i16 rail MUST raise the saturation flag")
    sat._imu_pending = [dict(ax=1, ay=2, az=1000, gx=0, gy=0, gz=0)]
    check(sat._imu_saturation() == 0,
          "an ordinary sample MUST NOT raise the saturation flag")

    # ---- Monitor values go stale, and say so ----------------------------
    # SPEC.md §13.5 — silence is the ONLY symptom a device sees when a client
    # crashes, is backgrounded or wedges: the link stays up and the writes
    # simply stop. Without an expiry the screen keeps showing a lap time from
    # minutes ago and the driver reading it cannot tell.
    mclk = [0]
    mon = dev.VtpDevice(now_us=lambda: mclk[0], gps_hz=0, imu_hz=0)
    mon.on_connect()
    declared = vtp1.decode_monitor_list(
        mon.handle_control(bytes([dev.MONITOR_LIST, 1]))[3:])
    by_slot = {e["slot"]: e for e in declared["entries"]}
    check(declared["declaration"]["count"] <= vtp1.MONITOR_MAX_CHANNELS,
          f"a device MUST NOT ask for more channels than fit in one complete "
          f"write: {declared['declaration']['count']} > "
          f"{vtp1.MONITOR_MAX_CHANNELS}")
    # SPEC.md §13.5 — every declared channel carries a deadline, and none of
    # them is zero. There used to be two kinds of channel here, "perishable"
    # and "durable", plus a derived device-wide liveness bound to expire the
    # durable ones; one rule per channel replaced all of it.
    check(all(e["max_age"] for e in declared["entries"]),
          f"every declared channel MUST carry a non-zero max_age: "
          f"{[(e['slot'], e['max_age']) for e in declared['entries']]}")
    deadlines = sorted(declared["entries"], key=lambda e: e["max_age"])
    check(deadlines[0]["max_age"] != deadlines[-1]["max_age"],
          "this device should declare channels with DIFFERENT deadlines, or "
          "the per-channel half of the rule is not exercised")

    # The shortest and the longest, so one expires while the other is still
    # inside its own deadline. That is the whole of §13.5 now.
    slot_fast = deadlines[0]["slot"]
    slot_slow = deadlines[-1]["slot"]
    # SPEC.md §13.4 — a write is a complete statement, so it carries every
    # declared slot and not just the two this check is about.
    all_slots = [e["slot"] for e in declared["entries"]]
    def value_for(slot):
        return {slot_fast: 12345, slot_slow: 99999}.get(slot, 7)
    write = struct.pack("<HBB", 1, len(all_slots), 0) + b"".join(
        struct.pack("<BBi", s, dev.MONITOR_PRESENT, value_for(s))
        for s in all_slots)
    check(mon.handle_monitor_write(write) is None,
          "a well-formed monitor write was rejected")

    def present_at(t_us):
        mclk[0] = t_us
        return {slot: present for slot, _, _, present in mon.monitor_state()}

    deadline = by_slot[slot_fast]["max_age"] * 100_000
    check(present_at(deadline - 100_000)[slot_fast],
          "a value inside its max_age MUST still be shown")
    check(not present_at(deadline + 100_000)[slot_fast],
          "a value past its max_age MUST be rendered unavailable, not held on "
          "screen as though it were current")
    check(present_at(deadline + 100_000)[slot_slow],
          "a deadline belongs to its own channel: a longer-lived channel MUST "
          "survive the expiry of a shorter-lived one")
    # ...and it expires too, on its own deadline. Nothing is immortal, which
    # is what the liveness bound existed to guarantee and what a per-channel
    # deadline now guarantees directly.
    slow_deadline = by_slot[slot_slow]["max_age"] * 100_000
    check(present_at(slow_deadline - 100_000)[slot_slow],
          "a value inside its own max_age MUST still be shown")
    after = present_at(slow_deadline + 100_000)
    check(not any(after.values()),
          f"past the LONGEST declared deadline nothing may still be shown; "
          f"still present: {sorted(s for s, p in after.items() if p)}")

    # SPEC.md §13.4 — a slot twice in one write, and nothing says which wins.
    twice = (struct.pack("<HBB", 2, len(all_slots) + 1, 0)
             + struct.pack("<BBi", slot_fast, dev.MONITOR_PRESENT, 1)
             + b"".join(struct.pack("<BBi", s, dev.MONITOR_PRESENT, value_for(s))
                        for s in all_slots))
    check(mon.handle_monitor_write(twice) == "duplicate-slot",
          "a write naming one slot twice MUST be rejected rather than "
          "resolved arbitrarily")

    # ---- TIME_SYNC bounds its own error ---------------------------------
    # SPEC.md §9.7 — two readings, and the earlier one MUST be taken when the
    # write arrived rather than when the reply is composed. A device reading
    # its clock once and reporting it twice looks identical on the wire to one
    # that answered instantly, so the check drives it with a known gap.
    tclock = [5_000_000]
    timed = dev.VtpDevice(now_us=lambda: tclock[0], gps_hz=0, imu_hz=0)
    timed.on_connect()
    arrived = tclock[0]
    tclock[0] += 1_500                      # the device spends 1.5 ms answering
    answer = timed.handle_control(bytes([dev.TIME_SYNC, 1]), t_rx=arrived)
    check(answer[2] == dev.ST_OK, "TIME_SYNC was refused")
    sync = vtp1.decode_time_sync(answer[3:])
    check(sync["t_device_rx"] == arrived,
          f"t_device_rx is {sync['t_device_rx']}, not the {arrived} at which "
          f"the write arrived; §9.7 forbids reading the clock later")
    check(sync["processing_us"] == 1_500,
          f"the device took 1500 us to answer and reported "
          f"{sync['processing_us']}; that difference is the whole point of "
          f"the two-timestamp form")
    check(sync["t_device_tx"] >= sync["t_device_rx"],
          "t_device_tx MUST NOT precede t_device_rx")

    # SPEC.md §5.6 — a device that knows the solution epoch says so.
    epoch_clock = [0]
    epoch_dev = dev.VtpDevice(now_us=lambda: epoch_clock[0], gps_hz=10, imu_hz=0)
    epoch_dev.on_connect()
    stamped = None
    for _ in range(200):
        epoch_clock[0] += 5_000
        for characteristic, payload in epoch_dev.poll():
            if characteristic == "gps" and stamped is None:
                stamped = vtp1.decode_gps_fix(payload)
    check(stamped is not None and
          stamped["fix_flags"] & dev.FIX_FLAG_SOLUTION_EPOCH,
          "this device computes its fix at a known instant, so t_device IS "
          "the solution epoch and fix_flags bit 4 MUST say so")

    # ---- Per-connection state -------------------------------------------
    # SPEC.md §8.2 — the FIRST notification after a connection carries seq 0.
    fresh_clock = [0]
    fresh = dev.VtpDevice(now_us=lambda: fresh_clock[0], gps_hz=10, imu_hz=100)
    fresh.on_connect()
    firsts = {}
    for _ in range(200):
        fresh_clock[0] += 5_000
        for characteristic, payload in fresh.poll():
            payload = fresh.stamp_seq(characteristic, payload)
            fresh.commit_seq(characteristic)
            if characteristic in firsts:
                continue
            # Not named `decode`: this function already has one, and
            # shadowing it here broke the batch checks further down.
            reader = {"gps": vtp1.decode_gps_fix,
                      "imu": vtp1.decode_imu_batch}[characteristic]
            got = reader(payload)
            firsts[characteristic] = (got.get("seq")
                                      if "seq" in got else got["header"]["seq"])
    for characteristic, first in sorted(firsts.items()):
        check(first == 0,
              f"{characteristic}: the first notification of a connection "
              f"carries seq {first}; §8.2 says 0")

    # SPEC.md §9.2 — a handle MUST NOT be reused while its subscription lives.
    wrapper = dev.VtpDevice(now_us=lambda: 0, gps_hz=0, imu_hz=0)
    wrapper.on_connect()
    live = struct.unpack("<H", wrapper.handle_control(
        bytes([dev.CAN_SUBSCRIBE, 1])
        + struct.pack("<IBH", 0x0C0, dev.SUB_EVERY_FRAME, 0))[3:])[0]
    wrapper._next_handle = 0xFFFF          # one install short of wrapping
    for tag, cid in ((2, 0x111), (3, 0x222)):
        wrapper.handle_control(bytes([dev.CAN_SUBSCRIBE, tag])
                               + struct.pack("<IBH", cid, dev.SUB_EVERY_FRAME, 0))
    check(wrapper._subscriptions[live]["id"] == 0x0C0,
          f"handle {live} was reassigned when the counter wrapped; the client "
          f"would unsubscribe a subscription it never installed")
    check(len(set(wrapper._subscriptions)) == len(wrapper._subscriptions),
          "the wrap produced a duplicate handle")

    # SPEC.md §8.2 and §8.3 — a refused notification was never sent, so it
    # neither consumes a sequence number nor discards the backlog its header
    # was reporting.
    rclock = [0]
    refuser = dev.VtpDevice(now_us=lambda: rclock[0], gps_hz=0, imu_hz=0)
    refuser.on_connect()
    refuser.handle_control(bytes([dev.CAN_SUBSCRIBE, 1])
                           + struct.pack("<IBH", 0x0C0, dev.SUB_EVERY_FRAME, 0))
    refuser._dropped["can"] = 500
    rclock[0] += 100_000
    batch = next(p for c, p in refuser.poll() if c == "can")
    was = vtp1.decode_can_batch(batch)["header"]
    refuser.record_refused("can", batch)
    rclock[0] += 100_000
    now_hdr = vtp1.decode_can_batch(
        next(p for c, p in refuser.poll() if c == "can"))["header"]
    check(now_hdr["seq"] == was["seq"],
          f"a refused notification consumed seq {was['seq']}; the next one is "
          f"{now_hdr['seq']}, so the client sees a gap for data that never "
          f"went out")
    check(now_hdr["dropped"] == was["dropped"] + was["count"],
          f"the refused header reported {was['dropped']} dropped and carried "
          f"{was['count']} record(s), but the next reports "
          f"{now_hdr['dropped']}; the backlog was thrown away with the "
          f"notification")

    # SPEC.md §9.1 — the negotiated MTU is reported, and drives batch sizing.
    sized = dev.VtpDevice(now_us=lambda: 0, mtu=247)
    roomy = sized._can_capacity()
    sized.set_negotiated_mtu(185)
    check(sized._can_capacity() < roomy,
          "a smaller negotiated MTU MUST shrink the batch, or the device "
          "builds notifications the link cannot carry")
    reported = vtp1.decode_link_params(
        sized.handle_control(bytes([dev.GET_LINK_PARAMS, 1]))[3:])
    check(reported["att_mtu"] == 185 and "att_mtu" not in reported["absent"],
          "GET_LINK_PARAMS MUST report the negotiated ATT MTU once known")
    check(dev.MIN_ATT_MTU == 100,
          "SPEC.md §2's minimum ATT MTU should come from the schema")

    # SPEC.md §4.2 — max_notify_bytes is a DEVICE ceiling, so it does not move
    # when the link does. It used to be the negotiated payload, which a client
    # cannot ever read at the right moment: Info is read on connect, and this
    # peripheral does not learn the negotiated maximum until a central
    # subscribes, which is later.
    ceiling = vtp1.decode_info(sized.info())["max_notify_bytes"]
    check(ceiling == 247 - 3,
          f"max_notify_bytes must be the device ceiling (244), not the "
          f"negotiated {sized.notify_bytes}; got {ceiling}")
    sized.set_negotiated_mtu(517)
    check(vtp1.decode_info(sized.info())["max_notify_bytes"] == ceiling,
          "a larger negotiated MTU must NOT raise the ceiling Info published; "
          "a client sized its buffer from that number")
    check(sized.notify_bytes <= ceiling,
          f"batches must never exceed the published ceiling: sizing for "
          f"{sized.notify_bytes} against a ceiling of {ceiling}")

    # ...and nothing negotiated may outlive the link that negotiated it.
    sized.on_disconnect()
    after = vtp1.decode_link_params(
        sized.handle_control(bytes([dev.GET_LINK_PARAMS, 2]))[3:])
    check("att_mtu" in after["absent"],
          "after the link dropped, GET_LINK_PARAMS still reported the ATT MTU "
          "that link negotiated — with the validity bit set, which asserts it "
          "is a measurement of the link being asked about")

    # SPEC.md §9.1 — a grouped validity bit is set when every field of the
    # group is KNOWN. `peripheral_latency` of 0 is known, and is the value §2
    # says a device SHOULD request while streaming; a truthiness test read it
    # as missing and reported the whole group absent.
    latent = dev.VtpDevice(now_us=lambda: 0, mtu=247)
    latent.set_link_params(conn_interval=12, peripheral_latency=0,
                           supervision_timeout=500)
    group = vtp1.decode_link_params(
        latent.handle_control(bytes([dev.GET_LINK_PARAMS, 3]))[3:])
    check("peripheral_latency" not in group["absent"],
          "a peripheral_latency of 0 is a value, not an absence")
    check(group["peripheral_latency"] == 0 and group["conn_interval"] == 12,
          f"the connection-parameter group must report what it was told: "
          f"{group}")

    try:
        serve.encrypted_characteristics("sometimes")
        check(False, "an unknown posture MUST be rejected, not silently "
                     "treated as one of the three")
    except ValueError:
        pass

    # A start past the end is ok with count 0, never an error.
    beyond = vtp1.decode_can_list(
        device.handle_control(bytes([dev.CAN_LIST, 10])
                              + struct.pack("<H", 99))[3:])
    check(beyond["page"]["count"] == 0 and beyond["page"]["total"] == 1,
          "CAN_LIST past the end MUST answer ok with count 0 and the true total")

    # An unknown handle is refused, not silently ignored.
    bad = device.handle_control(bytes([dev.CAN_UNSUBSCRIBE, 11])
                                + struct.pack("<H", 0xBEEF))
    check(bad[2] == dev.ST_UNKNOWN_HANDLE,
          "unsubscribing an unknown handle MUST answer unknown_handle")

    device.handle_control(bytes([dev.CAN_UNSUBSCRIBE, 1])
                          + struct.pack("<H", handle))
    after = run(device, clock, 1.0)
    frames = [r for c, p in after if c == "can"
              for r in (decode(c, p) or {"records": []})["records"]]
    check(not frames, "CAN frames continued after CAN_UNSUBSCRIBE")

    resp = device.handle_control(bytes([dev.GPS_SET_RATE, 2])
                                 + struct.pack("<H", 5))
    check(resp[2] == dev.ST_OK, "GPS_SET_RATE 5 Hz was refused")
    before = len(run(device, clock, 2.0))
    check(before > 0, "the device stopped emitting after a rate change")

    resp = device.handle_control(bytes([dev.GPS_SET_RATE, 3])
                                 + struct.pack("<H", 1000))
    check(resp[2] == dev.ST_RATE_EXCEEDED,
          "a rate above gps_max_rate_hz MUST be refused with rate_exceeded, "
          "not accepted and silently clamped")

    resp = device.handle_control(bytes([dev.TIME_SYNC, 4]))
    check(device.handle_control(bytes([dev.TIME_SYNC, 5])
                                + struct.pack("<q", 0))[2] == dev.ST_BAD_PARAMS,
          "SPEC.md §9.7 — TIME_SYNC is parameterless; the host UTC field it "
          "used to carry could not be used by the equations and was discarded")
    check(resp[2] == dev.ST_OK
          and len(resp) == 3 + vtp1.SCHEMA["records"]["time_sync"]["size"],
          "TIME_SYNC must answer ok with a time_sync record. This had "
          "asserted 11 bytes, the single-timestamp form SPEC.md §9.7 "
          "replaced, and the length is taken from the schema now so the "
          "record cannot change under it again")

    resp = device.handle_control(bytes([dev.GET_LINK_PARAMS, 5]))
    check(resp[2] == dev.ST_OK, "GET_LINK_PARAMS was refused")
    link = vtp1.decode_link_params(resp[3:])
    check(set(link["absent"]) == {
        "att_mtu", "ll_max_tx_octets", "ll_max_rx_octets", "conn_interval",
        "peripheral_latency", "supervision_timeout", "phy_tx", "phy_rx"},
        "link_params: with no link attached every field MUST read absent, "
        "never as a guess")

    # SPEC.md §9.3 — a frame matching several subscriptions is forwarded once,
    # governed by the most specific mask.
    device.handle_control(bytes([dev.CAN_RESET, 20]))
    broad = device.handle_control(bytes([dev.CAN_SUBSCRIBE_MASK, 21])
                                  + struct.pack("<IIBH", 0x0C0, 0x1FFFFF00,
                                                dev.SUB_EVERY_FRAME, 0))
    check(broad[2] == dev.ST_OK, "CAN_SUBSCRIBE_MASK was refused")
    device.handle_control(bytes([dev.CAN_SUBSCRIBE, 22])
                          + struct.pack("<IBH", 0x0C0, dev.SUB_EVERY_NTH, 3))
    overlap = run(device, clock, 2.0)
    got = [r for c, p in overlap if c == "can"
           for r in (decode(c, p) or {"records": []})["records"]]
    check(all(r["id"] == 0x0C0 for r in got),
          "an id outside both subscriptions was forwarded")
    ts = [r["t_device_us"] for r in got]
    check(len(ts) == len(set(ts)),
          "a frame was forwarded more than once: two subscriptions matched and "
          "both emitted, which is indistinguishable from a bus fault")
    check(len(got) > 0,
          "no frames were forwarded while two overlapping subscriptions were "
          "installed; the more specific one should govern, not silence the id")
    # 0x0C0 arrives at 50 Hz, so 2 s is ~100 frames. The broad mask says
    # every_frame and the exact id says every 3rd; §9.3 makes the exact one
    # govern, so ~33 is right and ~100 means the wrong subscription won.
    check(len(got) < 60,
          f"{len(got)} frames in 2 s: the broad every_frame mask governed "
          f"instead of the more specific every_nth subscription (SPEC.md §9.3)")

    # SPEC.md §6.8 — the first matching frame is forwarded in every mode, and
    # every_nth with N of 0 is meaningless.
    device.handle_control(bytes([dev.CAN_RESET, 30]))
    zero = device.handle_control(bytes([dev.CAN_SUBSCRIBE, 31])
                                 + struct.pack("<IBH", 0x0C0,
                                               dev.SUB_EVERY_NTH, 0))
    check(zero[2] == dev.ST_BAD_PARAMS,
          "every_nth with N of 0 selects no frames and MUST be bad_params")

    device.handle_control(bytes([dev.CAN_SUBSCRIBE, 32])
                          + struct.pack("<IBH", 0x0C0, dev.SUB_EVERY_NTH, 5))
    early = run(device, clock, 0.1)
    first_frames = [r for c, p in early if c == "can"
                    for r in (decode(c, p) or {"records": []})["records"]]
    check(len(first_frames) >= 1,
          "every_nth held back the first matching frame: SPEC.md §6.8 forwards "
          "it in every mode, so a client need not wait for a second")

    resp = device.handle_control(bytes([0xEE, 6]))
    check(resp[2] == dev.ST_UNSUPPORTED,
          "an unimplemented opcode MUST answer unsupported_opcode, and MUST "
          "still be answered")

    # ---- Sequence and loss (SPEC.md §8.2, §8.3) -------------------------
    fresh = dev.VtpDevice(now_us=lambda: clock[0], mtu=247, gps_hz=10, imu_hz=0)
    first = run(fresh, clock, 0.5)
    first_gps = [decode(c, p) for c, p in first if c == "gps"]
    check(first_gps and first_gps[0]["seq"] == 0,
          "SPEC.md §8.2: seq restarts at 0 ON the first notification of a "
          "connection. This had asserted 1, encoding the device's own "
          "pre-increment rather than the rule, so the check agreed with the "
          "bug it existed to catch")

    # A device that never loses anything cannot demonstrate that it reports
    # loss, which is why simulate_loss exists.
    fresh.simulate_loss("gps", 5)
    after_loss = [decode(c, p) for c, p in run(fresh, clock, 0.3) if c == "gps"]
    check(after_loss and after_loss[0]["dropped"] == 5,
          "a discarded fix MUST be reported in dropped on the next notification")
    check(len(after_loss) > 1 and after_loss[1]["dropped"] == 0,
          "dropped counts since the PREVIOUS notification, so it MUST reset")

    fresh.simulate_loss("gps", 200_000)
    saturated = [decode(c, p) for c, p in run(fresh, clock, 0.3) if c == "gps"]
    check(saturated and saturated[0]["dropped"] == 0xFFFF,
          f"dropped MUST saturate at 65535, not wrap: 200000 discards reported "
          f"as {saturated[0]['dropped'] if saturated else 'nothing'}")

    # SPEC.md §9.2 — a reconnection inherits nothing.
    fresh.handle_control(bytes([dev.CAN_SUBSCRIBE, 1])
                         + struct.pack("<IBH", 0x0C0, dev.SUB_EVERY_FRAME, 0))
    fresh.on_connect()
    table = vtp1.decode_can_list(
        fresh.handle_control(bytes([dev.CAN_LIST, 2]) + struct.pack("<H", 0))[3:])
    check(table["page"]["total"] == 0,
          "subscriptions MUST NOT survive a reconnection")
    reconnected = [decode(c, p) for c, p in run(fresh, clock, 0.3) if c == "gps"]
    check(reconnected and reconnected[0]["seq"] == 0,
          "seq MUST restart at 0 on a new connection")

    # ---- A stall must lose samples, not replay them (§8.3) --------------
    stalled = dev.VtpDevice(now_us=lambda: clock[0], mtu=247, gps_hz=0,
                            imu_hz=100)
    clock[0] += 60_000_000          # a minute with nobody polling
    burst = stalled.poll()
    check(len(burst) <= 2,
          f"a minute of backlog produced {len(burst)} notifications in one "
          f"poll; a device MUST discard what it cannot deliver, not replay it")
    decoded = [decode(c, p) for c, p in burst if c == "imu"]
    check(decoded and decoded[0]["header"]["dropped"] > 0,
          "samples discarded during a stall MUST be reported in dropped, not "
          "dropped silently")
    ts = [s["t_device_us"] for b in decoded for s in b["samples"]]
    check(all(t > 59_000_000 for t in ts),
          f"the samples delivered after a stall MUST be recent ones, not the "
          f"start of the backlog: {ts[:3]}")

    # ---- Monitor: the client supplies, the device displays (§13) --------
    mon = dev.VtpDevice(now_us=lambda: clock[0], mtu=247, gps_hz=0, imu_hz=0)
    info2 = vtp1.decode_info(mon.info())
    check(info2["capabilities"] & (1 << 3),
          "a device implementing Monitor MUST declare capability bit 3")

    listing = mon.handle_control(bytes([dev.MONITOR_LIST, 1]))
    check(listing[2] == dev.ST_OK, "MONITOR_LIST was refused")
    table = vtp1.decode_monitor_list(listing[3:])
    check(table["declaration"]["count"] == 6,
          f"expected 6 requested channels, got {table['declaration']['count']}")

    # SPEC.md §13.3 — parameterless. A trailing `start` is a malformed request
    # now, not a page number, and §9 rejects trailing parameters.
    check(mon.handle_control(bytes([dev.MONITOR_LIST, 2])
                             + struct.pack("<H", 0))[2] == dev.ST_BAD_PARAMS,
          "MONITOR_LIST takes no parameters; a trailing start MUST be refused")
    check(all(e["channel_known"] for e in table["entries"]),
          "the device asked for a channel the reference decoder cannot name")

    # Before anything is supplied, every slot is absent and renders as such.
    check(all(not present for _, _, _, present in mon.monitor_state()),
          "nothing has been supplied, so no slot may be present")
    check(all(disp.ABSENT in line for line in mon.display_lines()),
          f"a device MUST NOT display a value nobody supplied: "
          f"{mon.display_lines()}")

    # Mid first lap: elapsed is known; last lap and delta do not exist yet.
    import vtp1_encode as menc
    # SPEC.md §13.4 — a write carries every declared slot. Elapsed is known;
    # everything else does not exist yet and says so with a clear present bit,
    # which is a different statement from leaving it out.
    mon_slots = [slot for slot, _ in mon._monitor_channels]
    update = menc.encode_monitor_update(
        {"seq": 1, "count": len(mon_slots), "reserved": 0},
        [{"slot": s,
          "validity": dev.MONITOR_PRESENT if s == 0 else 0,
          "value": 42_318 if s == 0 else 0} for s in mon_slots])
    check(mon.handle_monitor_write(update) is None,
          "a well-formed monitor update was rejected")
    state = {slot: (value, present) for slot, _, value, present
             in mon.monitor_state()}
    check(state[0] == (42_318, True), f"elapsed lap time not stored: {state}")
    check(state[1] == (0, False) and state[2] == (0, False),
          f"a slot whose present bit is clear MUST be absent and zero: {state}")
    check(all(state[s] == (0, False) for s in (3, 4, 5)),
          "a slot the client has not written about at all MUST stay absent, "
          "never default to a value")
    lines = mon.display_lines()
    check(lines[0] == "LAP: 42.318",
          f"lap time should render as a clock, got {lines[0]!r}")
    check(disp.ABSENT in lines[1] and disp.ABSENT in lines[2],
          f"an absent slot MUST render as absence, not as 0: {lines}")

    # A cleared present bit with a stale value in the bytes: the bit governs.
    stale = menc.encode_monitor_update(
        {"seq": 2, "count": len(mon_slots), "reserved": 0},
        [{"slot": s,
          "validity": dev.MONITOR_PRESENT if s == 0 else 0,
          "value": 87_340 if s == 1 else (42_318 if s == 0 else 0)}
         for s in mon_slots])
    check(mon.handle_monitor_write(stale) is None,
          "the write must be ACCEPTED for this check to mean anything; a "
          "rejected one leaves the previous display in place and the check "
          "below then passes without testing the bit at all")
    check(disp.ABSENT in mon.display_lines()[1],
          "a stale value behind a cleared present bit MUST NOT be displayed")

    # A slot the device never asked for is ignored, not an error.
    # §13.1 and §13.4 together: the write still carries every slot the device
    # asked for, and the one it did not ask for is ignored rather than making
    # the write an error. A write of the stray slot ALONE would be incomplete,
    # which is a different fault and correctly a rejection.
    stray = menc.encode_monitor_update(
        {"seq": 3, "count": len(mon_slots) + 1, "reserved": 0},
        [{"slot": s, "validity": 0, "value": 0} for s in mon_slots]
        + [{"slot": 200, "validity": dev.MONITOR_PRESENT, "value": 5}])
    check(mon.handle_monitor_write(stray) is None,
          "a value for an unrequested slot MUST be ignored, not rejected")
    only_stray = menc.encode_monitor_update(
        {"seq": 4, "count": 1, "reserved": 0},
        [{"slot": 200, "validity": dev.MONITOR_PRESENT, "value": 5}])
    check(mon.handle_monitor_write(only_stray) is not None,
          "a write carrying ONLY an unrequested slot names none of the slots "
          "the device asked for, so it is incomplete (§13.4) and MUST be "
          "rejected")

    check(mon.handle_monitor_write(update[:-1]) is not None,
          "a truncated monitor update MUST be rejected, not partly applied")

    # A reconnection starts blank rather than showing the last session.
    mon.on_connect()
    check(all(disp.ABSENT in line for line in mon.display_lines()),
          "a reconnection MUST clear the display, not inherit the previous "
          "connection's values")
    check(mon.monitor_updates == 0,
          "the update counter MUST reset with the connection")

    # ---- Rendering, which is the only way the present bit is visible -------
    check(disp.format_value(disp.LAP_TIME, 87_340, True) == "1:27.340",
          "a lap time over a minute should render as minutes and seconds")
    check(disp.format_value(disp.LAP_TIME, 42_318, True) == "42.318",
          "a lap time under a minute should not show a leading 0:")
    check(disp.format_value(disp.DELTA_BEST, 1_250, True) == "+1.250",
          "a positive delta MUST show its sign, or it reads as a fast lap")
    check(disp.format_value(disp.DELTA_BEST, -1_250, True) == "-1.250",
          "a negative delta should render negative")
    # The unit is part of the static cell label, so the formatter must never
    # change units under it. It used to: below 1 km it rendered bare metres
    # beneath a heading that said "km".
    check(disp.format_value(disp.SESSION_DISTANCE, 999, True) == "0.999",
          f"999 m must render as 0.999 km, not as 999 under a km heading; "
          f"got {disp.format_value(disp.SESSION_DISTANCE, 999, True)!r}")
    check(disp.format_value(disp.SESSION_DISTANCE, 12_345, True) == "12.345",
          f"12345 m must render as 12.345 km; got "
          f"{disp.format_value(disp.SESSION_DISTANCE, 12_345, True)!r}")
    check(disp.format_value(disp.SPEED, 38_000, True) == "136.8",
          "speed is mm/s on the wire and km/h on a dash")
    for channel in (disp.LAP_TIME, disp.DELTA_BEST, disp.SPEED,
                    disp.LAP_NUMBER, disp.SESSION_DISTANCE):
        rendered = disp.format_value(channel, 12_345, False)
        check(rendered == disp.ABSENT,
              f"channel {channel} rendered {rendered!r} for an absent value; "
              f"a cleared present bit MUST win over whatever is in the field")

    # The formatter mirrors SPEC.md §13.2's enum; drift between them would show
    # the wrong label against the right number.
    check((disp.LAP_TIME, disp.DELTA_BEST, disp.SESSION_TIME)
          == (dev.CH_LAP_TIME, dev.CH_DELTA_BEST, dev.CH_SESSION_TIME),
          "display.py's channel constants have drifted from the device's")

    # ---- A refused notification is loss, and must be reported (§8.3) ----
    # The host stack refuses when its transmit queue is full and returns false.
    # Ignoring that return loses data silently and misreports it, because the
    # device's own counter never learns the notification was never sent.
    refused = dev.VtpDevice(now_us=lambda: clock[0], mtu=247, gps_hz=10,
                            imu_hz=100)
    refused.handle_control(bytes([dev.CAN_SUBSCRIBE, 1])
                           + struct.pack("<IBH", 0x0C0, dev.SUB_EVERY_FRAME, 0))
    produced = run(refused, clock, 1.0)
    by_stream = {}
    for characteristic, payload in produced:
        by_stream.setdefault(characteristic, []).append(payload)

    for characteristic, payloads in by_stream.items():
        lost = refused.record_refused(characteristic, payloads[0])
        check(lost > 0,
              f"{characteristic}: a refused notification must count at least "
              f"one lost item, got {lost}")
        if characteristic in ("can", "imu"):
            decoded = decode(characteristic, payloads[0])
            expected = decoded["header"]["count"] if decoded else None
            check(lost == expected,
                  f"{characteristic}: a refused batch must count its "
                  f"{expected} records, not {lost} — dropped is defined in "
                  f"source items, not notifications")

    after = [decode(c, p) for c, p in run(refused, clock, 0.5)]
    reported = [d for d in after if d and (
        d.get("dropped") or d.get("header", {}).get("dropped"))]
    check(reported,
          "items lost to a refused notification MUST appear in dropped on a "
          "later notification, or the loss is invisible to the client")

    # ---- A dropped link clears the table (§9.2) -------------------------
    dropped_link = dev.VtpDevice(now_us=lambda: clock[0], mtu=247,
                                 gps_hz=0, imu_hz=0)
    dropped_link.handle_control(bytes([dev.CAN_SUBSCRIBE, 1])
                                + struct.pack("<IBH", 0x0C0,
                                              dev.SUB_EVERY_FRAME, 0))
    check(len(dropped_link.can_table()) == 1, "the subscription did not install")
    dropped_link.on_disconnect()
    check(dropped_link.can_table() == [],
          "SPEC.md §9.2 clears the table when the LINK DROPS, not when the "
          "next connection starts; a disconnected device holding a stale table "
          "reports ids nobody subscribed to")

    # ---- Connection edges drive the per-connection reset ----------------
    # The transport must tell the device when a link starts, or §8.2's sequence
    # restart and §9.2's table clear never happen. They did not, for a while,
    # because nothing called on_connect() outside this file.
    # Importable without bless installed, which is the point: CI has no
    # Bluetooth library and must still be able to check these.
    check(serve.BlessServer is None,
          "importing serve must not pull in bless; it is loaded when the "
          "server starts, so a machine with no Bluetooth can still run this")
    tracker = serve.ConnectionTracker()
    check(tracker.update(False) is None, "no edge from disconnected to disconnected")
    check(tracker.update(True) == "connected", "a first connection is a rising edge")
    check(tracker.update(True) is None, "a steady connection is not an edge")
    check(tracker.update(False) == "disconnected", "a drop is a falling edge")
    check(tracker.update(True) == "connected", "a reconnection is a rising edge")

    # ---- The advertisement has to carry the service UUID ----------------
    # A client that matches on the service UUID never sees a device whose
    # advertisement overflowed and dropped it, and nothing in the peripheral's
    # log says so. This is a pure size calculation, so it is checkable here.
    check(serve.check_advertisement_fits("VTP") is None,
          "the default advertised name must fit beside the service UUID")
    check(serve.check_advertisement_fits("VTP Logger") is not None,
          "a 10-character name does NOT fit beside a 128-bit service UUID in "
          "31 bytes, and must be reported rather than silently overflowing")
    check(serve.MAX_NAME_CHARS == 8,
          f"31 - 3 flags - 18 UUID - 2 header leaves 8 characters, not "
          f"{serve.MAX_NAME_CHARS}")

    # ---- The smoke test's own checks, without a radio -------------------
    # smoketest.py is the only thing here that needs an adapter, which makes it
    # the only thing here nobody runs before pushing. Its BLE half genuinely
    # cannot be tested without hardware; its DECODE and INSPECT half can, and
    # that is where a client author's bugs live. Run it over the software
    # device's own output, so the script that gets pointed at unfamiliar
    # hardware is at least known to work against known-good input.
    import argparse
    import contextlib
    import io
    import smoketest

    def run_smoke(result):
        """Run smoketest.inspect quietly and return the problems it found.

        Quietly because one of the two runs below is a deliberate failure, and
        a passing suite that prints FAIL lines is a suite nobody reads."""
        smoketest.problems.clear()
        smoketest.notes.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            smoketest.inspect(result, argparse.Namespace(seconds=4))
        return list(smoketest.problems)

    smoke_clock = [0]
    smoke_dev = dev.VtpDevice(now_us=lambda: smoke_clock[0], gps_hz=10,
                              imu_hz=100)
    smoke_dev.on_connect()
    smoke_dev.handle_control(
        bytes([dev.CAN_SUBSCRIBE, 1]) + (0x1A0).to_bytes(4, "little")
        + bytes([0]) + (0).to_bytes(2, "little"))
    captured = {"gps": [], "can": [], "imu": []}
    for _ in range(4000):
        smoke_clock[0] += 1000
        for name, payload in smoke_dev.poll():
            captured[name].append(smoke_dev.stamp_seq(name, payload))
            smoke_dev.commit_seq(name)
    found = run_smoke(dict(captured, mtu=247,
                           info=vtp1.decode_info(smoke_dev.info())))
    check(not found,
          f"smoketest.py rejects this repository's own device: {found}")
    check(all(captured[n] for n in ("gps", "can", "imu")),
          "the smoke-test fixture produced no notifications, so its checks "
          "passed by having nothing to look at")

    # ...and it must be able to fail. A notification larger than the ceiling
    # Info published is exactly what SPEC.md 4.2 forbids, and is the check most
    # likely to matter on real hardware with an unexpected MTU.
    overrun = vtp1.decode_info(smoke_dev.info())
    overrun["max_notify_bytes"] = 8
    check(run_smoke(dict(captured, mtu=247, info=overrun)),
          "smoketest.py passed a device whose notifications exceed the "
          "max_notify_bytes it published (SPEC.md 4.2)")

    # A device that connects, answers Info and then sends nothing is the most
    # likely way for a first bring-up to fail, and an empty stream used to be
    # only a note -- so the all-silent device passed.
    silent = {"gps": [], "can": [], "imu": [], "mtu": 247,
              "info": vtp1.decode_info(smoke_dev.info())}
    check(run_smoke(silent),
          "smoketest.py passed a device that sent nothing at all while Info "
          "said it was running")

    # ...and the rate-aware half of that: GPS silent while Info promises 10 Hz
    # is a fault, whatever the other streams did.
    check(run_smoke(dict(captured, gps=[], mtu=247,
                         info=vtp1.decode_info(smoke_dev.info()))),
          "smoketest.py passed a silent GPS stream on a device whose Info "
          "reports a non-zero gps_rate_hz")

    # The converse must NOT fail: a device that says it is not running GPS and
    # then does not send any is behaving correctly, and a check that cannot
    # distinguish those two is a check that will be turned off on real
    # hardware.
    quiet_by_design = vtp1.decode_info(smoke_dev.info())
    quiet_by_design["gps_rate_hz"] = 0
    check(not run_smoke(dict(captured, gps=[], mtu=247, info=quiet_by_design)),
          "smoketest.py failed a device that reports gps_rate_hz 0 and sends "
          "no GPS, which is exactly what such a device should do")

    # ---- The optional CAN capability bits, in the direction nobody runs --
    # SPEC.md 4.1 gives each of the three a rule for when it is CLEAR, and a
    # device that declares all three can only ever demonstrate the other half.
    plain = dev.VtpDevice(
        now_us=lambda: 0,
        capabilities=dev.CAP_CAN | dev.CAP_CONTROL)   # no FD, mask or on_change

    masked = plain.handle_control(
        bytes([dev.CAN_SUBSCRIBE_MASK, 1])
        + struct.pack("<IIBH", 0x1A0, 0x3FFFFFFF, 0, 0))
    check(masked[2] == dev.ST_UNSUPPORTED,
          f"without masked_subscriptions, CAN_SUBSCRIBE_MASK MUST answer "
          f"unsupported_opcode, got status {masked[2]}")

    # ...while CAN_SUBSCRIBE keeps working: it is a separate opcode that every
    # CAN device implements, and the capability governs whether a client may
    # CHOOSE the mask.
    plainsub = plain.handle_control(
        bytes([dev.CAN_SUBSCRIBE, 2]) + struct.pack("<IBH", 0x1A0, 0, 0))
    check(plainsub[2] == dev.ST_OK,
          f"CAN_SUBSCRIBE is unaffected by masked_subscriptions, got status "
          f"{plainsub[2]}")

    onchange = plain.handle_control(
        bytes([dev.CAN_SUBSCRIBE, 3])
        + struct.pack("<IBH", 0x2B0, dev.SUB_ON_CHANGE, 100))
    check(onchange[2] == dev.ST_BAD_PARAMS,
          f"without on_change_subscriptions, an on_change subscription MUST be "
          f"refused with bad_params rather than silently forwarding every "
          f"frame; got status {onchange[2]}")

    # SPEC.md 4.1 -- the capability decides the capacity.
    check(vtp1.decode_info(plain.info())["can_max_payload"] == 8,
          "a CAN device without can_fd MUST report can_max_payload 8")
    fd = dev.VtpDevice(now_us=lambda: 0,
                       capabilities=dev.CAP_CAN | dev.CAP_CONTROL | dev.CAP_CAN_FD)
    check(vtp1.decode_info(fd.info())["can_max_payload"] == 64,
          "a CAN FD device MUST report can_max_payload 64")
    nocan = dev.VtpDevice(now_us=lambda: 0, capabilities=dev.CAP_GPS)
    check(vtp1.decode_info(nocan.info())["can_max_payload"] == 0,
          "a device with no CAN MUST report can_max_payload 0 (SPEC.md 4.1)")

    # ---- The real clock, which the injected one above never exercises ---
    live = dev.VtpDevice(mtu=247, gps_hz=10, imu_hz=100)
    ticks = [live.now_us() for _ in range(200)]
    check(ticks == sorted(ticks),
          "the default monotonic clock went backwards (SPEC.md §8: it MUST "
          "NOT, and every timestamp in the protocol derives from it)")
    check(ticks[-1] >= 0, "the default clock produced a negative timestamp")

    # ---- Report ---------------------------------------------------------
    total = len(gps) + len(can) + len(imu)
    if FAILURES:
        for f in FAILURES:
            print(f"FAIL: {f}", file=sys.stderr)
        print(f"\n{len(FAILURES)} problem(s) across {total} notifications.",
              file=sys.stderr)
        return 1
    print(f"Device conforms: {total} notifications decoded by the reference "
          f"decoder ({len(gps)} GPS, {len(can)} CAN, {len(imu)} IMU), one "
          f"shared clock, control plane verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
