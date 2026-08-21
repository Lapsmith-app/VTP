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
    """Advance the injected clock and collect everything the device emits."""
    out = []
    for _ in range(int(seconds * 1_000_000 // step_us)):
        clock[0] += step_us
        out.extend(device.poll())
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
    check(table["entries"][0]["mask"] == 0x1FFFFFFF,
          "CAN_SUBSCRIBE MUST be recorded as a mask of 0x1FFFFFFF")

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

    resp = device.handle_control(bytes([dev.TIME_SYNC, 4])
                                 + struct.pack("<q", 1_766_000_000_000))
    check(resp[2] == dev.ST_OK and len(resp) == 11,
          "TIME_SYNC must answer ok and echo the device clock")

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
    check(first_gps and first_gps[0]["seq"] == 1,
          "seq MUST start from 0 and reach 1 on the first notification of a "
          "connection, so a client never confuses a reconnection with a wrap")

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
    check(reconnected and reconnected[0]["seq"] == 1,
          "seq MUST restart at 0 on a new connection")

    # ---- Monitor: the client supplies, the device displays (§13) --------
    mon = dev.VtpDevice(now_us=lambda: clock[0], mtu=247, gps_hz=0, imu_hz=0)
    info2 = vtp1.decode_info(mon.info())
    check(info2["capabilities"] & (1 << 3),
          "a device implementing Monitor MUST declare capability bit 3")

    listing = mon.handle_control(bytes([dev.MONITOR_LIST, 1])
                                 + struct.pack("<H", 0))
    check(listing[2] == dev.ST_OK, "MONITOR_LIST was refused")
    table = vtp1.decode_monitor_list(listing[3:])
    check(table["page"]["total"] == 4,
          f"expected 4 requested channels, got {table['page']['total']}")
    check(all(e["channel_known"] for e in table["entries"]),
          "the device asked for a channel the reference decoder cannot name")

    # Before anything is supplied, every slot renders unavailable.
    check(all(line.endswith("--") for line in mon.display_lines()),
          f"a device MUST NOT display a value nobody supplied: "
          f"{mon.display_lines()}")

    # Mid first lap: elapsed is known; last lap and delta do not exist yet.
    import vtp1_encode as menc
    update = menc.encode_monitor_update(
        {"seq": 1, "count": 3, "reserved": 0},
        [{"slot": 0, "validity": dev.MONITOR_PRESENT, "value": 42_318},
         {"slot": 1, "validity": 0, "value": 0},
         {"slot": 2, "validity": 0, "value": 0}])
    check(mon.handle_monitor_write(update) is None,
          "a well-formed monitor update was rejected")
    lines = mon.display_lines()
    check(lines[0] == "LAP: 42318", f"elapsed lap time not displayed: {lines}")
    check(lines[1].endswith("--") and lines[2].endswith("--"),
          f"a slot whose present bit is clear MUST render unavailable, not 0: "
          f"{lines}")

    # A cleared present bit with a stale value in the bytes: the bit governs.
    stale = menc.encode_monitor_update(
        {"seq": 2, "count": 1, "reserved": 0},
        [{"slot": 1, "validity": 0, "value": 87_340}])
    mon.handle_monitor_write(stale)
    check(mon.display_lines()[1].endswith("--"),
          "a stale value behind a cleared present bit MUST NOT be displayed")

    # A slot the device never asked for is ignored, not an error.
    stray = menc.encode_monitor_update(
        {"seq": 3, "count": 1, "reserved": 0},
        [{"slot": 200, "validity": dev.MONITOR_PRESENT, "value": 5}])
    check(mon.handle_monitor_write(stray) is None,
          "a value for an unrequested slot MUST be ignored, not rejected")

    check(mon.handle_monitor_write(update[:-1]) is not None,
          "a truncated monitor update MUST be rejected, not partly applied")

    # A reconnection starts blank rather than showing the last session.
    mon.on_connect()
    check(all(line.endswith("--") for line in mon.display_lines()),
          "a reconnection MUST clear the display, not inherit the previous "
          "connection's values")

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
