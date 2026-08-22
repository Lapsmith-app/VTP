#!/usr/bin/env python3
"""Connect to a real VTP/1 device over a real radio and check it end to end.

Everything else in this repository is tested without a Bluetooth adapter, on
purpose: conformance/run.py decodes byte vectors, selftest.py drives the device
model, and transport_selftest.py drives the pump against a fake GATT link. That
covers the protocol thoroughly and covers the radio not at all.

This is the missing half. It is a CLIENT — the direction nothing else in this
repository takes — and it exercises exactly the things a fake link cannot:

  * discovery by service UUID, and the advertisement actually reaching a scan
  * a real ATT MTU negotiation, and batches that fit inside it
  * CCCD writes on three characteristics of a real stack
  * an indication arriving on Control after a write, on a real link
  * timestamps from one device clock arriving in real time
  * a reconnection, where SPEC.md 8.2 restarts `seq`

Run the peripheral on one machine and this on another. Both commands are from
`reference/peripheral`, so the install and the script agree about where they
are:

    cd reference/peripheral

    python3 serve.py --no-display                          # machine A

    pip install -r requirements-client.txt                 # machine B
    python3 smoketest.py

`bleak` is a central-role library; `bless`, which serve.py uses, is the
peripheral one. They are different roles and this needs the other end of the
link from everything else here.

**Pairing.** SPEC.md 10 lets a device require an encrypted link, and
`serve.py` defaults to requiring it on everything except Info. macOS pairs on
demand when an encrypted characteristic is first touched. Linux and Windows
generally do not: BlueZ answers "Insufficient Authentication" and bleak raises
rather than prompting, so pair the two machines FIRST --
`bluetoothctl pair <address>` on Linux, or Windows Settings > Bluetooth &
devices > Add device. This script says so when it sees that error rather than
reporting a protocol fault. `serve.py --encrypt none` skips the whole question
for a first bring-up.

Exit status is 0 only if every check passed. It prints what it saw either way,
because "it did not work" is not a useful bug report and this is the test most
likely to be run by someone holding unfamiliar hardware.
"""
import argparse
import asyncio
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "reference" / "python"))

import vtp1                                          # noqa: E402

UUIDS = json.loads((ROOT / "schema" / "uuids.json").read_text())
SERVICE = UUIDS["service"]["vtp1"]
CHAR = UUIDS["characteristics"]

problems = []
notes = []


def check(ok, why):
    if ok:
        return True
    problems.append(why)
    print(f"  FAIL {why}")
    return False


def note(text):
    notes.append(text)
    print(f"  ---- {text}")


# What BlueZ, WinRT and CoreBluetooth each say when a characteristic needs an
# encrypted link and the two machines have never paired. Matched on text
# because none of the three raises a distinguishable exception type through
# bleak, and reporting this as a protocol fault sends someone hunting a bug in
# their device that is really a missing pairing.
PAIRING_HINTS = ("insufficient authentication", "insufficient encryption",
                 "not paired", "authentication", "0x05", "0x0f",
                 "access denied", "gatt operation not permitted")


def looks_like_pairing(exc):
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(hint in text for hint in PAIRING_HINTS)


def pairing_advice(what, exc):
    note(f"{what} failed with {exc!r}")
    note("this reads like an unpaired link rather than a device fault. "
         "SPEC.md 10 lets a device require encryption and serve.py does by "
         "default; macOS pairs on demand, Linux and Windows generally do not.")
    note("  Linux:   bluetoothctl pair <address>, then re-run")
    note("  Windows: Settings > Bluetooth & devices > Add device")
    note("  or, for a first bring-up: serve.py --encrypt none")


async def collect(client, sinks, seconds):
    """Subscribe to every named stream, gather for `seconds`, then stop.

    All of them at once, and this is not a convenience. Collecting them one
    after another made SPEC.md 8.1's central promise untestable and then
    tested it anyway: three streams gathered in series have three disjoint
    device-clock windows, so the overlap check below failed on a device that
    was behaving perfectly. Sharing a clock is only observable while they are
    sharing a wall-clock second.
    """
    for name, sink in sinks.items():
        try:
            await client.start_notify(
                CHAR[name],
                lambda _h, data, sink=sink: sink.append(bytes(data)))
        except Exception as exc:                       # noqa: BLE001
            if looks_like_pairing(exc):
                pairing_advice(f"enabling notifications on {name}", exc)
                raise SystemExit(1)
            raise
    await asyncio.sleep(seconds)
    for name in sinks:
        try:
            await client.stop_notify(CHAR[name])
        except Exception as exc:                       # noqa: BLE001
            note(f"{name}: stop_notify raised {exc!r}; harmless on disconnect")


async def one_connection(BleakClient, address, args, first):
    """Everything checked on a single link. Run twice; `first` is the label."""
    gps, can, imu = [], [], []
    async with BleakClient(address) as client:
        print(f"\n[{first}] connected to {address}")

        # ---- Info, before anything else --------------------------------
        # SPEC.md 10.2 leaves Info readable under every encryption posture, so
        # a failure HERE is not a pairing problem -- it is a device that has
        # protected the one characteristic that identifies it.
        try:
            raw = bytes(await client.read_gatt_char(CHAR["info"]))
        except Exception as exc:                       # noqa: BLE001
            if looks_like_pairing(exc):
                note("Info itself is encrypted, which SPEC.md 10.2 says to "
                     "avoid: a client that cannot pair can then not even "
                     "identify what it found.")
                pairing_advice("reading Info", exc)
                raise SystemExit(1)
            raise
        try:
            info = vtp1.decode_info(raw)
        except vtp1.Reject as exc:
            check(False, f"the device's Info did not decode: {exc} ({raw.hex()})")
            return None
        note(f"Info: minor {info['protocol_minor']}, capabilities "
             f"0x{info['capabilities']:X}, max_notify_bytes "
             f"{info['max_notify_bytes']}")
        check(info["protocol_major"] == 1,
              f"protocol_major is {info['protocol_major']}, not 1")

        # SPEC.md 4.1 -- decode_info already enforces the matrix, so reaching
        # here means it held. Say so, because a client author reading this
        # output wants to know the check ran.
        note("Info satisfies the SPEC.md 4.1 capability matrix")

        caps = info["capabilities"]
        has = {name: bool(caps & (1 << bit))
               for name, bit in vtp1.CAP_BIT.items()}

        # ---- The MTU the stack actually negotiated ---------------------
        #
        # BlueZ reports 23 through this bleak property whatever the link
        # actually negotiated -- it is the ATT default, not a measurement --
        # so treating it as one failed every healthy Linux link. Only CoreBluetooth
        # and WinRT give a real number here, and a value of exactly 23 is
        # indistinguishable from "not reported" in practice: a link that had
        # genuinely negotiated the minimum could not carry this protocol at all
        # and would be failing everywhere else in this run.
        mtu = getattr(client, "mtu_size", None)
        if mtu and mtu > 23:
            note(f"negotiated ATT MTU {mtu}")
            check(mtu >= 100,
                  f"negotiated ATT MTU {mtu} is below the 100 SPEC.md 2 requires")
        elif mtu == 23:
            note("this backend reports the ATT default (23) rather than the "
                 "negotiated MTU — BlueZ does — so SPEC.md 2's floor is not "
                 "checked here; the notification sizes below still are")
        else:
            note("this backend does not expose the negotiated MTU")

        # ---- Control: a real indication on a real link -----------------
        answers = []
        can_subscribed = None

        async def request(opcode, tag, params=b"", what=""):
            """Write one request and wait for the indication that answers it.

            Every request is checked. CAN_SUBSCRIBE used to be written and
            slept on, so a device that refused it -- table_full, bad_params,
            or an unsupported_opcode from a build with no CAN -- produced a
            silent CAN stream that this test then blamed on the radio.
            """
            before = len(answers)
            t0 = time.monotonic()
            await client.write_gatt_char(
                CHAR["control"], bytes([opcode, tag]) + params, response=True)
            for _ in range(60):
                if len(answers) > before:
                    break
                await asyncio.sleep(0.05)
            if not check(len(answers) > before,
                         f"{what}: written, but no indication arrived; a "
                         f"device MUST answer every request it applies "
                         f"(SPEC.md 9)"):
                return None, 0.0
            resp = vtp1.decode_control_response(answers[before])
            check(resp["tag"] == tag,
                  f"{what}: the response echoed tag {resp['tag']}, not {tag}")
            return resp, (time.monotonic() - t0) * 1000

        if has["control"]:
            try:
                await client.start_notify(
                    CHAR["control"], lambda _h, d: answers.append(bytes(d)))
            except Exception as exc:                   # noqa: BLE001
                if looks_like_pairing(exc):
                    pairing_advice("enabling indications on Control", exc)
                    raise SystemExit(1)
                raise

            # TIME_SYNC: parameterless, idempotent, and the answer is a record
            # rather than an empty ok, so a wrong offset shows up immediately.
            resp, rtt_ms = await request(0x30, 0x5A, what="TIME_SYNC")
            if resp and check(resp["status"] == 0,
                              f"TIME_SYNC answered status {resp['status']}"):
                ts = vtp1.decode_time_sync(bytes.fromhex(resp["detail_hex"]))
                check(ts["t_device_tx"] >= ts["t_device_rx"],
                      "t_device_tx precedes t_device_rx (SPEC.md 9.7)")
                note(f"TIME_SYNC round trip {rtt_ms:.1f} ms, device "
                     f"processing "
                     f"{(ts['t_device_tx'] - ts['t_device_rx']) / 1000:.2f} ms")

            # A CAN device forwards nothing until asked (SPEC.md 9.2).
            if has["can"]:
                resp, _ = await request(
                    0x02, 0x5B,
                    (0x1A0).to_bytes(4, "little") + bytes([0])
                    + (0).to_bytes(2, "little"),
                    what="CAN_SUBSCRIBE")
                can_subscribed = bool(resp) and resp["status"] == 0
                check(can_subscribed,
                      f"CAN_SUBSCRIBE was refused (status "
                      f"{resp['status'] if resp else 'none'}), so nothing "
                      f"below can say anything about the CAN stream")
                if can_subscribed:
                    detail = bytes.fromhex(resp["detail_hex"])
                    check(len(detail) == 2,
                          f"CAN_SUBSCRIBE answered ok with {len(detail)} "
                          f"detail byte(s); SPEC.md 9 says the detail is a "
                          f"u16 handle")

        # ---- The streams, together --------------------------------------
        sinks = {n: s for n, s in (("gps", gps), ("can", can), ("imu", imu))
                 if has[n]}
        if sinks:
            await collect(client, sinks, args.seconds)

    return {"info": info, "gps": gps, "can": can, "imu": imu, "mtu": mtu,
            "can_subscribed": can_subscribed}


def inspect(result, args):
    """Decode what arrived and check the properties a real link can break."""
    info = result["info"]
    decoders = {"gps": lambda p: vtp1.decode_gps_fix(p),
                "can": lambda p: vtp1.decode_can_batch(p)["header"],
                "imu": lambda p: vtp1.decode_imu_batch(p)["header"]}

    # Silence is only acceptable where the device itself said to expect it.
    # An empty stream used to be a note, so a device that connected, answered
    # Info and then sent absolutely nothing passed this test -- which is the
    # single most likely way for a first bring-up to fail.
    #
    # GPS and IMU publish the rate they are CURRENTLY running at (SPEC.md 4);
    # non-zero means notifications are due and none arriving is a fault. CAN
    # has no such field: a real vehicle bus can be genuinely quiet, so CAN
    # silence stays a note -- but only once the subscription that gates it has
    # been confirmed installed, which `one_connection` now checks.
    expected = {
        "gps": info.get("gps_rate_hz", 0) > 0,
        "imu": info.get("imu_rate_hz", 0) > 0,
        "can": False,
    }
    clocks = {}
    for name, decode in decoders.items():
        payloads = result[name]
        if not payloads:
            check(not expected[name],
                  f"{name}: nothing arrived in {args.seconds:.0f}s, but Info "
                  f"says the device is running at "
                  f"{info.get(name + '_rate_hz', 0)} Hz")
            if not expected[name]:
                note(f"{name}: nothing arrived, and Info did not promise any")
            continue
        note(f"{name}: {len(payloads)} notification(s), "
             f"largest {max(len(p) for p in payloads)} bytes")

        headers = []
        for payload in payloads:
            try:
                headers.append(decode(payload))
            except vtp1.Reject as exc:
                check(False, f"{name}: the device sent something the reference "
                             f"decoder rejects: {exc} ({payload.hex()})")
                break
        if len(headers) != len(payloads):
            continue

        # SPEC.md 4.2 -- the ceiling the device published bounds every
        # notification it sends, on any link.
        biggest = max(len(p) for p in payloads)
        check(biggest <= info["max_notify_bytes"],
              f"{name}: a {biggest}-byte notification exceeds the "
              f"max_notify_bytes {info['max_notify_bytes']} Info published "
              f"(SPEC.md 4.2)")

        # SPEC.md 8.2 -- +1 each, wrapping. Gaps are the link losing what the
        # device sent, which is worth reporting but is not a device fault.
        seqs = [h["seq"] for h in headers]
        gaps = sum(1 for a, b in zip(seqs, seqs[1:]) if (a + 1) & 0xFFFF != b)
        check(seqs[0] is not None and len(set(seqs)) == len(seqs),
              f"{name}: a sequence number repeated: {seqs[:8]}")
        if gaps:
            note(f"{name}: {gaps} sequence gap(s) — the LINK lost "
                 f"notifications the device sent; not a device fault")
        dropped = sum(h["dropped"] for h in headers)
        if dropped:
            note(f"{name}: device reported {dropped} dropped item(s)")

        t = [h["t_device"] if name == "gps" else h["t_base"] for h in headers]
        check(all(b >= a for a, b in zip(t, t[1:])),
              f"{name}: device timestamps went backwards, so this is not one "
              f"monotonic clock (SPEC.md 8.1)")
        clocks[name] = (min(t), max(t))

    # SPEC.md 8.1 -- one clock for every role. Three streams whose windows do
    # not overlap are three clocks, and cross-channel alignment is the whole
    # point of carrying them on one link.
    if len(clocks) > 1:
        lo = max(v[0] for v in clocks.values())
        hi = min(v[1] for v in clocks.values())
        check(hi >= lo,
              f"the streams' device-clock windows do not overlap, so they are "
              f"not sharing one clock: {clocks}")
        if hi >= lo:
            note(f"{len(clocks)} streams share a device clock over a "
                 f"{(hi - lo) / 1e6:.1f} s overlap")
    elif len(clocks) == 1:
        note("only one stream produced data, so SPEC.md 8.1's shared clock is "
             "not exercised")
    else:
        # Every rate could legitimately be zero and the bus genuinely quiet,
        # and each of those is a note above. All of them at once is not a
        # device this test can say anything about: no telemetry crossed the
        # radio, which is the one thing a smoke test exists to witness.
        check(False,
              "no notification arrived on any characteristic, so nothing was "
              "carried over the air; a green run here would mean only that "
              "the device answered a read")


async def main_async(args):
    try:
        from bleak import BleakClient, BleakScanner
    except ImportError:
        sys.exit("bleak is required: pip install -r "
                 "reference/peripheral/requirements-client.txt")

    print(f"scanning {args.timeout:.0f}s for service {SERVICE} ...")
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: SERVICE.lower() in [u.lower() for u in ad.service_uuids],
        timeout=args.timeout)
    if device is None:
        sys.exit("no VTP/1 device advertising that service UUID was found.\n"
                 "Is the peripheral running, and is it advertising the SERVICE "
                 "uuid (SPEC.md 3.3)?")
    print(f"found {device.name or '<unnamed>'} at {device.address}")

    first = await one_connection(BleakClient, device.address, args, "first")
    if first is None:
        return 1
    inspect(first, args)

    # SPEC.md 8.2 -- seq restarts at 0 per connection, and 9.2 -- the
    # subscription table is cleared. Neither is observable without actually
    # dropping a real link and making a second one.
    if args.reconnect:
        await asyncio.sleep(1.0)
        second = await one_connection(BleakClient, device.address, args, "second")
        if second:
            inspect(second, args)
            for name in ("gps", "can", "imu"):
                if not second[name]:
                    continue
                head = (vtp1.decode_gps_fix(second[name][0])["seq"]
                        if name == "gps" else
                        vtp1.decode_can_batch(second[name][0])["header"]["seq"]
                        if name == "can" else
                        vtp1.decode_imu_batch(second[name][0])["header"]["seq"])
                check(head == 0,
                      f"{name}: the second connection began at seq {head}, "
                      f"not 0; SPEC.md 8.2 restarts per connection")

    print()
    for text in notes:
        print(f"  {text}")
    if problems:
        print(f"\n{len(problems)} problem(s) over a real radio.", file=sys.stderr)
        return 1
    print("\nReal-link smoke test passed: discovered by service UUID, Info "
          "coherent, control answered by indication, streams decoded on one "
          "device clock.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seconds", type=float, default=5.0,
                    help="how long to collect each stream (default 5)")
    ap.add_argument("--timeout", type=float, default=15.0,
                    help="scan timeout (default 15)")
    ap.add_argument("--no-reconnect", dest="reconnect", action="store_false",
                    help="skip the second connection; SPEC.md 8.2's per-"
                         "connection restart then goes unchecked")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
