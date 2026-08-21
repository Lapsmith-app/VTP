#!/usr/bin/env python3
"""VTP/1 conformance runner — implementation-agnostic.

Feeds every vector to a decoder that speaks the runner contract (see
conformance/README.md) and compares its output to the expected decode.

    python3 conformance/run.py --impl "reference/c/vtp1_cli"
    python3 conformance/run.py --impl "dart run reference/dart/bin/vtp_decode.dart"

Exit status is 0 only when every case for every role passes.
"""
import argparse, json, pathlib, shlex, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VECTORS = ROOT / "conformance" / "vectors"


def load():
    cases = []
    for path in sorted(VECTORS.glob("*.json")):
        for c in json.loads(path.read_text())["cases"]:
            c["_file"] = path.name
            cases.append(c)
    return cases


def diff(expect, got, path=""):
    """Yield human-readable mismatches. Only keys present in `expect` are
    checked, so an implementation may report extra fields."""
    if isinstance(expect, dict):
        if not isinstance(got, dict):
            yield f"{path or '<root>'}: expected object, got {type(got).__name__}"
            return
        for k, v in expect.items():
            if k not in got:
                yield f"{path}.{k}: missing from output"
            else:
                yield from diff(v, got[k], f"{path}.{k}")
    elif isinstance(expect, list):
        if not isinstance(got, list):
            yield f"{path}: expected list, got {type(got).__name__}"
            return
        if len(expect) != len(got):
            yield f"{path}: expected {len(expect)} entries, got {len(got)}"
            return
        for i, (e, g) in enumerate(zip(expect, got)):
            yield from diff(e, g, f"{path}[{i}]")
    else:
        if isinstance(expect, str) and isinstance(got, str):
            if expect.lower() != got.lower():
                yield f"{path}: expected {expect!r}, got {got!r}"
        elif expect != got:
            yield f"{path}: expected {expect!r}, got {got!r}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--impl", required=True,
                    help="command implementing the runner contract")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    cases = load()
    stdin = "".join(f"{c['record']}\t{c['hex']}\n" for c in cases)

    proc = subprocess.run(shlex.split(args.impl), input=stdin, text=True,
                          capture_output=True, cwd=ROOT)
    if proc.returncode != 0:
        print(f"implementation exited {proc.returncode}\n{proc.stderr}", file=sys.stderr)
        return 2

    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    if len(lines) != len(cases):
        print(f"expected {len(cases)} output lines, got {len(lines)}", file=sys.stderr)
        return 2

    passed = failed = roundtripped = 0
    absent_checked = [0]
    current = None
    for case, line in zip(cases, lines):
        if case["_file"] != current:
            current = case["_file"]
            print(f"\n  {current}")
        try:
            got = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"    FAIL {case['name']}: unparseable output ({e})")
            failed += 1
            continue

        problems = []
        if case.get("must_reject"):
            if got.get("ok") is not False:
                problems.append(f"MUST be rejected ({case['must_reject']}), but decoded")
            elif args.verbose and got.get("reason") != case["must_reject"]:
                print(f"      note: rejected as {got.get('reason')!r}, "
                      f"vector suggests {case['must_reject']!r} (informational)")
        else:
            if got.get("ok") is not True:
                problems.append(f"rejected as {got.get('reason')!r}, but MUST decode")
            else:
                problems += list(diff(case["expect"], got))

                # The protocol's central rule: a field whose validity bit is
                # clear MUST be reported absent, never as a value. Checked as a
                # set so an implementation that resolves absence differently
                # (null, a sentinel-free optional, an omitted key) still proves
                # it honours the bitmask rather than the payload.
                if "expect_absent" in case:
                    if "absent" not in got:
                        # Reporting absence IS the protocol's central rule
                        # (SPEC.md §1.1). An implementation that omits it has
                        # not demonstrated the one thing this corpus exists to
                        # prove, so this is a failure and not a skip.
                        problems.append(
                            "no `absent` list reported, so the central rule of "
                            "the protocol is untested for this case")
                    else:
                        absent_checked[0] += 1
                    want, have = set(case["expect_absent"]), set(got.get("absent", []))
                    for field in sorted(want - have):
                        problems.append(
                            f"{field}: validity bit is clear, so it MUST be "
                            f"reported absent")
                    for field in sorted(have - want):
                        problems.append(
                            f"{field}: reported absent, but its validity bit is set")

                # Optional: an implementation that also encodes reports the
                # re-encoded bytes. Requiring them to equal the input exactly
                # checks what a decode cannot -- that the encoder agrees about
                # the layout, and that it emits the canonical form rather than
                # merely one that happens to decode back.
                if got.get("roundtrip_error"):
                    problems.append(f"re-encode failed: {got['roundtrip_error']}")
                elif "roundtrip_hex" in got:
                    roundtripped += 1
                    # A conforming encoder reproduces a canonical payload
                    # exactly, and normalises a non-canonical one — zeroing the
                    # stale bytes behind cleared validity bits.
                    want = case.get("expect_roundtrip_hex", case["hex"])
                    if got["roundtrip_hex"].lower() != want.lower():
                        label = ("round-trip did not normalise to the canonical form"
                                 if "expect_roundtrip_hex" in case
                                 else "round-trip is not byte-identical")
                        problems.append(
                            f"{label}\n"
                            f"           in   {case['hex']}\n"
                            f"           want {want}\n"
                            f"           got  {got['roundtrip_hex']}"
                        )

        if problems:
            failed += 1
            print(f"    FAIL {case['name']}")
            for p in problems:
                print(f"         {p}")
        else:
            passed += 1
            if args.verbose:
                print(f"    ok   {case['name']}")

    print(f"\n{passed} passed, {failed} failed, {len(cases)} total")
    if roundtripped:
        print(f"{roundtripped} of those also verified byte-identical through the encoder")
    else:
        print("encoder not exercised: this implementation reported no roundtrip_hex")
    if absent_checked[0]:
        print(f"{absent_checked[0]} case(s) had their absent-field set verified")
    else:
        print("absence not exercised: this implementation reported no `absent` list")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
