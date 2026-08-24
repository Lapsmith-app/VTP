#!/usr/bin/env python3
"""Producer-conformance adapter for the Python reference encoder.

The counterpart to reference/c/vtp1_producer.c, speaking the same contract
(conformance/README.md, "The producer contract"):

    stdin  — one case per line: <record><TAB><json-object>
    stdout — one JSON object per line, in the same order:
               {"ok": true,  "hex": "..."}      the encoder produced these bytes
               {"ok": false, "reason": "..."}   the encoder refused

Kept separate from vtp1_encode.py for the same reason vtp1.py's `__main__`
adapter is separate from the decoder: the harness is not part of what a device
would ship.

An exception that is not an EncodeError is deliberately NOT reported as a
refusal. A TypeError or a struct.error is a crash, and an encoder that falls
over is not an encoder that declined — reporting the two the same way is how a
producer suite goes green over a segfault.
"""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import vtp1_encode as enc      # noqa: E402


def produce(record, data):
    """Drive the encoder for `record` from the case's structured input."""
    if record == "gps_fix":
        return enc.encode_gps_fix(data["fix"],
                                  bytes.fromhex(data.get("ext_hex", "")))
    if record == "can_batch":
        return enc.encode_can_batch(data["header"], data["records"])
    if record == "imu_batch":
        return enc.encode_imu_batch(data["header"], data["samples"])
    if record == "info":
        return enc.encode_info(data)
    if record == "monitor_list":
        return enc.encode_monitor_list(data["declaration"], data["entries"])
    if record == "monitor_update":
        return enc.encode_monitor_update(data["header"], data["values"])
    if record == "control_response":
        return enc.encode_control_response(data)
    if record == "time_sync":
        return enc.encode_time_sync(data)
    if record == "power_state":
        return enc.encode_power_state(data)
    if record == "gnss_aid_caps":
        return enc.encode_gnss_aid_caps(data)
    if record == "aid_begin_result":
        return enc.encode_aid_begin_result(data)
    if record == "aid_commit_result":
        return enc.encode_aid_commit_result(data)
    if record == "obd_info":
        return enc.encode_obd_info(data["probe"], data["ecus"])
    raise LookupError(record)


def main():
    for line in sys.stdin:
        if "\t" not in line:
            continue
        record, raw = line.rstrip("\n").split("\t", 1)
        try:
            data = json.loads(raw)
        except ValueError as exc:
            print(json.dumps({"ok": False, "reason": f"harness: {exc}"}),
                  flush=True)
            continue
        try:
            payload = produce(record, data)
        except enc.EncodeError as exc:
            print(json.dumps({"ok": False, "reason": str(exc)}), flush=True)
            continue
        except LookupError:
            print(json.dumps({"ok": False,
                              "reason": "harness: no encoder for record"}),
                  flush=True)
            continue
        print(json.dumps({"ok": True, "hex": payload.hex()}), flush=True)


if __name__ == "__main__":
    main()
