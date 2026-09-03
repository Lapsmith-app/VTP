#!/usr/bin/env python3
"""Check that no existing conformance case has changed meaning.

Covers both corpora: the decode vectors in `conformance/vectors/` and the
producer cases in `conformance/encoders.json`, which make the same kind of
promise about what an encoder must refuse.

SPEC.md §12 makes a promise: "A minor version MAY add cases and MUST NOT
modify or remove an existing case. A change that alters the expected decode of
an existing vector is by definition not a minor version."

Nothing enforced it. The corpus is *generated*, so a schema edit can silently
change what an existing vector expects, and every other check in this
repository would still pass: the generator, the decoders and the runner would
simply agree with each other about the new answer. The corpus is compared
against itself, and a baseline is the only thing that compares it against what
it used to say.

That is the failure this repository keeps finding in its own tooling -- a check
that cannot fail. This one can.

What is hashed is the MEANING of a case: its bytes, and what a decoder must do
with them. Descriptions and notes are excluded on purpose, so prose can be
improved without tripping the check, and so tripping it always means something
real.

Usage:
  python3 tools/check_baseline.py            verify; record new cases
  python3 tools/check_baseline.py --check    verify only, never write (CI)
  python3 tools/check_baseline.py --accept   rewrite the baseline

`--accept` declares that the change is intended and is not a minor version.
Before 1.0 that is ordinary; after it, it is a major version (§11.1) and the
UUIDs change with it.
"""
import argparse, hashlib, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VECTORS = ROOT / "conformance" / "vectors"
PRODUCERS = ROOT / "conformance" / "encoders.json"
BASELINE = ROOT / "conformance" / "baseline.json"

# Everything that decides what an implementation must DO. `desc` and `note` are
# absent deliberately: they are how the corpus explains itself, and improving
# an explanation is not a compatibility break.
SEMANTIC = ("record", "hex", "expect", "must_reject", "expect_absent",
            "expect_roundtrip_hex", "expect_scaled", "canonical",
            "no_roundtrip")

# The same idea for the producer corpus, whose cases say what an encoder must
# refuse and what it must emit. They were baselined by nothing until a change
# to the gps_fix reserved-bits baseline altered two of them: the generator, the
# cases and both references are regenerated together, so they agreed about the
# new answer and every check stayed green -- exactly the failure this file
# exists to prevent for decode vectors.
PRODUCER_SEMANTIC = ("record", "must_refuse", "structural", "vector", "input",
                     "expect_hex")


def digest(case, semantic=SEMANTIC):
    payload = {k: case[k] for k in semantic if k in case}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def current():
    out = {}

    def record(name, value):
        if name in out:
            sys.exit(f"duplicate case name {name!r}; names are the "
                     f"baseline's keys and MUST be unique")
        out[name] = value

    for path in sorted(VECTORS.glob("*.json")):
        for case in json.loads(path.read_text())["cases"]:
            record(f"{path.stem}/{case['name']}", digest(case))
    for case in json.loads(PRODUCERS.read_text())["cases"]:
        record(f"encoders/{case['name']}",
               digest(case, PRODUCER_SEMANTIC))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--accept", action="store_true",
                    help="rewrite the baseline, declaring the change intended")
    ap.add_argument("--check", action="store_true",
                    help="verify only, never write; also fails when the "
                         "baseline is missing cases that exist. This is what "
                         "CI runs, because a mode that writes on addition "
                         "passes in CI by discarding the write, and the "
                         "baseline would then protect only the cases it "
                         "started with while appearing to cover them all")
    args = ap.parse_args()

    now = current()
    if not BASELINE.exists() and args.check:
        print("conformance/baseline.json does not exist, so nothing is "
              "protected. Run tools/check_baseline.py and commit it.",
              file=sys.stderr)
        return 1
    if args.accept or not BASELINE.exists():
        BASELINE.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n")
        verb = "rewrote" if BASELINE.exists() else "created"
        print(f"{verb} the baseline: {len(now)} case(s)")
        return 0

    was = json.loads(BASELINE.read_text())
    changed = sorted(n for n in was if n in now and was[n] != now[n])
    removed = sorted(n for n in was if n not in now)
    added = sorted(n for n in now if n not in was)

    if changed or removed:
        for n in changed:
            print(f"CHANGED: {n}: an existing case now requires something "
                  f"different", file=sys.stderr)
        for n in removed:
            print(f"REMOVED: {n}", file=sys.stderr)
        print(f"\n{len(changed) + len(removed)} existing case(s) no longer mean "
              f"what they did.\n"
              f"SPEC.md §12: a minor version MUST NOT modify or remove one. If "
              f"this is deliberate\nand you accept it is not a minor version, "
              f"run:\n\n    python3 tools/check_baseline.py --accept\n",
              file=sys.stderr)
        return 1

    if added:
        if args.check:
            for n in added:
                print(f"UNRECORDED: {n}", file=sys.stderr)
            print(f"\n{len(added)} case(s) exist but are not in the baseline, "
                  f"so nothing is protecting them.\nRun tools/check_baseline.py "
                  f"and commit conformance/baseline.json.\n", file=sys.stderr)
            return 1
        print(f"{len(added)} case(s) added, none changed or removed:")
        for n in added:
            print(f"  + {n}")
        BASELINE.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n")
        print("baseline extended")
        return 0

    print(f"All {len(was)} baselined case(s) still mean what they did.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
