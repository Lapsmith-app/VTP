#!/usr/bin/env python3
"""Check that the corpus can actually detect the faults it is meant to.

tools/mutate.py answers the same question by breaking the C reference and
requiring the corpus to notice. That works, but its operators are *textual* —
it recognises `gate32(...)` calls, so C's IMU gating, written as a ternary,
was invisible to it and two unprotected presence gates went unreported. An
operator list written by hand is incomplete in exactly the way the corpus it
audits is incomplete.

This asks the question the other way round, from the schema, with no code
involved at all. For each rule the specification states, it derives what a
vector would have to look like to exercise that rule, and checks the corpus
contains one:

  gates       For every field behind a validity bit or a presence flag, a
              vector where that bit is CLEAR and the bytes are NON-ZERO.
              Without one, an encoder that never gates emits identical bytes
              and passes, and a decoder that ignores the mask reports a value
              nobody wrote. Canonical vectors cannot test a gate.
  rejects     For every record, a short payload and an over-long payload that
              must be rejected. SPEC.md §1.1 — malformed is rejected whole.
  distinct    For every pair of same-typed fields in a record, a vector giving
              them different values. Otherwise a decoder can read either from
              the other's offset and pass.
  enums       For every enum field, a vector carrying an unrecognised value.
              SPEC.md §11.4 — unknown stays unknown.

Usage:
  python3 tools/check_corpus.py          report gaps, exit 1 if any
"""
import itertools, json, pathlib, struct, sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA = yaml.safe_load((ROOT / "schema" / "vtp1.yaml").read_text())
VECTORS = ROOT / "conformance" / "vectors"

PACK = {"u8": "B", "i8": "b", "u16": "H", "i16": "h",
        "u32": "I", "i32": "i", "u64": "Q", "i64": "q"}

# A case names a record or a composite. `instances` walks a payload and yields
# every record instance inside it with its base offset, so a check written once
# applies to a batch's entries as readily as to a standalone record. Doing this
# by name alone silently skipped every imu_sample in the first version of this
# file, and reported all fifteen sample field pairs as untested when it had in
# fact never looked at one.
COMPOSITE = {"can_batch": ("can_header", "can_record"),
             "imu_batch": ("imu_header", "imu_sample"),
             "can_list": ("can_list_page", "can_subscription"),
             "monitor_list": ("monitor_page", "monitor_channel"),
             "monitor_update": ("monitor_header", "monitor_value")}
STANDALONE = ("gps_fix", "info", "link_params")


def cases():
    for path in sorted(VECTORS.glob("*.json")):
        for c in json.loads(path.read_text())["cases"]:
            c["_file"] = path.name
            yield c


def field(record, name):
    return next(g for g in SCHEMA["records"][record]["fields"]
                if g["name"] == name)


def read_field(buf, record, name, base=0):
    f = field(record, name)
    off = base + f["offset"]
    if off + f["size"] > len(buf):
        return None
    return struct.unpack_from("<" + PACK[f["type"]], buf, off)[0]


def instances(case, buf):
    """(record, base_offset) for every record instance in this payload."""
    name = case["record"]
    if name in STANDALONE:
        yield name, 0
        return
    header, entry = COMPOSITE[name]
    hsize = SCHEMA["records"][header]["size"]
    yield header, 0
    count = read_field(buf, header, "count")
    if count is None:
        return
    off = hsize
    for _ in range(count):
        esize = SCHEMA["records"][entry]["size"]
        if off + esize > len(buf):
            return
        yield entry, off
        # can_record carries a trailing payload counted by its own len field.
        off += esize + (read_field(buf, entry, "len", off) or 0
                        if SCHEMA["records"][entry].get("variable") else 0)


def exact_length(case, buf):
    """The length this payload's own header fields say it should be."""
    name = case["record"]
    if name in STANDALONE:
        base = SCHEMA["records"][name]["size"]
        if name != "gps_fix":
            return base
        return None            # extensions make the exact length open-ended
    header, entry = COMPOSITE[name]
    hsize = SCHEMA["records"][header]["size"]
    count = read_field(buf, header, "count")
    if count is None:
        return None
    total, off = hsize, hsize
    for _ in range(count):
        esize = SCHEMA["records"][entry]["size"]
        if off + esize > len(buf):
            return None
        plen = (read_field(buf, entry, "len", off) or 0
                if SCHEMA["records"][entry].get("variable") else 0)
        total += esize + plen
        off += esize + plen
    return total


def check_validity_gates(problems, decoded):
    """A gate is only tested by a vector that is non-canonical for it."""
    for name, rec in SCHEMA["records"].items():
        mask = rec.get("validity")
        if not mask:
            continue
        bit_of = {b["name"]: b["bit"]
                  for b in SCHEMA["bitmasks"][mask]["bits"]}
        for f in rec["fields"]:
            gate = f.get("valid_bit")
            if gate is None:
                continue
            if not any(
                    not (read_field(buf, name, "validity", base) or 0)
                    & (1 << bit_of[gate])
                    and read_field(buf, name, f["name"], base)
                    for case, buf in decoded
                    for r, base in instances(case, buf) if r == name):
                problems.append(
                    f"gate {name}.{f['name']} (bit {gate}) is never exercised: "
                    f"no vector clears the bit while the bytes hold a value, so "
                    f"an encoder that skips the gate passes the whole corpus")


def check_presence_gates(problems, decoded):
    """Same rule, for records whose presence flags live in a batch header."""
    for name, rec in SCHEMA["records"].items():
        pres = rec.get("presence")
        if not pres:
            continue
        header = pres["record"]
        for f in rec["fields"]:
            group = f.get("presence_bit")
            if group is None:
                continue
            bit = pres["bits"][group]
            covered = False
            for case, buf in decoded:
                flags = None
                for r, base in instances(case, buf):
                    if r == header:
                        flags = read_field(buf, header, pres["field"], base)
                    elif r == name and flags is not None and not (flags & (1 << bit)):
                        if read_field(buf, name, f["name"], base):
                            covered = True
                            break
                if covered:
                    break
            if not covered:
                problems.append(
                    f"presence gate {name}.{f['name']} ({header}.{pres['field']} "
                    f"bit {bit}, {group}) is never exercised: no vector clears "
                    f"the flag while a sample holds a value")


def check_rejects(problems):
    """Classified by actual payload length, never by the case's name."""
    short, over = {}, {}
    for c in cases():
        if not c.get("must_reject"):
            continue
        buf = bytes.fromhex(c["hex"])
        name = c["record"]
        base = SCHEMA["records"][
            name if name in STANDALONE else COMPOSITE[name][0]]["size"]
        (short if len(buf) < base else over).setdefault(name, []).append(c["name"])
    for record in sorted(STANDALONE + tuple(COMPOSITE)):
        if record not in short:
            problems.append(
                f"record {record}: no must-reject vector shorter than the base "
                f"record, so a decoder that reads past the end passes")
        if record not in over:
            problems.append(
                f"record {record}: no must-reject vector at or beyond the base "
                f"record length, so a decoder that ignores trailing bytes or a "
                f"miscounted batch passes")


def check_distinct_fields(problems, decoded):
    """Two same-typed fields never given different values are interchangeable."""
    for name, rec in SCHEMA["records"].items():
        for a, b in itertools.combinations(rec["fields"], 2):
            if a["type"] != b["type"] or a.get("reserved") or b.get("reserved"):
                continue
            if not any(
                    (lambda x, y: x is not None and y is not None and x != y)(
                        read_field(buf, name, a["name"], base),
                        read_field(buf, name, b["name"], base))
                    for case, buf in decoded
                    for r, base in instances(case, buf) if r == name):
                problems.append(
                    f"record {name}: {a['name']} and {b['name']} hold equal "
                    f"values in every vector, so a decoder reading either from "
                    f"the other's offset passes")


def check_unknown_enums(problems, decoded):
    for name, rec in SCHEMA["records"].items():
        for f in rec["fields"]:
            if not f.get("enum"):
                continue
            known = {m["value"] for m in SCHEMA["enums"][f["enum"]]["members"]}
            if not any(
                    (read_field(buf, name, f["name"], base) is not None
                     and read_field(buf, name, f["name"], base) not in known)
                    for case, buf in decoded
                    for r, base in instances(case, buf) if r == name):
                problems.append(
                    f"record {name}.{f['name']}: no vector carries an "
                    f"unrecognised {f['enum']} value, so a decoder that coerces "
                    f"unknown to a default passes")


def main():
    decoded = [(c, bytes.fromhex(c["hex"]))
               for c in cases() if not c.get("must_reject")]
    problems = []
    check_validity_gates(problems, decoded)
    check_presence_gates(problems, decoded)
    check_rejects(problems)
    check_distinct_fields(problems, decoded)
    check_unknown_enums(problems, decoded)

    if problems:
        for p in problems:
            print(f"UNTESTED: {p}", file=sys.stderr)
        print(f"\n{len(problems)} rule(s) the corpus cannot detect a violation "
              f"of. Add vectors; do not relax the rule.", file=sys.stderr)
        return 1
    print(f"Corpus exercises every gate, reject, field pair and enum "
          f"({len(decoded)} decodable vectors).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
