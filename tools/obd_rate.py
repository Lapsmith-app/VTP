#!/usr/bin/env python3
"""Measure the per-PID sample rate of an OBD poll set against the reference
peripheral, ungrouped and grouped (SPEC.md 15.4.1).

The point of measuring rather than asserting: SPEC.md 15.4 says one PID in a
schedule of *g* groups is sampled every *g* x `interval_ms`, and that is a
claim about the transmitter. What a CLIENT gets is what arrives in CAN
batches, after the delivery fallback, batching, and whatever the car chose to
answer -- including an ECU that implements only part of a group. This drives
the device on an injected clock and counts what actually lands.

    python3 tools/obd_rate.py [--seconds 10] [--interval-ms 20]
"""
import argparse
import os
import struct
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, os.pardir, "reference", "peripheral"))
sys.path.insert(0, os.path.join(_HERE, os.pardir, "reference", "python"))

import vtp_device as dev            # noqa: E402
import vtp1                         # noqa: E402

TICK_US = 5_000                     # the peripheral's own 200 Hz service rate

#: A representative lap-timing set: four two-byte PIDs and eight one-byte,
#: every one of them inside the synthetic car's supported union so the two
#: runs poll exactly the same PIDs and the only variable is how they group.
RACE_SET = [0x0C, 0x0D, 0x04, 0x05, 0x11, 0x0F,
            0x10, 0x42, 0x1F, 0x0B, 0x33, 0x2F]

#: Response bytes each PID costs inside a grouped answer: the `pid` echo plus
#: its J1979 data bytes. This table lives in the CLIENT, which is exactly
#: where SPEC.md 15.5 puts it -- the device has no such table and must not.
PID_RESPONSE_BYTES = {
    0x04: 2, 0x05: 2, 0x0B: 2, 0x0C: 3, 0x0D: 2, 0x0F: 2,
    0x10: 3, 0x11: 2, 0x1F: 3, 0x2F: 2, 0x33: 2, 0x42: 3,
}

#: Six: a single frame carries seven data bytes and one is the `41` echo.
GROUP_BUDGET = 6


def pack_greedily(pids):
    """Group PIDs first-fit into six-byte responses, preserving order.

    Deliberately dumb -- a client can do better by reordering -- because the
    measurement should show what the obvious implementation gets, not what a
    tuned one does.
    """
    groups, run, used = [], [], 0
    for pid in pids:
        cost = PID_RESPONSE_BYTES[pid]
        if run and (used + cost > GROUP_BUDGET or len(run) >= dev.OBD_MAX_GROUP):
            groups.append(run)
            run, used = [], 0
        run.append(pid)
        used += cost
    if run:
        groups.append(run)
    return groups


def encode(groups):
    """SPEC.md 15.4.1 -- bit 7 set on every PID but the last of its group."""
    out = bytearray()
    for group in groups:
        for i, pid in enumerate(group):
            out.append(pid | (dev.OBD_PID_MORE if i < len(group) - 1 else 0))
    return bytes(out)


def classify(payload):
    """`(kind, pids)` for one frame the client was delivered.

    A grouped answer is `41` then one `pid`+`data` pair per PID, so the
    client walks it with the same table it decodes with.

    `first_frame` is the answer to a group whose response did not fit: a high
    nibble of 1 in byte 0. It yields nothing and never will -- SPEC.md 15.1
    forbids the device the flow control that would continue the transfer, so
    it is dead on arrival, which is the whole cost of sizing a group wrong.

    `other` is a real frame this client did not ask after and is counted
    separately rather than as damage: the probe's own mask responses ride
    SPEC.md 15.5's fallback once a poll set is active, and so does anything
    another tester puts on these identifiers.
    """
    if not payload:
        return "other", ()
    if payload[0] >> 4 == 1:
        return "first_frame", ()
    if payload[0] >> 4 != 0 or len(payload) < 2 or payload[1] != 0x41:
        return "other", ()
    body, out, i = payload[2:payload[0] + 1], [], 0
    while i < len(body):
        pid = body[i]
        if pid not in PID_RESPONSE_BYTES:
            return "other", tuple(out)
        out.append(pid)
        i += PID_RESPONSE_BYTES[pid]
    return "mode01", tuple(out)


def run(car, clock, seconds):
    """Advance the injected clock and count every PID answer that lands.

    Counted per (pid, bus-arrival instant), not per frame. Functional
    addressing means both ECUs answer a PID they both implement, so counting
    frames would report engine speed at twice the rate the client can
    actually observe it change -- two copies of one sample are one sample.
    """
    samples, kinds = set(), Counter()
    for _ in range(int(seconds * 1_000_000) // TICK_US):
        clock[0] += TICK_US
        for ch, payload in car.poll():
            if ch != "can":
                continue
            batch = vtp1.decode_can_batch(car.stamp_seq(ch, payload))
            car.commit_seq(ch)
            for record in batch["records"]:
                kind, pids = classify(bytes.fromhex(record["payload"]))
                kinds[kind] += 1
                for pid in pids:
                    samples.add((pid, record["t_device_us"]))
    return Counter(pid for pid, _ in samples), kinds


def measure(label, pids_payload, groups, seconds, interval_ms):
    clock = [0]
    car = dev.VtpDevice(now_us=lambda: clock[0], mtu=247, gps_hz=0, imu_hz=0)
    car.on_connect()

    # Declare, verify, use: nothing is pollable until a probe has answered.
    assert car.handle_control(bytes([dev.OBD_INFO, 0x01])) is dev.RESPONSE_PENDING
    for _ in range(400):
        clock[0] += TICK_US
        if car.due_control_response() is not None:
            break
    else:
        raise SystemExit("the probe never completed")

    request = (bytes([dev.OBD_POLL_SET, 0x02])
               + struct.pack("<HB", interval_ms, len(pids_payload))
               + pids_payload)
    status = car.handle_control(request)[2]
    if status != dev.ST_OK:
        raise SystemExit(f"{label}: OBD_POLL_SET refused, status {status}")

    seen, kinds = run(car, clock, seconds)
    rates = {pid: seen[pid] / seconds for pid in RACE_SET}
    worst = min(rates.values())
    note = f"{sum(kinds.values())} CAN records, {kinds['other']} not ours"
    if kinds["first_frame"]:
        note += f", {kinds['first_frame']} DEAD first frames"
    print(f"  {label:<22} {len(groups):>2} groups  "
          f"cycle {len(groups) * interval_ms:>4} ms  "
          f"{worst:5.2f} Hz per PID   ({note})")
    return worst, rates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--interval-ms", type=int, default=None)
    ap.add_argument("--min-interval-ms", type=int, default=None,
                    help="Override the device's declared floor, to size what "
                         "hardware answering faster than the reference "
                         "peripheral could offer. 20 ms is this build's "
                         "declaration, not a constant of the specification.")
    args = ap.parse_args()

    if args.min_interval_ms is not None:
        dev.OBD_MIN_INTERVAL_MS = args.min_interval_ms
    interval = (args.interval_ms if args.interval_ms is not None
                else dev.OBD_MIN_INTERVAL_MS)

    grouped = pack_greedily(RACE_SET)
    single = [[pid] for pid in RACE_SET]
    # What a client that read only J1979's "up to six PIDs" and never counted
    # response bytes would send. It is legal -- rule 6 passes -- and it is
    # exactly the mistake SPEC.md 15.4.1 leaves the client free to make.
    overpacked = [RACE_SET[i:i + 6] for i in range(0, len(RACE_SET), 6)]

    print(f"\n{len(RACE_SET)} PIDs, interval_ms={interval}, "
          f"{args.seconds:g}s of device time, "
          f"obd_min_interval_ms={dev.OBD_MIN_INTERVAL_MS}\n")
    print("  grouping: "
          + "  ".join("(" + " ".join(f"{p:02X}" for p in g) + ")"
                      for g in grouped) + "\n")

    base, _ = measure("ungrouped (today)", bytes(RACE_SET), single,
                      args.seconds, interval)
    fast, rates = measure("grouped (15.4.1)", encode(grouped), grouped,
                          args.seconds, interval)
    measure("overpacked (6/group)", encode(overpacked), overpacked,
            args.seconds, interval)

    print(f"\n  gain: {fast / base:.2f}x  ({base:.2f} Hz -> {fast:.2f} Hz)\n")
    print("  per-PID, grouped:")
    for pid in RACE_SET:
        print(f"    {pid:02X}  {rates[pid]:5.2f} Hz")
    print()


if __name__ == "__main__":
    main()
