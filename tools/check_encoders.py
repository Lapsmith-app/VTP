#!/usr/bin/env python3
"""Check the producer direction: what a conforming encoder must REFUSE.

conformance/run.py tests decoding. Everything it says about encoders comes from
round-tripping payloads that already decoded, so it only ever asks whether an
encoder reproduces something valid — never whether it declines something
invalid.

That gap hid a whole class of defect. An encoder handed an identifier outside
the arbitration field masked it and emitted a perfectly valid frame for a
DIFFERENT one; -1 and 0x3FFFFFFF both became 0x1FFFFFFF, so two distinct
mistakes produced one wrong answer that no decoder could ever flag. No byte
vector reaches that, because the whole point is that the wrong bytes are never
produced.

These cases carry structured input instead of bytes, and assert the outcome.

Usage:
  python3 tools/check_encoders.py
"""
import json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CASES = ROOT / "conformance" / "encoders.json"
sys.path.insert(0, str(ROOT / "reference" / "python"))

import vtp1_encode as enc      # noqa: E402


def call(case):
    """Drive the encoder for this record from the case's structured input."""
    i = case["input"]
    record = case["record"]
    if record == "can_batch":
        return enc.encode_can_batch(i["header"], i["records"])
    if record == "imu_batch":
        return enc.encode_imu_batch(i["header"], i["samples"])
    if record == "gps_fix":
        return enc.encode_gps_fix(i["fix"], bytes.fromhex(i.get("ext_hex", "")))
    if record == "monitor_list":
        return enc.encode_monitor_list(i["page"], i["entries"])
    if record == "monitor_update":
        return enc.encode_monitor_update(i["header"], i["values"])
    if record == "control_response":
        return enc.encode_control_response(i)
    if record == "time_sync":
        return enc.encode_time_sync(i)
    raise SystemExit(f"no encoder wired for record {record!r}")


def main():
    cases = json.loads(CASES.read_text())["cases"]
    passed = failed = 0
    for case in cases:
        want_refusal = case["must_refuse"]
        try:
            call(case)
            refused, why = False, None
        except enc.EncodeError as exc:
            refused, why = True, str(exc)
        except Exception as exc:                      # noqa: BLE001
            # A TypeError or a struct.error is a crash, not a refusal. An
            # encoder that falls over is not an encoder that declined.
            print(f"    FAIL {case['name']}: raised {type(exc).__name__} "
                  f"instead of EncodeError: {exc}")
            failed += 1
            continue

        if refused == want_refusal:
            passed += 1
            if "-v" in sys.argv:
                print(f"    ok   {case['name']}"
                      + (f" — {why}" if why else ""))
        else:
            failed += 1
            print(f"    FAIL {case['name']}: "
                  + ("encoded a payload it MUST refuse"
                     if want_refusal else f"refused a valid input: {why}"))

    print(f"\n{passed} passed, {failed} failed, {len(cases)} producer case(s)")
    if failed:
        print("\nAn encoder that reshapes its caller's input hands the mistake "
              "to whoever is\non the other end of the link, where no decoder "
              "can find it.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
