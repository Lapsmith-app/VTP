#!/usr/bin/env python3
"""Check the prose against the artefacts it describes.

tools/generate.py --check proves the generated tables match the schema. This
proves the *hand-written* prose matches reality, which is the other half of the
same problem: a fact asserted in a sentence drifts as silently as a stale table,
and neither the corpus nor the drift check can see it.

Two classes of fact are checkable:

  Section references — every "§9.1" resolves to a heading that exists. A
  renumbered section otherwise rots every pointer to it, in a repository whose
  documents cross-reference each other constantly.

  Stated counts — "38 vectors across 5 record types" matches the corpus on
  disk. This one had already drifted before it was ever checked.

Usage:
  python3 tools/check_docs.py        report problems, exit 1 if any
"""
import json, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
VECTORS = ROOT / "conformance" / "vectors"

# Documents that carry their own § numbering. A bare "§4" inside one of these
# means that document's own §4; anywhere else a bare reference means SPEC.md.
NUMBERED = ("SPEC.md", "RATIONALE.md")

# Everything that may cite a section, including source comments -- a stale
# "SPEC.md §5.1" in a decoder comment misleads exactly the reader who most
# needs it to be right.
SOURCES = ["SPEC.md", "RATIONALE.md", "README.md", "CONTRIBUTING.md",
           "reference/c/vtp1.c", "reference/c/vtp1.h",
           "reference/c/vtp1_cli.c",
           "reference/c/vtp1_encode.c", "reference/c/vtp1_encode.h",
           "reference/python/vtp1.py", "tools/generate.py",
           "conformance/run.py", "conformance/README.md",
           "schema/README.md", "reference/README.md",
           # The harness cites more sections than anything else here, in
           # messages a developer reads while their device is failing. A
           # pointer to the wrong clause is worse there than in a comment.
           "harness/README.md", "harness/selftest.py",
           *sorted(str(p.relative_to(ROOT))
                   for p in (ROOT / "harness" / "vtp1_harness").rglob("*.py"))]

HEADING = re.compile(r"^#{2,4}\s+(?:Appendix\s+)?(\d+(?:\.\d+)*)\.?\s")
# An optional "SPEC.md "/"RATIONALE.md " qualifier decides which document is
# meant; a bare reference means the document it appears in. Both qualifiers are
# recognised because both documents carry their own numbering and cite each
# other, and a bare "§4.1" inside SPEC.md that meant RATIONALE's §4.1 resolved
# silently against the wrong document until this checker flagged it.
REFERENCE = re.compile(
    r"(SPEC|RATIONALE)?(?:\.md)?\s*§\s?(\d+(?:\.\d+)*)")
COUNT = re.compile(r"(\d+)\s+vectors\s+across\s+(\d+)\s+record\s+types")
# The producer corpus is a second corpus with a second count, and it was
# stated in prose and checked by nobody.
PRODUCERS = re.compile(r"(\d+)\s+producer\s+cases")
# Everything from the first released heading onward is history.
RELEASED = re.compile(r"^##\s+\[(?!Unreleased\])")


def headings(name):
    """The set of section numbers a document defines."""
    path = ROOT / name
    if not path.exists():
        return set()
    return {m.group(1) for m in
            (HEADING.match(line) for line in path.read_text().splitlines()) if m}


def check_references(problems):
    known = {name: headings(name) for name in NUMBERED}

    for name in SOURCES:
        path = ROOT / name
        if not path.exists():
            problems.append(f"{name}: listed in check_docs.py but missing")
            continue
        own = name if name in NUMBERED else None
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for qualifier, section in REFERENCE.findall(line):
                # An explicit qualifier names the document. A bare reference
                # means the current document when it has its own numbering,
                # and SPEC.md otherwise.
                if qualifier:
                    target = f"{qualifier}.md"
                else:
                    target = own or "SPEC.md"
                if section not in known[target]:
                    problems.append(
                        f"{name}:{n}: §{section} does not exist in {target}")


def check_counts(problems):
    files = sorted(VECTORS.glob("*.json"))
    cases = [c for p in files for c in json.loads(p.read_text())["cases"]]
    actual_cases = len(cases)
    # The record TYPES, not the files that hold them. Counting files made the
    # claim readable and false: ten files carry thirteen records, because a
    # file is a subject -- monitor, obd -- and a record is a struct.
    actual_records = len({c["record"] for c in cases})
    actual_producers = len(json.loads(
        (ROOT / "conformance" / "encoders.json").read_text())["cases"])

    seen = False
    for name in ("README.md", "CHANGELOG.md"):
        path = ROOT / name
        released = False
        for n, line in enumerate(path.read_text().splitlines(), 1):
            # A released changelog entry is a record of what that release
            # contained, not a claim about the corpus as it stands now.
            # Rewriting it to match today's count would falsify history, so
            # only [Unreleased] is held to the current corpus.
            if name == "CHANGELOG.md" and RELEASED.match(line):
                released = True
            if released:
                continue
            for cases, records in COUNT.findall(line):
                seen = True
                if (int(cases), int(records)) != (actual_cases,
                                                  actual_records):
                    problems.append(
                        f"{name}:{n}: claims {cases} vectors across {records} "
                        f"record types; the corpus holds {actual_cases} across "
                        f"{actual_records}")
            for stated in PRODUCERS.findall(line):
                if int(stated) != actual_producers:
                    problems.append(
                        f"{name}:{n}: claims {stated} producer cases; "
                        f"conformance/encoders.json holds {actual_producers}")
    if not seen:
        problems.append(
            "no document states the corpus size; README.md's status table "
            "should, so that the claim stays checkable")


def main():
    problems = []
    check_references(problems)
    check_counts(problems)

    if problems:
        for p in problems:
            print(f"STALE: {p}", file=sys.stderr)
        print(f"\n{len(problems)} stale claim(s).", file=sys.stderr)
        return 1
    print("Documentation claims match the artefacts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
