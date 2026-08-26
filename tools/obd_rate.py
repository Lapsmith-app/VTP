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

TICK_US = 5_000
LATENCY_US = [0]                     # the peripheral's own 200 Hz service rate

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


def verify_table(car, now_s=0.0):
    """Check PID_RESPONSE_BYTES against what the car actually emits.

    The table above is the CLIENT's, and SPEC.md 15.5 is why: the device
    holds no PID sizes and must not. But a client table that silently drifts
    from the car does not fail loudly -- it changes the measurement. One
    wrong entry (0x33 as 3 rather than 2) made the packer emit six groups
    instead of five and this tool report 8.20 Hz where the truth was 9.80,
    with no error anywhere. In the other direction the walk in classify()
    steps into the middle of a pair, reads a data byte as a PID, and
    undercounts.

    So the one number this tool exists to produce is guarded by comparing
    the table against the car's own answers before anything is measured.
    Nothing is derived FROM the car -- that would defeat the point, which is
    that clients size groups themselves -- only checked against it.
    """
    st = car.circuit.at(now_s)
    wrong = []
    for pid in sorted(set(RACE_SET)):
        if pid not in PID_RESPONSE_BYTES:
            wrong.append(f"PID {pid:02X} is polled but has no size in "
                         f"PID_RESPONSE_BYTES")
            continue
        actual = 1 + len(car._obd_pid_data(pid, st))
        if actual != PID_RESPONSE_BYTES[pid]:
            wrong.append(f"PID {pid:02X}: the table says {PID_RESPONSE_BYTES[pid]} "
                         f"response bytes, the car emits {actual}")
    if wrong:
        raise SystemExit(
            "the client PID size table disagrees with the car, so every rate "
            "below would be wrong in a way nothing reports:\n  "
            + "\n  ".join(wrong))


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


def encode(groups, min_ms=0):
    """SPEC.md 15.4.1 -- bit 7 set on every PID but the last of its group,
    then that group's u16 minimum interval (SPEC.md 15.4.2)."""
    out = bytearray()
    for group in groups:
        for i, pid in enumerate(group):
            out.append(pid | (dev.OBD_PID_MORE if i < len(group) - 1 else 0))
        out += struct.pack("<H", min_ms)
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
    # PCI is the Mode 01 response length: `41` plus the pairs. A frame whose
    # PCI runs past the eight bytes it arrived in is not a frame to read.
    pci = payload[0]
    if pci < 1 or 1 + pci > len(payload):
        return "other", ()
    body, out, i = payload[2:1 + pci], [], 0
    while i < len(body):
        pid = body[i]
        size = PID_RESPONSE_BYTES.get(pid)
        # Atomically, and never partially: a walk that stopped mid-body used
        # to return ("other", pids-so-far) and run() counted those PIDs
        # anyway, so a frame this parser had REJECTED still moved the rate it
        # was supposed to be measuring. A pair that runs past the body is the
        # same failure one byte later.
        if size is None or i + size > len(body):
            return "other", ()
        out.append(pid)
        i += size
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
                if kind != "mode01":
                    continue
                for pid in pids:
                    samples.add((pid, record["t_device_us"]))
    return Counter(pid for pid, _ in samples), kinds


def measure(label, pids_payload, groups, seconds, interval_ms):
    n_pids = sum(len(g) for g in groups)
    clock = [0]
    car = dev.VtpDevice(now_us=lambda: clock[0], mtu=247, gps_hz=0,
                        imu_hz=0, obd_latency_us=LATENCY_US[0])
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
               + struct.pack("<HB", interval_ms, n_pids)
               + pids_payload)
    status = car.handle_control(request)[2]
    if status != dev.ST_OK:
        raise SystemExit(f"{label}: OBD_POLL_SET refused, status {status}")

    seen, kinds = run(car, clock, seconds)
    rates = {pid: seen[pid] / seconds for pid in RACE_SET}
    worst, best = min(rates.values()), max(rates.values())
    dead = sum(1 for pid in RACE_SET if not rates[pid])
    note = f"{sum(kinds.values())} CAN records, {kinds['other']} not ours"
    if kinds["first_frame"]:
        note += f", {kinds['first_frame']} DEAD first frames"
    # min AND max, because the minimum alone hid a real result: under the
    # overpacked set two PIDs still arrive at full rate -- 0x7E9 implements
    # only its own subset of each group, and that subset fits a single frame
    # -- while the other ten get nothing. "0.00 Hz per PID" was true of the
    # worst PID and false as a description of what the client receives.
    span = (f"{worst:5.2f} Hz per PID" if worst == best
            else f"{worst:5.2f}-{best:5.2f} Hz per PID")
    if dead:
        span += f", {dead}/{len(RACE_SET)} silent"
    # Under SPEC.md 15.4 the spacing is max(interval_ms, the car), so the
    # cycle is not `groups x interval_ms` -- with interval 0 that reads 0 ms
    # for a loop plainly doing real work. Reported from what arrived.
    cycle = 1000.0 / best if best else float("inf")
    print(f"  {label:<22} {len(groups):>2} groups  "
          f"cycle {cycle:>5.0f} ms  {span:<32} ({note})")
    return worst, rates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--interval-ms", type=int, default=None)
    ap.add_argument("--latency-ms", type=float, default=0.0,
                    help="How long the synthetic car takes to answer. Zero "
                         "answers in the same tick, which makes SPEC.md "
                         "15.4's response pacing invisible: interval_ms is "
                         "then the only thing pacing the loop.")
    args = ap.parse_args()

    interval = args.interval_ms if args.interval_ms is not None else 0
    LATENCY_US[0] = int(args.latency_ms * 1000)

    # Before anything is measured, and before the packer reads the table.
    probe_car = dev.VtpDevice(now_us=lambda: 0, mtu=247, gps_hz=0, imu_hz=0)
    verify_table(probe_car)

    grouped = pack_greedily(RACE_SET)
    single = [[pid] for pid in RACE_SET]
    # What a client that read only J1979's "up to six PIDs" and never counted
    # response bytes would send. It is legal -- rule 6 passes -- and it is
    # exactly the mistake SPEC.md 15.4.1 leaves the client free to make.
    overpacked = [RACE_SET[i:i + 6] for i in range(0, len(RACE_SET), 6)]

    print(f"\n{len(RACE_SET)} PIDs, interval_ms={interval} (a MINIMUM; "
          f"0 = none), car answers in {args.latency_ms:g} ms, "
          f"{args.seconds:g}s of device time\n")
    print("  grouping: "
          + "  ".join("(" + " ".join(f"{p:02X}" for p in g) + ")"
                      for g in grouped) + "\n")

    base, _ = measure("ungrouped", encode(single), single,
                      args.seconds, interval)
    fast, rates = measure("grouped (15.4.1)", encode(grouped), grouped,
                          args.seconds, interval)
    _, over_rates = measure("overpacked (6/group)", encode(overpacked),
                            overpacked, args.seconds, interval)

    print(f"\n  gain: {fast / base:.2f}x  ({base:.2f} Hz -> {fast:.2f} Hz)\n")
    print(f"  {'PID':<5}{'grouped':>10}{'overpacked':>13}")
    for pid in RACE_SET:
        over = over_rates[pid]
        print(f"    {pid:02X}  {rates[pid]:8.2f} Hz {over:8.2f} Hz"
              + ("" if over else "   <- silent"))
    print()


if __name__ == "__main__":
    main()
