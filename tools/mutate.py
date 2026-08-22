#!/usr/bin/env python3
"""Mutation-test the conformance corpus against the C reference.

A suite that cannot fail proves nothing. ci.yml already seeds two faults by
hand, which proves the corpus can fail but says nothing about coverage: a
hand-picked mutation only tests the line someone thought to pick. This sweeps
whole classes of fault systematically, and every survivor is a hole in the
corpus rather than a bug in the decoder.

Both holes this found on its first run were real, and neither was visible to
review:

  * every link_params vector had ll_max_tx_octets equal to ll_max_rx_octets, so
    a decoder reading both from one offset passed the entire corpus;
  * every link_params vector was canonical, so an encoder that ignored the
    validity gates passed the entire corpus -- the same gap gps_fix had in the
    first version of this repository.

Operators:

  gate      Drop one encoder validity gate. Catches "the encoder does not
            enforce SPEC.md §5.1", which only a non-canonical vector detects.
  presence  Drop one ternary presence gate (SPEC.md §7). Added after the gate
            operator's textual pattern was found to miss the IMU gating
            entirely, leaving two unprotected gates unreported.
  offset    Read one decoder field from a sibling field's offset. Catches a
            corpus whose vectors never distinguish two same-sized fields.
  length    Relax one exact-length check to accept trailing bytes. Catches
            "a malformed payload is rejected whole", SPEC.md §1.1.
  bound     Disable a range check such as `len > 64`. A corpus cannot reach
            these by construction, so only a must-reject vector tests them.

Usage:
  python3 tools/mutate.py             sweep every operator, exit 1 on a survivor
  python3 tools/mutate.py --operator gate
  python3 tools/mutate.py --list      print the mutations without running them
"""
import argparse, pathlib, re, shutil, subprocess, sys, tempfile

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = pathlib.Path(__file__).resolve().parent.parent
CDIR = ROOT / "reference" / "c"
SCHEMA = yaml.safe_load((ROOT / "schema" / "vtp1.yaml").read_text())

DECODER = "vtp1.c"
ENCODER = "vtp1_encode.c"


def _call_span(text, start):
    """End index of the balanced-paren call whose '(' is at `start`."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("unbalanced parentheses")


def _split_args(argtext):
    """Top-level comma split, ignoring commas inside nested parens."""
    args, depth, cur = [], 0, ""
    for ch in argtext:
        if ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        cur += ch
    args.append(cur.strip())
    return args


def op_gate():
    """Replace each gate32(v, validity, BIT) call with a bare v."""
    text = (CDIR / ENCODER).read_text()
    for m in reversed(list(re.finditer(r"\bgate(?:32|64)\(", text))):
        line_start = text.rfind("\n", 0, m.start()) + 1
        # Skip the helpers' own definitions; replacing one with its first
        # parameter is a syntax error, not a mutation.
        if text[line_start:m.start()].lstrip().startswith("static"):
            continue
        open_paren = m.end() - 1
        close = _call_span(text, open_paren)
        args = _split_args(text[open_paren + 1:close])
        if len(args) != 3:
            continue
        value, _, bit = args
        mutated = text[:m.start()] + value + text[close + 1:]
        line = text[:m.start()].count("\n") + 1
        # A bit may gate several fields, so the line disambiguates.
        yield (f"encoder drops the {bit} gate ({ENCODER}:{line})",
               ENCODER, mutated)


def _offset_macro(record, field):
    return f"VTP_{record.upper()}_OFF_{field.upper()}"


def op_offset():
    """Read a field from a same-sized sibling's offset within its record."""
    text = (CDIR / DECODER).read_text()
    for name, rec in SCHEMA["records"].items():
        fields = rec["fields"]
        for f in fields:
            # Only a same-sized sibling produces a mutation that still compiles
            # and still reads in bounds; a different size would be a type error
            # rather than a corpus question.
            siblings = [g for g in fields
                        if g["size"] == f["size"] and g["name"] != f["name"]]
            if not siblings:
                continue
            macro = _offset_macro(name, f["name"])
            if macro not in text:
                continue
            other = _offset_macro(name, siblings[0]["name"])
            mutated = text.replace(macro, other, 1)
            if mutated == text:
                continue
            yield (f"decoder reads {name}.{f['name']} from "
                   f"{siblings[0]['name']}'s offset", DECODER, mutated)


def op_presence():
    """Drop a ternary presence gate, e.g. `accel ? (uint16_t)s->ax : 0`.

    The gate operator above only recognises gate32()/gate64() calls, so the
    IMU gating -- written as a ternary -- was invisible to it and two
    unprotected presence gates went unreported until an external review found
    them. Operators written by hand are incomplete in the same way a corpus
    is; tools/check_corpus.py exists because of this.
    """
    text = (CDIR / ENCODER).read_text()
    pattern = re.compile(r"(\w+)\s*\?\s*(\([a-z0-9_]+_t\)\s*[\w>.-]+)\s*:\s*0")
    for m in reversed(list(pattern.finditer(text))):
        flag, value = m.group(1), m.group(2)
        mutated = text[:m.start()] + value + text[m.end():]
        line = text[:m.start()].count("\n") + 1
        yield (f"encoder drops the '{flag}' presence gate ({ENCODER}:{line})",
               ENCODER, mutated)


def op_bound():
    """Disable a range check, e.g. `plen > 8`, or the CAN FD length ladder.

    A corpus cannot reach these by construction -- every legal vector is in
    range -- so only a hand-written must-reject vector tests them, and only a
    mutation reveals when there isn't one.

    The comparison is neutralised rather than the whole `if`, because these
    checks now live inside compound conditions: the original pattern required
    a bare `if (x > N)` and stopped matching the moment the length rules grew
    a format guard, at which point this operator generated nothing and said so
    only in a line of output nobody reads. An operator that matches nothing
    now fails the run.
    """
    text = (CDIR / DECODER).read_text()
    for m in reversed(list(re.finditer(r"(\w+) > (\d+)\)", text))):
        mutated = text[:m.start()] + "0)" + text[m.end():]
        line = text[:m.start()].count("\n") + 1
        yield (f"decoder drops the {m.group(1)} > {m.group(2)} bound "
               f"({DECODER}:{line})", DECODER, mutated)

    # The FD ladder is a lookup, not a comparison, so the pattern above cannot
    # reach it. Accepting every length is exactly the pre-§6.10 behaviour.
    if "vtp_fd_len_ok" in text:
        marker = "static int vtp_fd_len_ok(size_t n) {"
        start = text.index(marker) + len(marker)
        yield ("decoder accepts any CAN FD payload length "
               f"({DECODER}:{text[:start].count(chr(10)) + 1})",
               DECODER, text[:start] + "\n    return 1;" + text[start:])


def op_length():
    """Relax an exact-length check so trailing bytes are accepted."""
    text = (CDIR / DECODER).read_text()
    for m in reversed(list(re.finditer(r"len\s*!=\s*", text))):
        mutated = text[:m.start()] + "len < " + text[m.end():]
        line = text[:m.start()].count("\n") + 1
        yield (f"decoder accepts trailing bytes ({DECODER}:{line})",
               DECODER, mutated)


OPERATORS = {"gate": op_gate, "presence": op_presence,
             "offset": op_offset, "length": op_length, "bound": op_bound}


def run_case(label, filename, mutated, workdir):
    shutil.copytree(CDIR, workdir, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("vtp1_cli", "*.o"))
    (workdir / filename).write_text(mutated)
    binary = workdir / "vtp1_cli"

    build = subprocess.run(
        ["cc", "-std=c99", "-O2", "-o", str(binary),
         *(str(workdir / f) for f in ("vtp1_cli.c", "vtp1.c", "vtp1_encode.c"))],
        capture_output=True, text=True)
    if build.returncode != 0:
        return "build", build.stderr.strip().splitlines()[:1]

    # The corpus must NOTICE. A zero exit means the mutation survived.
    result = subprocess.run(
        [sys.executable, str(ROOT / "conformance" / "run.py"),
         "--impl", str(binary)],
        capture_output=True, text=True, cwd=ROOT)
    return ("survived" if result.returncode == 0 else "caught"), None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--operator", choices=sorted(OPERATORS),
                    help="run a single operator instead of all of them")
    ap.add_argument("--list", action="store_true",
                    help="print the mutations without building or running")
    args = ap.parse_args()

    chosen = [args.operator] if args.operator else sorted(OPERATORS)
    survived, build_failed, caught, empty = [], [], 0, []

    for name in chosen:
        mutations = list(OPERATORS[name]())
        print(f"\n{name} — {len(mutations)} mutation(s)")
        if not mutations:
            empty.append(name)
        if args.list:
            for label, filename, _ in mutations:
                print(f"    {filename}: {label}")
            continue
        for label, filename, mutated in mutations:
            with tempfile.TemporaryDirectory() as tmp:
                verdict, detail = run_case(label, filename, mutated,
                                           pathlib.Path(tmp))
            if verdict == "caught":
                caught += 1
            elif verdict == "build":
                build_failed.append((label, detail))
                print(f"    SKIP     {label}: did not compile")
            else:
                survived.append(label)
                print(f"    SURVIVED {label}")

    if args.list:
        return 0

    print(f"\n{caught} caught, {len(survived)} survived, "
          f"{len(build_failed)} uncompilable")

    failed = False
    if survived:
        failed = True
        print("\nA surviving mutation is a hole in the corpus, not a bug in "
              "the decoder:", file=sys.stderr)
        for label in survived:
            print(f"  {label}", file=sys.stderr)
        print("\nAdd a vector that distinguishes the mutated behaviour from "
              "the correct one.", file=sys.stderr)

    # A mutation that does not compile tested nothing, and a run of nothing
    # but those used to exit 0. Breaking a header made every one of 79
    # mutations fail to build and this tool still reported success -- turning
    # the strongest check in the repository into a no-op that CI reads as a
    # pass. The same failure has now appeared three times in different guises
    # (a stale build, a missing runner, a stale __pycache__), so it is worth
    # being blunt: a check that cannot fail is not a check.
    if build_failed:
        failed = True
        print(f"\n{len(build_failed)} mutation(s) did not compile, so they "
              f"proved nothing:", file=sys.stderr)
        for label, detail in build_failed:
            print(f"  {label}: {(detail or ['no output'])[0]}", file=sys.stderr)
        print("\nFix the build. A mutation sweep that cannot build is not a "
              "sweep that passed.", file=sys.stderr)

    # An operator whose pattern stops matching contributes zero mutations and
    # says nothing about it. `bound` became dead exactly this way when the
    # redundant length check it targeted was removed.
    if empty:
        failed = True
        print(f"\noperator(s) generated no mutations at all: "
              f"{', '.join(empty)}", file=sys.stderr)
        print("Either the code they target is gone -- in which case delete "
              "the operator -- or their pattern has drifted and they are "
              "silently testing nothing.", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
