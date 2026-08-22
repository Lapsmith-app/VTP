#!/usr/bin/env python3
"""VTP/1 producer conformance runner — implementation-agnostic.

conformance/run.py tests decoding. This tests the other direction: what a
conforming encoder must REFUSE rather than reshape.

    python3 conformance/produce.py --impl "reference/c/vtp1_producer"
    python3 conformance/produce.py --impl "python3 reference/python/vtp1_produce.py"

This replaces tools/check_encoders.py, which imported the Python encoder
directly. That made "14/14 producer cases" a statement about one of this
repository's two reference encoders: the C encoder had four malformed-input
crashes and a violation of its own "nothing written on failure" contract, and
every producer run stayed green throughout, because none of them was ever
called. A producer suite that can only test the language it is written in is
not a conformance suite.

Cases carry structured input rather than bytes, because the whole point is that
the wrong bytes are never produced — an encoder handed an identifier outside
the arbitration field masked it and emitted a perfectly valid frame for a
DIFFERENT one, and no decode vector reaches that.

Exit status is 0 only when every case passes.
"""
import argparse, json, pathlib, shlex, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CASES = ROOT / "conformance" / "encoders.json"

# Which record belongs to which role, so a partial implementation runs the
# subset it declares — the same rule conformance/run.py applies to decoding.
# Imported rather than restated: two copies of a role table are two role
# tables, and they disagree the moment one is edited.
sys.path.insert(0, str(ROOT / "conformance"))
from run import ROLES, IMPLIES     # noqa: E402


def load(roles=None):
    wanted = None
    if roles is not None:
        wanted = set(ROLES["core"])
        for role in roles:
            wanted |= ROLES[role]
            for implied in IMPLIES.get(role, ()):
                wanted |= ROLES[implied]
    cases = json.loads(CASES.read_text())["cases"]
    if wanted is None:
        return cases
    return [c for c in cases if c["record"] in wanted]


def drive(impl, cases):
    """Feed every case to the implementation and return its answers.

    A short reply is not padded out. An implementation that dies partway
    through has answered nothing for the case it died on, and saying so is the
    entire reason this runner exists: a crash is not a refusal.
    """
    stdin = "".join(f"{c['record']}\t{json.dumps(c['input'])}\n" for c in cases)
    proc = subprocess.run(shlex.split(impl), input=stdin, capture_output=True,
                          text=True)
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    answers = []
    for i, _ in enumerate(cases):
        if i >= len(lines):
            answers.append(None)
            continue
        try:
            answers.append(json.loads(lines[i]))
        except ValueError:
            answers.append({"ok": None, "reason": f"unparsable: {lines[i][:80]!r}"})
    return answers, proc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--impl", required=True,
                    help="command implementing the producer contract "
                         "(conformance/README.md)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--roles", metavar="LIST",
                    help="comma-separated roles this implementation declares "
                         f"({', '.join(r for r in ROLES if r != 'core')}). "
                         "Cases for other roles are skipped; core is always "
                         "included. Omit to run every case.")
    args = ap.parse_args()

    roles = None
    if args.roles:
        roles = [r.strip() for r in args.roles.split(",") if r.strip()]
        unknown = [r for r in roles if r not in ROLES or r == "core"]
        if unknown:
            sys.exit(f"unknown role(s): {', '.join(unknown)}")

    cases = load(roles)
    if not cases:
        sys.exit("no producer cases for those roles")

    answers, proc = drive(args.impl, cases)
    passed = failed = 0

    for case, got in zip(cases, answers):
        name, want_refusal = case["name"], case["must_refuse"]
        if got is None:
            failed += 1
            print(f"    FAIL {name}: no answer — the implementation stopped "
                  f"before this case. A crash is not a refusal.")
            continue
        if got.get("ok") is None:
            failed += 1
            print(f"    FAIL {name}: {got.get('reason')}")
            continue

        refused = not got["ok"]
        if refused != want_refusal:
            failed += 1
            print(f"    FAIL {name}: " + (
                "encoded a payload it MUST refuse" if want_refusal
                else f"refused a valid input: {got.get('reason')}"))
            continue

        # A case that must encode may also pin the bytes. That is what makes
        # two implementations agree on the producer side rather than merely
        # both being willing: `expect_hex` is derived from the schema layout
        # directly, so neither reference encoder is grading its own homework.
        expect_hex = case.get("expect_hex")
        if not want_refusal and expect_hex and got.get("hex") != expect_hex:
            failed += 1
            print(f"    FAIL {name}: encoded {got.get('hex')!r}, "
                  f"expected {expect_hex!r}")
            continue

        passed += 1
        if args.verbose:
            why = got.get("reason")
            print(f"    ok   {name}" + (f" — {why}" if why else ""))

    scope = f"roles: {', '.join(['core'] + roles)}" if roles else "all roles"
    print(f"\n{passed} passed, {failed} failed, {len(cases)} producer case(s) "
          f"({scope})")
    if proc.stderr.strip() and args.verbose:
        print("--- implementation stderr ---", file=sys.stderr)
        print(proc.stderr, file=sys.stderr)
    if failed:
        print("\nAn encoder that reshapes its caller's input hands the mistake "
              "to whoever is\non the other end of the link, where no decoder "
              "can find it.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
