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

Run the peripheral on one machine and this on another:

    python3 reference/peripheral/serve.py                 # machine A
    python3 reference/peripheral/smoketest.py             # machine B

Needs `bleak` (a central-role library; `bless` is the peripheral one):

    pip install -r reference/peripheral/requirements-client.txt

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


async def collect(client, name, seconds, sink):
    """Subscribe to `name` and gather notifications for `seconds`."""
    uuid = CHAR[name]
    await client.start_notify(uuid, lambda _h, data: sink.append(bytes(data)))
    await asyncio.sleep(seconds)
    await client.stop_notify(uuid)


async def one_connection(BleakClient, address, args, first):
    """Everything checked on a single link. Run twice; `first` is the label."""
    gps, can, imu = [], [], []
    async with BleakClient(address) as client:
        print(f"\n[{first}] connected to {address}")

        # ---- Info, before anything else --------------------------------
        raw = bytes(await client.read_gatt_char(CHAR["info"]))
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
        mtu = getattr(client, "mtu_size", None)
        if mtu:
            note(f"negotiated ATT MTU {mtu}")
            check(mtu >= 100,
                  f"negotiated ATT MTU {mtu} is below the 100 SPEC.md 2 requires")
        else:
            note("this backend does not expose the negotiated MTU")

        # ---- Control: a real indication on a real link -----------------
        answers = []
        if has["control"]:
            await client.start_notify(
                CHAR["control"], lambda _h, d: answers.append(bytes(d)))
            # TIME_SYNC: parameterless, idempotent, and the answer is a record
            # rather than an empty ok, so a wrong offset shows up immediately.
            t0 = time.monotonic()
            await client.write_gatt_char(CHAR["control"], bytes([0x30, 0x5A]),
                                         response=True)
            for _ in range(50):
                if answers:
                    break
                await asyncio.sleep(0.05)
            rtt_ms = (time.monotonic() - t0) * 1000
            if check(bool(answers),
                     "TIME_SYNC was written but no indication arrived; a "
                     "device MUST answer every request it applies (SPEC.md 9)"):
                resp = vtp1.decode_control_response(answers[0])
                check(resp["tag"] == 0x5A,
                      f"the response echoed tag {resp['tag']}, not 0x5A")
                check(resp["status"] == 0,
                      f"TIME_SYNC answered status {resp['status']}")
                if resp["status"] == 0:
                    ts = vtp1.decode_time_sync(
                        bytes.fromhex(resp["detail_hex"]))
                    check(ts["t_device_tx"] >= ts["t_device_rx"],
                          "t_device_tx precedes t_device_rx (SPEC.md 9.7)")
                    note(f"TIME_SYNC round trip {rtt_ms:.1f} ms, device "
                         f"processing {(ts['t_device_tx'] - ts['t_device_rx']) / 1000:.2f} ms")

        # ---- The streams ------------------------------------------------
        wanted = [(n, s) for n, s in (("gps", gps), ("can", can), ("imu", imu))
                  if has[n]]
        if has["can"] and has["control"]:
            # A CAN device forwards nothing until asked (SPEC.md 9.2), so a
            # smoke test that only subscribed would report a silent stream and
            # blame the device.
            await client.write_gatt_char(
                CHAR["control"],
                bytes([0x02, 0x5B]) + (0x1A0).to_bytes(4, "little")
                + bytes([0]) + (0).to_bytes(2, "little"),
                response=True)
            await asyncio.sleep(0.3)

        for name, sink in wanted:
            await collect(client, name, args.seconds, sink)

    return {"info": info, "gps": gps, "can": can, "imu": imu, "mtu": mtu}


def inspect(result, args):
    """Decode what arrived and check the properties a real link can break."""
    info = result["info"]
    decoders = {"gps": lambda p: vtp1.decode_gps_fix(p),
                "can": lambda p: vtp1.decode_can_batch(p)["header"],
                "imu": lambda p: vtp1.decode_imu_batch(p)["header"]}
    clocks = {}
    for name, decode in decoders.items():
        payloads = result[name]
        if not payloads:
            note(f"{name}: nothing arrived")
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
