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
    device.handle_control(bytes([dev.CAN_UNSUBSCRIBE, 1])
                          + struct.pack("<I", 0x0C0))
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

    resp = device.handle_control(bytes([0xEE, 6]))
    check(resp[2] == dev.ST_UNSUPPORTED,
          "an unimplemented opcode MUST answer unsupported_opcode, and MUST "
          "still be answered")

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
