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
    # Bytes 20 and 22-23 stopped being reserved when SPEC.md 15 assigned
    # them; a device declaring bit 10 MUST NOT declare either capacity zero.
    check(info["obd_poll_slots"] == dev.OBD_POLL_SLOTS,
          "info: obd_poll_slots must carry the declared capacity")
    check(info["obd_min_interval_ms"] == dev.OBD_MIN_INTERVAL_MS,
          "info: obd_min_interval_ms must carry the declared floor")
    check(info["obd_poll_slots"] and info["obd_min_interval_ms"],
          "info: a device declaring `obd` MUST NOT declare either capacity "
          "zero (SPEC.md 15)")
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
    check(resp == bytes([dev.CAN_SUBSCRIBE, 0x42, dev.ST_OK]),
          "CAN_SUBSCRIBE MUST answer ok with no detail, echoing the tag")

    # SPEC.md §9.1 — the same (id, mask) updates in place; it never consumes a
    # second slot, so a client reprogramming every connection cannot exhaust
    # the table.
    again = device.handle_control(bytes([dev.CAN_SUBSCRIBE, 0x43])
                                  + struct.pack("<IBH", 0x0C0,
                                                dev.SUB_PERIODIC, 50))
    check(again[2] == dev.ST_OK, "re-subscribing the same id was refused")
    check(len(device.can_table()) == 1,
          f"re-subscribing consumed a slot: table holds "
          f"{len(device.can_table())}")
    check(device.can_table()[0][2] == dev.SUB_PERIODIC,
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
    check(device.can_table()[0][1] == 0x3FFFFFFF,
          "CAN_SUBSCRIBE MUST be recorded as a mask of 0x3FFFFFFF")

    # ---- Subscription identity includes the frame format ----------------
    # SPEC.md §9.1 — standard 0x0C0 and extended 0x0C0 are different frames, so
    # an exact subscription to one MUST NOT match the other. A mask that
    # stopped at 0x1FFFFFFF could not tell them apart, and a client would have
    # decoded the wrong payload behind a correct-looking identifier.
    check(device._governing(0x0C0) is not None,
          "an exact subscription to standard 0x0C0 no longer matches it")
    check(device._governing(0x0C0 | (1 << 29)) is None,
          "an exact subscription to standard 0x0C0 also matches extended "
          "0x0C0; the format bit is part of a frame's identity")

    # A client that genuinely wants both formats clears bit 29 in its mask, and
    # the device can honour that because it can see it was asked.
    both = device.handle_control(
        bytes([dev.CAN_SUBSCRIBE_MASK, 30])
        + struct.pack("<IIBH", 0x1A0, 0x1FFFFFFF, dev.SUB_EVERY_FRAME, 0))
    check(both[2] == dev.ST_OK, "a both-formats subscription was refused")
    check(device._governing(0x1A0) is not None
          and device._governing(0x1A0 | (1 << 29)) is not None,
          "a mask clearing bit 29 MUST match both formats of the identifier")
    # SPEC.md §9.1 — removal names the same (id, mask) that installed it.
    gone = device.handle_control(bytes([dev.CAN_UNSUBSCRIBE, 31])
                                 + struct.pack("<II", 0x1A0, 0x1FFFFFFF))
    check(gone[2] == dev.ST_OK, "CAN_UNSUBSCRIBE by (id, mask) was refused")
    check(device._governing(0x1A0 | (1 << 29)) is None,
          "the both-formats subscription was not removed")

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
    # Derived from the profile rather than listed again. This was a written-out
    # set of six names, so a seventh characteristic was conforming, served,
    # documented -- and left out of the encrypt-everything posture by a
    # constant nobody thinks to revisit. SPEC.md §10 protects what a device
    # exposes, and the schema is what says what that is.
    protectable = {c["name"] for c in dev.enc.SCHEMA["profile"]["characteristics"]
                   if c["name"] != "info"}
    postures = {p: serve.encrypted_characteristics(p)
                for p in serve.ENCRYPTION_POSTURES}
    check(postures["none"] == set(),
          f"the none posture MUST protect nothing, protects {postures['none']}")
    check(postures["control"] == {"control"},
          f"the control posture MUST protect Control alone, protects "
          f"{postures['control']}")
    check(postures["all"] == protectable,
          f"the all posture MUST protect every characteristic but Info, "
          f"protects {postures['all']} and the profile declares {protectable}")
    # SPEC.md §10 says what a device may REQUIRE; bless 0.3.0 is what can
    # actually be required. The gap is invisible at runtime -- the permission
    # is accepted, the server starts, and the characteristic is writable in
    # clear -- so it is asserted here or nowhere. If a future bless closes it,
    # these expectations fail and the warning in serve.py comes out.
    aiding_props = {c["name"]: set(c["properties"])
                    for c in dev.enc.SCHEMA["profile"]["characteristics"]}["aiding"]
    check(aiding_props == {"write-without-response"},
          f"aiding declares {aiding_props}; the backend expectations below are "
          f"written against write-without-response alone")
    check(serve.unenforced_characteristics("all", "winrt") == protectable,
          "winrt shifts the permission word past the bit it is testing, so it "
          "enforces nothing; unenforced_characteristics must say so")
    check(serve.unenforced_characteristics("all", "bluezdbus")
          == {"gps", "can", "imu", "aiding"},
          f"bluezdbus converts only READ and WRITE, so the notify streams and "
          f"aiding go unprotected; got "
          f"{serve.unenforced_characteristics('all', 'bluezdbus')}")
    check(serve.unenforced_characteristics("all", "corebluetooth")
          == {"gps", "can", "imu"},
          f"corebluetooth covers both write forms but not notification "
          f"delivery; got "
          f"{serve.unenforced_characteristics('all', 'corebluetooth')}")
    check(serve.unenforced_characteristics("none", "bluezdbus") == set(),
          "a posture that protects nothing cannot leave anything unprotected")
    check(serve.unenforced_characteristics("control", "bluezdbus") == set(),
          "Control carries `write`, which is the one flag bluezdbus does "
          "convert, so the control posture is fully enforced there")
    check(serve.backend_for("darwin") == "corebluetooth"
          and serve.backend_for("linux") == "bluezdbus"
          and serve.backend_for("win32") == "winrt"
          and serve.backend_for("freebsd13") is None,
          "backend_for does not map the platforms bless supports")

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

    # SPEC.md §9.1 — (id, mask) IS the subscription's identity, so removal
    # must name both halves: the same id under a different mask is a different
    # subscription and MUST answer unknown_subscription.
    ident = dev.VtpDevice(now_us=lambda: 0, gps_hz=0, imu_hz=0)
    ident.on_connect()
    ident.handle_control(bytes([dev.CAN_SUBSCRIBE, 1])
                         + struct.pack("<IBH", 0x0C0, dev.SUB_EVERY_FRAME, 0))
    miss = ident.handle_control(bytes([dev.CAN_UNSUBSCRIBE, 2])
                                + struct.pack("<II", 0x0C0, 0x1FFFFF00))
    check(miss[2] == dev.ST_UNKNOWN_SUBSCRIPTION,
          "the same id under a different mask is a different subscription and "
          "MUST answer unknown_subscription")
    check(len(ident.can_table()) == 1,
          "the miss above must not have removed anything")

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

    # SPEC.md §2 — the negotiated MTU drives batch sizing.
    sized = dev.VtpDevice(now_us=lambda: 0, mtu=247)
    roomy = sized._can_capacity()
    sized.set_negotiated_mtu(185)
    check(sized._can_capacity() < roomy,
          "a smaller negotiated MTU MUST shrink the batch, or the device "
          "builds notifications the link cannot carry")
    sized.set_negotiated_mtu(517)
    check(sized.notify_bytes <= 247 - 3,
          f"batches must never be sized beyond the build's own ceiling: "
          f"sizing for {sized.notify_bytes} on a 247-byte build")
    check(dev.MIN_ATT_MTU == 100,
          "SPEC.md §2's minimum ATT MTU should come from the schema")

    # ...and nothing negotiated may outlive the link that negotiated it.
    sized.set_negotiated_mtu(185)
    sized.on_disconnect()
    check(sized.notify_bytes == 247 - 3,
          "after the link dropped, batches were still sized to the MTU that "
          "link negotiated")

    try:
        serve.encrypted_characteristics("sometimes")
        check(False, "an unknown posture MUST be rejected, not silently "
                     "treated as one of the three")
    except ValueError:
        pass

    # An unknown (id, mask) is refused, not silently ignored.
    bad = device.handle_control(bytes([dev.CAN_UNSUBSCRIBE, 11])
                                + struct.pack("<II", 0xBEE, 0x3FFFFFFF))
    check(bad[2] == dev.ST_UNKNOWN_SUBSCRIPTION,
          "unsubscribing an unknown subscription MUST answer "
          "unknown_subscription")

    device.handle_control(bytes([dev.CAN_UNSUBSCRIBE, 1])
                          + struct.pack("<II", 0x0C0, 0x3FFFFFFF))
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

    # ---- GET_POWER reports what it measures, and only that --------------
    # SPEC.md 9.7. The two fields are independent because a device knows them
    # independently, and this asserts both ends of that: a device on its own
    # pack with a working gauge, and one on the ignition feed with no charge to
    # report at all.
    resp = device.handle_control(bytes([dev.GET_POWER, 6]))
    check(resp[2] == dev.ST_OK, "GET_POWER was refused")
    power = vtp1.decode_power_state(resp[3:])
    check(power["absent"] == [] and power["source_known"],
          "this build measures both, so GET_POWER MUST report both")
    check(power["percent"] <= 100,
          "SPEC.md 9.7 -- percent is 0..100 and a device MUST NOT emit a "
          "larger value")

    wired = dev.VtpDevice(now_us=lambda: 0)
    wired.set_power(source=dev.SRC_EXTERNAL)
    measured = vtp1.decode_power_state(
        wired.handle_control(bytes([dev.GET_POWER, 7]))[3:])
    check(measured["absent"] == ["percent"]
          and measured["source"] == dev.SRC_EXTERNAL,
          "a device on the ignition feed MUST clear the percent bit rather "
          "than reporting 100% for a battery it does not have -- and MUST "
          "still report the source it does know")
    check(measured["percent"] == 0,
          "SPEC.md 1.1 -- the byte behind a cleared validity bit MUST be zero, "
          "so a stale reading cannot leak onto the wire")

    check(device.handle_control(bytes([dev.GET_POWER, 8]) + b"\x00")[2]
          == dev.ST_BAD_PARAMS,
          "GET_POWER is parameterless; a trailing byte MUST be refused")

    # SPEC.md §9.2 — a frame matching several subscriptions is forwarded once,
    # governed by the most specific mask.
    device.handle_control(bytes([dev.CAN_RESET, 20]))
    broad = device.handle_control(bytes([dev.CAN_SUBSCRIBE_MASK, 21])
                                  + struct.pack("<IIBH", 0x0C0, 0x1FFFFF00,
                                                dev.SUB_EVERY_FRAME, 0))
    check(broad[2] == dev.ST_OK, "CAN_SUBSCRIBE_MASK was refused")
    device.handle_control(bytes([dev.CAN_SUBSCRIBE, 22])
                          + struct.pack("<IBH", 0x0C0, dev.SUB_PERIODIC, 500))
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
    # every_frame and the exact id says one per 500 ms; §9.2 makes the exact
    # one govern, so ~4 is right and ~100 means the wrong subscription won.
    check(len(got) < 60,
          f"{len(got)} frames in 2 s: the broad every_frame mask governed "
          f"instead of the more specific periodic subscription (SPEC.md §9.2)")

    # SPEC.md §6.8 — the first matching frame is forwarded in every mode, and
    # a mode value this version does not define is bad_params.
    device.handle_control(bytes([dev.CAN_RESET, 30]))
    unknown_mode = device.handle_control(bytes([dev.CAN_SUBSCRIBE, 31])
                                         + struct.pack("<IBH", 0x0C0, 2, 100))
    check(unknown_mode[2] == dev.ST_BAD_PARAMS,
          "mode 2 is unassigned in this version and MUST be bad_params, "
          "never silently substituted")

    device.handle_control(bytes([dev.CAN_SUBSCRIBE, 32])
                          + struct.pack("<IBH", 0x0C0, dev.SUB_PERIODIC, 5000))
    early = run(device, clock, 0.1)
    first_frames = [r for c, p in early if c == "can"
                    for r in (decode(c, p) or {"records": []})["records"]]
    check(len(first_frames) >= 1,
          "periodic held back the first matching frame: SPEC.md §6.8 forwards "
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

    # SPEC.md §9.1 — a reconnection inherits nothing.
    fresh.handle_control(bytes([dev.CAN_SUBSCRIBE, 1])
                         + struct.pack("<IBH", 0x0C0, dev.SUB_EVERY_FRAME, 0))
    fresh.on_connect()
    check(fresh.can_table() == [],
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

    # ...and it must be able to fail. A notification larger than the
    # negotiated ATT payload is one no link can carry, and is the check most
    # likely to matter on real hardware with an unexpected MTU.
    check(run_smoke(dict(captured, mtu=30,
                         info=vtp1.decode_info(smoke_dev.info()))),
          "smoketest.py passed a device whose notifications exceed the "
          "negotiated ATT payload (SPEC.md 2)")

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
    # SPEC.md 4.1 gives each qualifier bit a rule for when it is CLEAR, and a
    # device that declares them all can only ever demonstrate the other half.
    plain = dev.VtpDevice(
        now_us=lambda: 0,
        capabilities=dev.CAP_CAN | dev.CAP_CONTROL)   # no FD, no masks

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

    # SPEC.md 4.1 -- the largest CAN payload is derived from the bits, not
    # carried in a field. There is nothing left to disagree with, which is the
    # point: `can_max_payload` could only ever hold 0, 8 or 64 and the bits
    # already said which, so two rules covered a no-CAN device and gave
    # different answers.
    def payload_ceiling(info):
        caps = info["capabilities"]
        if not caps & dev.CAP_CAN:
            return 0
        return 64 if caps & dev.CAP_CAN_FD else 8

    check(payload_ceiling(vtp1.decode_info(plain.info())) == 8,
          "a CAN device without can_fd carries at most 8 payload bytes")
    fd = dev.VtpDevice(now_us=lambda: 0,
                       capabilities=dev.CAP_CAN | dev.CAP_CONTROL | dev.CAP_CAN_FD)
    check(payload_ceiling(vtp1.decode_info(fd.info())) == 64,
          "a CAN FD device carries up to 64")
    nocan = dev.VtpDevice(now_us=lambda: 0, capabilities=dev.CAP_GPS)
    check(payload_ceiling(vtp1.decode_info(nocan.info())) == 0,
          "a device with no CAN carries none")
    check(vtp1.decode_info(nocan.info())["obd_poll_slots"] == 0
          and vtp1.decode_info(nocan.info())["obd_min_interval_ms"] == 0,
          "both OBD capacities MUST be zero while bit 10 is clear "
          "(SPEC.md 4.1)")

    # SPEC.md 13.4 -- a device asking for more channels than fit in one
    # complete write is refused where the mistake is, not at the first
    # MONITOR_LIST.
    try:
        dev.VtpDevice(now_us=lambda: 0,
                      monitor_channels=[dev.CH_SPEED] * 16)
        check(False, "16 monitor channels MUST be refused at construction "
                     "(SPEC.md 13.4 allows 15)")
    except ValueError:
        pass
    check(len(dev.VtpDevice(now_us=lambda: 0,
                            monitor_channels=[dev.CH_SPEED] * 15)
              ._monitor_channels) == 15,
          "15 channels is the cap, and MUST be accepted")

    bare_clock = [0]

    # ---- A device does only what it says it does ------------------------
    # SPEC.md 4.1 and 9. Every opcode is owned by a capability, and a device
    # without the bit answers unsupported_opcode BEFORE looking at parameters.
    # A device declaring only `control` used to emit GPS and IMU notifications
    # and answer `ok` to CAN_SUBSCRIBE, GPS_SET_RATE, IMU_SET_RATE and
    # MONITOR_LIST -- the capability set said what a device MAY do and nothing
    # made it so.
    bare = dev.VtpDevice(now_us=lambda: bare_clock[0],
                         capabilities=dev.CAP_CONTROL)
    bare.on_connect()
    produced = []
    for _ in range(400):
        bare_clock[0] += 5_000
        produced += [name for name, _ in bare.poll()]
    check(not produced,
          f"a device declaring only `control` MUST notify on nothing; it "
          f"produced {sorted(set(produced))}")

    owned = {
        "CAN_RESET": bytes([dev.CAN_RESET, 1]),
        "CAN_SUBSCRIBE": bytes([dev.CAN_SUBSCRIBE, 2])
                         + struct.pack("<IBH", 0x1A0, 0, 0),
        "CAN_SUBSCRIBE_MASK": bytes([dev.CAN_SUBSCRIBE_MASK, 3])
                              + struct.pack("<IIBH", 0x1A0, 0x3FFFFFFF, 0, 0),
        "CAN_UNSUBSCRIBE": bytes([dev.CAN_UNSUBSCRIBE, 4])
                           + struct.pack("<II", 0x1A0, 0x3FFFFFFF),
        "GPS_SET_RATE": bytes([dev.GPS_SET_RATE, 6]) + struct.pack("<H", 5),
        "IMU_SET_RATE": bytes([dev.IMU_SET_RATE, 7]) + struct.pack("<H", 50),
        "MONITOR_LIST": bytes([dev.MONITOR_LIST, 8]),
        "GET_POWER": bytes([dev.GET_POWER, 12]),
        "OBD_INFO": bytes([dev.OBD_INFO, 13]),
        "OBD_POLL_SET": bytes([dev.OBD_POLL_SET, 14])
                        + struct.pack("<HB", 0, 0),
    }
    for name, request in owned.items():
        got = bare.handle_control(request)
        check(got[2] == dev.ST_UNSUPPORTED,
              f"{name} MUST answer unsupported_opcode on a device that has "
              f"not declared the capability owning it; got status {got[2]}")

    # ...and the one that belongs to no role still works, because it is about
    # the clock, which every device has.
    check(bare.handle_control(bytes([dev.TIME_SYNC, 9]))[2] == dev.ST_OK,
          "TIME_SYNC has no owning capability and MUST still be answered")

    # SPEC.md 9 -- availability is decided BEFORE parameters. A malformed
    # request for an opcode this device does not have is unsupported_opcode,
    # not bad_params: the two mean different things to a client, and getting
    # them the wrong way round either loops it forever or makes it give up on
    # a device that would have worked.
    malformed = bare.handle_control(bytes([dev.GPS_SET_RATE, 11]) + b"\x01")
    check(malformed[2] == dev.ST_UNSUPPORTED,
          f"a MALFORMED request for an unavailable opcode MUST still answer "
          f"unsupported_opcode, not bad_params; got status {malformed[2]}")

    # The same order one level down: an unsupported subscription MODE is
    # bad_params, but only once the opcode itself was available.
    check(bare.handle_control(
              bytes([dev.CAN_SUBSCRIBE, 12])
              + struct.pack("<IBH", 0x1A0, 2, 100))[2]
          == dev.ST_UNSUPPORTED,
          "on a device with no CAN at all, an unknown-mode subscription is "
          "unsupported_opcode -- the opcode was never available to carry the "
          "mode (SPEC.md 9)")

    # SPEC.md 4.1 -- monitor_values is inert without the bit.
    check(bare.handle_monitor_write(
              struct.pack("<HBB", 0, 1, 0) + struct.pack("<BBi", 0, 1, 5))
          is not None,
          "a device without the monitor bit MUST reject a value write; "
          "accepting it would put values on a display for a role it does not "
          "have")

    # ---- OBD polling: declare, verify, use (SPEC.md §15) ----------------
    # The first role whose device TRANSMITS. Everything asserted here is
    # about the boundary between the transmitter (the poll set) and the
    # receiver (subscriptions): the two are controlled separately, and no
    # response reaches the client except through an ordinary subscription.
    obd_clock = [0]
    car = dev.VtpDevice(now_us=lambda: obd_clock[0], mtu=247,
                        gps_hz=0, imu_hz=0)
    car.on_connect()

    def obd_ctl(op, tag, params=b""):
        return car.handle_control(bytes([op, tag]) + params)

    def obd_run(seconds):
        """Advance the clock and return every decoded CAN batch."""
        batches = []
        for _ in range(int(seconds * 1_000_000) // 5_000):
            obd_clock[0] += 5_000
            for ch, payload in car.poll():
                if ch == "can":
                    b = decode("can", car.stamp_seq(ch, payload))
                    car.commit_seq(ch)
                    if b is not None:
                        batches.append(b)
        return batches

    # SPEC.md 15.4 -- nothing is pollable before a probe: declare-verify-use
    # is structural, not convention.
    early = obd_ctl(dev.OBD_POLL_SET, 0x60,
                    struct.pack("<HB", 25, 1) + bytes([0x0C]))
    check(early[2] == dev.ST_BAD_PARAMS,
          "OBD_POLL_SET before OBD_INFO MUST answer bad_params (SPEC.md 15.4)")
    # ...but the stop is always available, whatever the probe state.
    check(obd_ctl(dev.OBD_POLL_SET, 0x61, struct.pack("<HB", 0, 0))[2]
          == dev.ST_OK, "the empty poll set MUST be accepted before a probe")

    # SPEC.md 15.2 -- the probe reports the synthetic car: 11-bit functional
    # addressing, both ECUs ascending, and the union of their masks.
    resp = obd_ctl(dev.OBD_INFO, 0x62)
    check(resp[:3] == bytes([dev.OBD_INFO, 0x62, dev.ST_OK]),
          "OBD_INFO MUST answer ok")
    probe = vtp1.decode_obd_info(resp[3:])
    check(probe["probe"]["request_id"] == car.OBD_REQUEST_ID,
          "the probe MUST report the identifier its requests went out on")
    check([e["id"] for e in probe["ecus"]] == sorted(car.OBD_ECUS),
          "the probe MUST list every answering ECU, strictly ascending")
    want_union = [0, 0, 0]
    for masks in car.OBD_ECUS.values():
        for i in range(3):
            want_union[i] |= masks[i]
    check((probe["probe"]["supported_01_20"],
           probe["probe"]["supported_21_40"],
           probe["probe"]["supported_41_60"]) == tuple(want_union),
          "the probe masks MUST be the union over answering ECUs "
          "(SPEC.md 15.3)")
    check(probe["absent"] == [],
          "a probe something answered has every gated field valid")

    # SPEC.md 15.5 -- the probe's mask responses were real bus frames, but
    # with no poll set and nothing subscribed there is no delivery path.
    # Their content reached the client in the OBD_INFO detail; the frames
    # themselves have nowhere to go yet.
    check(not obd_run(0.3),
          "probe responses have no delivery path before a poll set exists "
          "(SPEC.md 15.5)")

    # SPEC.md 15.4, 15.5 -- an accepted poll set is the WHOLE of what a
    # client must do to receive the answers: nothing is subscribed here, and
    # the fallback delivers on the probe's reported response identifiers.
    check(obd_ctl(dev.OBD_POLL_SET, 0x63,
                  struct.pack("<HB", 25, 1) + bytes([0x0C]))[2] == dev.ST_OK,
          "a probed, supported PID at a legal interval MUST be accepted")
    batches = obd_run(1.0)
    frames = [r for b in batches for r in b["records"]]
    check(frames,
          "a polling client MUST receive the answers with no subscription "
          "installed -- the fallback is the delivery path (SPEC.md 15.5)")
    check({r["id"] for r in frames} <= set(car.OBD_ECUS),
          "every delivered frame is on an ECU response id OBD_INFO reported")
    check(all(b["header"]["flags"] & 0x02 for b in batches),
          "every batch flushed while the poll set is non-empty MUST carry "
          "the polling flag (SPEC.md 15.6)")
    # At 25 ms per request over 1 s, 40 requests; 0x0C is answered by both
    # ECUs, so about 80 frames. Bounded loosely: the property is the rate
    # cap, not the exact count.
    check(30 <= len(frames) <= 90,
          f"one request per 25 ms for 1 s should yield 40 requests' worth "
          f"of answers; {len(frames)} frames arrived")
    # SPEC.md 15.5 -- the request identifier never appears: the stream
    # carries what the device hears, not what it says.
    check(car.OBD_REQUEST_ID not in {r["id"] for r in frames},
          "the device MUST NOT emit a can_record for its own request frames "
          "(SPEC.md 15.5)")
    # The responses are self-describing Mode 01 answers, and the engine
    # speed in them is the same motion state the GPS fix derives from.
    rpm_frames = [r for r in frames
                  if bytes.fromhex(r["payload"])[1:3] == b"\x41\x0c"
                  and r["id"] == 0x7E8]
    check(rpm_frames, "0x7E8 answered PID 0x0C with a 41 0C response")
    for r in rpm_frames[:5]:
        data = bytes.fromhex(r["payload"])
        got_rpm = ((data[3] << 8) | data[4]) / 4
        want_rpm = car.circuit.at(r["t_device_us"] / 1e6)["rpm"]
        check(abs(got_rpm - want_rpm) < 2,
              f"PID 0x0C decodes to {got_rpm} rpm; the circuit says "
              f"{want_rpm:.0f} at that instant")

    # SPEC.md 15.1 -- spacing is measured from the last transmission, so a
    # poll-set replacement mid-interval does not reset the clock: no two
    # requests, across the boundary included, may be closer than the
    # interval. Observed through the response timestamps, which are the
    # request ticks in this model.
    check(obd_ctl(dev.OBD_POLL_SET, 0x76,
                  struct.pack("<HB", 25, 1) + bytes([0x0C]))[2] == dev.ST_OK,
          "replacing the poll set with itself must answer ok")
    spaced = [r for b in obd_run(0.6) for r in b["records"]]
    ticks = sorted({r["t_device_us"] for r in spaced
                    if bytes.fromhex(r["payload"])[1:3] == b"\x41\x0c"})
    check(len(ticks) >= 2, "the spacing check needs at least two requests")
    check(all(b - a >= 25_000 for a, b in zip(ticks, ticks[1:])),
          "two requests closer than interval_ms apart: a replacement MUST "
          "NOT reset the spacing clock (SPEC.md 15.1)")

    # SPEC.md 15.5 -- the fallback is not an entry in the table: it holds no
    # slot and CAN_UNSUBSCRIBE cannot name it.
    check(obd_ctl(dev.CAN_UNSUBSCRIBE, 0x80,
                  struct.pack("<II", 0x7E8, 0x3FFFFFFF))[2]
          == dev.ST_UNKNOWN_SUBSCRIPTION,
          "CAN_UNSUBSCRIBE naming a fallback-delivered id MUST answer "
          "unknown_subscription; the client never installed anything "
          "(SPEC.md 15.5)")

    # SPEC.md 15.5 -- the fallback yields to the table. A periodic
    # subscription installed on one response identifier governs those
    # frames; the other ECU's answers stay every_frame underneath.
    check(obd_ctl(dev.CAN_SUBSCRIBE, 0x81,
                  struct.pack("<IBH", 0x7E8, dev.SUB_PERIODIC, 400))[2]
          == dev.ST_OK, "a periodic subscription on a response id must install")
    obd_run(0.3)      # drain frames admitted before the subscription governed
    governed = [r for b in obd_run(1.0) for r in b["records"]]
    n_7e8 = sum(1 for r in governed if r["id"] == 0x7E8)
    n_7e9 = sum(1 for r in governed if r["id"] == 0x7E9)
    check(n_7e8 <= 5,
          f"0x7E8 is governed by a 400 ms periodic subscription and MUST be "
          f"rate-limited; {n_7e8} frames arrived in 1 s")
    check(n_7e9 >= 20,
          f"0x7E9 matches nothing installed and stays every_frame under the "
          f"fallback; only {n_7e9} frames arrived in 1 s")
    check(obd_ctl(dev.CAN_UNSUBSCRIBE, 0x82,
                  struct.pack("<II", 0x7E8, 0x3FFFFFFF))[2] == dev.ST_OK,
          "removing the periodic subscription must succeed")

    # SPEC.md 15.4 -- a refused request leaves the installed set unchanged.
    check(obd_ctl(dev.OBD_POLL_SET, 0x65,
                  struct.pack("<HB", dev.OBD_MIN_INTERVAL_MS - 1, 1)
                  + bytes([0x0C]))[2] == dev.ST_BAD_PARAMS,
          "an interval below obd_min_interval_ms MUST answer bad_params")
    check(obd_run(0.2),
          "a refused OBD_POLL_SET MUST leave the previous set polling")

    # SPEC.md 15.4 -- the rest of the refusal table.
    check(obd_ctl(dev.OBD_POLL_SET, 0x66,
                  struct.pack("<HB", 25, dev.OBD_POLL_SLOTS + 1)
                  + bytes([0x0C]) * (dev.OBD_POLL_SLOTS + 1))[2]
          == dev.ST_TABLE_FULL,
          "more PIDs than obd_poll_slots MUST answer table_full")
    check(obd_ctl(dev.OBD_POLL_SET, 0x67,
                  struct.pack("<HB", 25, 1) + bytes([0x02]))[2]
          == dev.ST_BAD_PARAMS,
          "a PID the probe's union does not claim MUST answer bad_params")
    check(obd_ctl(dev.OBD_POLL_SET, 0x68,
                  struct.pack("<HB", 25, 1) + bytes([0x7F]))[2]
          == dev.ST_BAD_PARAMS,
          "a PID above 0x60 MUST answer bad_params (SPEC.md 15.4)")
    check(obd_ctl(dev.OBD_POLL_SET, 0x69,
                  struct.pack("<HB", 25, 2) + bytes([0x0C]))[2]
          == dev.ST_BAD_PARAMS,
          "a count disagreeing with the PID bytes present MUST answer "
          "bad_params")
    check(obd_ctl(dev.OBD_POLL_SET, 0x6A, struct.pack("<HB", 25, 0))[2]
          == dev.ST_BAD_PARAMS,
          "the empty set MUST carry interval_ms 0 (SPEC.md 15.4)")

    # SPEC.md 15.7 -- the empty set stops the transmitter, and the polling
    # flag's falling edge is on the wire: a batch flushed after the stop
    # carries it clear.
    check(obd_ctl(dev.OBD_POLL_SET, 0x6B, struct.pack("<HB", 0, 0))[2]
          == dev.ST_OK, "the empty poll set is the stop and MUST answer ok")
    drained = obd_run(0.5)
    # SPEC.md 15.7 -- the stop flushes what was already accepted rather than
    # stranding it, and it flushes BEFORE the set clears, so that one batch
    # legitimately carries the polling flag. Everything after it must not.
    check(all(not b["header"]["flags"] & 0x02 for b in drained[1:]),
          "a batch flushed after the stop's own flush MUST carry the polling "
          "flag clear")
    check(not [r for b in drained[1:] for r in b["records"]],
          "no new response may arrive after the stop; the transmitter is off "
          "and nothing is stranded for a later subscription to surface "
          "(SPEC.md 15.7)")

    # SPEC.md 15.7 -- CAN_RESET clears the poll set along with the table:
    # the one opcode that clears the receiver clears the transmitter too.
    check(obd_ctl(dev.OBD_POLL_SET, 0x6C,
                  struct.pack("<HB", 25, 1) + bytes([0x0C]))[2] == dev.ST_OK,
          "re-arming the poll set after a stop must succeed")
    check(obd_ctl(dev.CAN_RESET, 0x6D)[2] == dev.ST_OK, "CAN_RESET answers ok")
    check(obd_ctl(dev.CAN_SUBSCRIBE_MASK, 0x6E,
                  struct.pack("<IIBH", 0x7E8, 0x3FFFFFF8, 0, 0))[2]
          == dev.ST_OK, "resubscribing after CAN_RESET must succeed")
    check(not obd_run(0.5),
          "CAN_RESET MUST clear the poll set: even with a subscription "
          "installed, no new OBD_POLL_SET means no transmitter and nothing "
          "on the bus to forward (SPEC.md 15.7)")
    # ...but the probe result is a fact about the car, not the poll set, so
    # a re-arm without a second probe still works on this connection.
    check(obd_ctl(dev.OBD_POLL_SET, 0x6F,
                  struct.pack("<HB", 25, 1) + bytes([0x0C]))[2] == dev.ST_OK,
          "the probe result survives CAN_RESET; only the poll set clears")

    # SPEC.md 15.2 -- a probe nothing answered clears the poll set with the
    # probe result it replaces: the set was verified against a car that has
    # stopped answering, and without this rule the device transmits into
    # silence with the fallback's delivery path already dead.
    car.OBD_ECUS = {}          # the gateway closes mid-session
    silent = obd_ctl(dev.OBD_INFO, 0x77)
    check(silent[2] == dev.ST_OK
          and vtp1.decode_obd_info(silent[3:])["probe"]["count"] == 0,
          "a mid-session silent probe answers ok with `responded` clear")
    quiet = obd_run(0.6)
    check(not [r for b in quiet[1:] for r in b["records"]],
          "a probe nothing answered MUST clear the poll set: after its own "
          "flush, nothing may be transmitted or delivered (SPEC.md 15.7)")
    check(obd_ctl(dev.OBD_POLL_SET, 0x78,
                  struct.pack("<HB", 25, 1) + bytes([0x0C]))[2]
          == dev.ST_BAD_PARAMS,
          "the silent probe replaced the probe result too, so nothing is "
          "pollable until a probe answers again (SPEC.md 15.4)")
    del car.OBD_ECUS           # back to the class's car

    # SPEC.md 15.7 -- transmit never survives the link.
    car.on_disconnect()
    car.on_connect()
    check(obd_ctl(dev.OBD_POLL_SET, 0x70,
                  struct.pack("<HB", 25, 1) + bytes([0x0C]))[2]
          == dev.ST_BAD_PARAMS,
          "a reconnect clears the probe result with the poll set, so the "
          "next connection starts at declare-verify-use again")

    # A gatewayed car: the probe transmits, nothing answers, and `responded`
    # clear with every gated field absent is the honest report -- not an
    # empty mask, which would claim a car that supports no PIDs.
    gated = dev.VtpDevice(now_us=lambda: obd_clock[0], gps_hz=0, imu_hz=0)
    gated.OBD_ECUS = {}
    gated.on_connect()
    resp = gated.handle_control(bytes([dev.OBD_INFO, 0x71]))
    silent = vtp1.decode_obd_info(resp[3:])
    check(silent["probe"]["count"] == 0 and silent["ecus"] == [],
          "a silent probe lists no ECUs")
    check(set(silent["absent"]) == {"request_id", "supported_01_20",
                                    "supported_21_40", "supported_41_60"},
          "a silent probe reports every gated field absent (SPEC.md 15.2)")
    check(gated.handle_control(
              bytes([dev.OBD_POLL_SET, 0x72],)
              + struct.pack("<HB", 25, 1) + bytes([0x0C]))[2]
          == dev.ST_BAD_PARAMS,
          "nothing is pollable on a car that answered nothing")

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
