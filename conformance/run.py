#!/usr/bin/env python3
"""VTP/1 conformance runner — implementation-agnostic.

Feeds every vector to a decoder that speaks the runner contract (see
conformance/README.md) and compares its output to the expected decode.

    python3 conformance/run.py --impl "reference/c/vtp1_cli"
    python3 conformance/run.py --impl "dart run reference/dart/bin/vtp_decode.dart"

Exit status is 0 only when every case for every role passes. An
implementation that declares a subset of roles runs that subset with --roles,
and the summary says which.
"""
import argparse, json, pathlib, shlex, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VECTORS = ROOT / "conformance" / "vectors"


# SPEC.md §12 — "Every implementation MUST pass those for the roles it
# declares." The runner had no notion of a role, so a GPS-only decoder was
# handed every CAN, IMU and Monitor vector and failed conformance for records
# it never claimed to implement. Its only options were to fail, or to decode
# things it does not support.
#
# `core` is not optional: Info, the control response envelope and link
# parameters are what every device answers regardless of which streams it has.
ROLES = {
    # §4 — every device answers Info, whatever else it implements. Nothing
    # else is unconditional.
    "core":    {"info"},
    # §9 — the Control characteristic is a capability (§4 bit 3), not a
    # requirement. `core` used to carry the control-plane records, so
    # `--roles gps` demanded a Control characteristic from a GPS-only device
    # that the specification permits not to have one, leaving it to fail
    # conformance for records it never claimed or to implement things it does
    # not support.
    "control": {"control_response", "link_params", "time_sync"},
    "gps":     {"gps_fix"},
    "can":     {"can_batch", "can_list"},
    "imu":     {"imu_batch"},
    "monitor": {"monitor_list", "monitor_update"},
    # §9.9 — the GET_POWER detail. A role like any other: a device without a
    # battery never declares the bit and is never handed these.
    "power":   {"power_state"},
}

# A role whose records only exist if another one does. This is not the runner's
# own rule any more: SPEC.md §4.1 makes `can` and `monitor` require `control`
# normatively, and the table below is read from the schema that generates it.
#
# It used to be a hard-coded dict, which meant the runner enforced an
# implication the specification did not state — canonical Info vectors blessed
# a CAN device with no Control characteristic while `--roles can` demanded
# control responses from it. Two answers to one question, in one repository.
def _implications():
    import yaml
    schema = yaml.safe_load((ROOT / "schema" / "vtp1.yaml").read_text())
    bits = schema["bitmasks"]["capabilities"]["bits"]
    implies = {}
    for b in bits:
        # Only capabilities that are also conformance roles: `can_fd` implies
        # `can`, but there is no `can_fd` role to run.
        if b["name"] not in ROLES:
            continue
        needed = tuple(r for r in (b.get("implies") or []) if r in ROLES)
        if needed:
            implies[b["name"]] = needed
    return implies


IMPLIES = _implications()


def load(roles=None):
    """Every vector, or only those for the given roles. `core` is always in."""
    wanted = None
    if roles is not None:
        wanted = set(ROLES["core"])
        for role in roles:
            wanted |= ROLES[role]
            for implied in IMPLIES.get(role, ()):
                wanted |= ROLES[implied]
    cases = []
    for path in sorted(VECTORS.glob("*.json")):
        for c in json.loads(path.read_text())["cases"]:
            if wanted is not None and c["record"] not in wanted:
                continue
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
        # In Python `True == 1` and `False == 0`, so a decoder emitting 1 for a
        # boolean field -- or 1.0 for an integer one -- compared equal and
        # passed. The runner exists to check what an implementation put on
        # stdout, and JSON distinguishes these even where Python does not.
        elif isinstance(expect, bool) != isinstance(got, bool):
            yield (f"{path}: expected {expect!r} ({type(expect).__name__}), "
                   f"got {got!r} ({type(got).__name__})")
        elif isinstance(expect, int) and isinstance(got, float):
            yield (f"{path}: expected the integer {expect!r}, got the float "
                   f"{got!r}")
        elif expect != got:
            yield f"{path}: expected {expect!r}, got {got!r}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--impl", required=True,
                    help="command implementing the runner contract")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--roles", metavar="LIST",
                    help="comma-separated roles this implementation declares "
                         f"({', '.join(r for r in ROLES if r != 'core')}). "
                         "Vectors for other roles are skipped; core is always "
                         "included. Omit to run every vector.")
    args = ap.parse_args()

    roles = None
    if args.roles:
        roles = [r.strip() for r in args.roles.split(",") if r.strip()]
        unknown = [r for r in roles if r not in ROLES]
        if unknown:
            print(f"unknown role(s): {', '.join(unknown)}. Known: "
                  f"{', '.join(sorted(ROLES))}", file=sys.stderr)
            return 2

    cases = load(roles)
    if not cases:
        print("no vectors selected", file=sys.stderr)
        return 2
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

    # Name the roles, or a green partial run reads as full conformance.
    scope = ("every role" if roles is None
             else f"roles: core, {', '.join(sorted(roles))}")
    print(f"\n{passed} passed, {failed} failed, {len(cases)} total ({scope})")
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
