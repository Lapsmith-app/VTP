#!/usr/bin/env python3
"""Generate every derived VTP/1 artefact from schema/vtp1.yaml.

Outputs:
  SPEC.md                        field tables, substituted between markers
  reference/c/vtp1_generated.h   offsets, sizes, enums, bitmask constants
  conformance/vectors/*.json     byte vectors with expected decodes

Usage:
  python3 tools/generate.py            regenerate in place
  python3 tools/generate.py --check    fail if anything is out of date (CI)

The schema is the source of truth. Never hand-edit a generated artefact.
"""
import argparse, json, pathlib, re, struct, sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema" / "vtp1.yaml"
UUIDS = ROOT / "schema" / "uuids.json"

PACK = {"u8": "B", "i8": "b", "u16": "H", "i16": "h",
        "u32": "I", "i32": "i", "u64": "Q", "i64": "q"}

# The width each type actually occupies on the wire. `size` in the schema is
# checked against this rather than trusted: the two are consumed by different
# code paths -- documentation reads `size`, encoding reads `type` -- so nothing
# else in this generator would ever notice them disagreeing.
WIDTH = {"u8": 1, "i8": 1, "u16": 2, "i16": 2,
         "u32": 4, "i32": 4, "u64": 8, "i64": 8}

_pending: list[tuple[pathlib.Path, str]] = []
SCHEMA_ENUMS: dict = {}


def emit(path: pathlib.Path, text: str) -> None:
    _pending.append((path, text))


def flush(check: bool) -> int:
    stale = 0
    for path, text in _pending:
        current = path.read_text() if path.exists() else None
        if current == text:
            continue
        stale += 1
        rel = path.relative_to(ROOT)
        if check:
            print(f"STALE: {rel}", file=sys.stderr)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            print(f"wrote {rel}")
    return stale


# --------------------------------------------------------------------------
# schema validation
# --------------------------------------------------------------------------

PROPERTIES = {"read", "write", "write-without-response", "notify", "indicate"}
CCCD = {"none", "notify", "indicate"}
WRITE_TYPES = {"with-response", "without-response"}
SIDES = {"device", "client"}


def _validate_profile(schema, bitmasks):
    """SPEC.md 4.1 -- the attribute table and the capability implications.

    Unvalidated, `implies` naming a capability that does not exist would have
    generated a matrix demanding a bit no device can set, and the encoders
    would have enforced it.
    """
    problems = []
    profile = schema.get("profile")
    if not profile:
        return ["schema: no `profile` block; SPEC.md 4.1 cannot be generated"]
    cap_names = {b["name"] for b in bitmasks["capabilities"]["bits"]}
    info_fields = {f["name"] for f in schema["records"]["info"]["fields"]}

    seen = set()
    for ch in profile["characteristics"]:
        where = f"profile: characteristic {ch.get('name')!r}"
        if ch["name"] in seen:
            problems.append(f"{where}: named twice")
        seen.add(ch["name"])
        cap = ch["capability"]
        if cap is not None and cap not in cap_names:
            problems.append(f"{where}: unknown capability {cap!r}")
        for prop in ch["properties"]:
            if prop not in PROPERTIES:
                problems.append(f"{where}: unknown GATT property {prop!r}")
        if ch["cccd"] not in CCCD:
            problems.append(f"{where}: unknown cccd {ch['cccd']!r}")
        # A CCCD is what carries notifications and indications. Declaring one
        # without the matching property, or the property without the CCCD,
        # describes an attribute no stack can produce.
        if ch["cccd"] == "notify" and "notify" not in ch["properties"]:
            problems.append(f"{where}: cccd `notify` without the notify property")
        if ch["cccd"] == "indicate" and "indicate" not in ch["properties"]:
            problems.append(f"{where}: cccd `indicate` without the indicate property")
        if ch["cccd"] == "none" and ({"notify", "indicate"} & set(ch["properties"])):
            problems.append(
                f"{where}: notifies or indicates but declares no CCCD, which "
                f"no client could ever enable")
        wt = ch.get("write_type")
        if wt is not None and wt not in WRITE_TYPES:
            problems.append(f"{where}: unknown write_type {wt!r}")
        writes = {"write", "write-without-response"} & set(ch["properties"])
        if writes and wt is None:
            problems.append(f"{where}: writable but declares no write_type")
        if wt is not None and not writes:
            problems.append(f"{where}: declares a write_type but is not writable")
        for side in ("written_by", "read_by"):
            if ch[side] not in SIDES:
                problems.append(f"{where}: {side} is {ch[side]!r}, not one of "
                                f"{sorted(SIDES)}")
        if ch["record"] not in schema["records"]:
            problems.append(f"{where}: unknown record {ch['record']!r}")

    # Every characteristic UUID has a profile row and vice versa: an attribute
    # allocated but undescribed is an attribute nobody knows how to use.
    allocated = set(json.loads(UUIDS.read_text())["characteristics"])
    for name in sorted(allocated - seen):
        problems.append(f"profile: characteristic {name!r} is allocated a UUID "
                        f"but has no profile row")
    for name in sorted(seen - allocated):
        problems.append(f"profile: characteristic {name!r} has a profile row "
                        f"but no allocated UUID")

    for b in bitmasks["capabilities"]["bits"]:
        for implied in b.get("implies") or []:
            if implied not in cap_names:
                problems.append(f"capabilities bit {b['name']!r}: implies "
                                f"unknown capability {implied!r}")
            elif implied == b["name"]:
                problems.append(f"capabilities bit {b['name']!r}: implies itself")

    for cap, fields in profile["capacity"].items():
        if cap not in cap_names:
            problems.append(f"profile: capacity names unknown capability {cap!r}")
        for f in fields:
            if f not in info_fields:
                problems.append(f"profile: capacity {cap!r} names {f!r}, which "
                                f"is not a field of `info`")
    return problems


def _validate_protocol(schema):
    """The header block. Nothing downstream reads these twice, so a wrong one
    is generated straight into every artefact without argument."""
    problems = []
    proto = schema["protocol"]
    # SPEC.md 2: every field of every record, no exceptions. Both reference
    # codecs hard-code "<" and would silently ignore a change here, so the
    # schema must not be able to claim otherwise.
    if proto.get("endianness") != "little":
        problems.append(
            f"protocol.endianness is {proto.get('endianness')!r}; VTP/1 is "
            f"little-endian everywhere and both reference codecs assume it")
    if proto.get("major") != 1:
        problems.append(f"protocol.major is {proto.get('major')!r}; this file "
                        f"defines major version 1")
    minor = proto.get("minor")
    if not isinstance(minor, int) or isinstance(minor, bool) or not 0 <= minor <= 255:
        problems.append(f"protocol.minor is {minor!r}; it travels as a u8")
    mtu = proto.get("min_att_mtu")
    # 23 is the ATT default; below it the value is not an MTU at all. The
    # Monitor channel cap and every batching bound are derived from this, so a
    # nonsense value propagates into the C header and the corpus.
    if not isinstance(mtu, int) or isinstance(mtu, bool) or not 23 <= mtu <= 517:
        problems.append(
            f"protocol.min_att_mtu is {mtu!r}; an ATT MTU is 23..517")
    return problems


PARAM_TYPES = {"u8", "i8", "u16", "i16", "u32", "i32", "u64", "i64"}


def _validate_control(schema):
    """Opcode values and the parameter grammar SPEC.md 9 states.

    `params` is a table column in SPEC.md and a parser contract in every
    implementation. TIME_SYNC carried a literal em-dash here -- prose said it
    was parameterless, the schema said its parameter list was the character
    "-", and the generated table rendered that as a parameter named nothing.
    """
    problems = []
    seen = set()
    for op in schema["control"]["opcodes"]:
        where = f"control opcode {op.get('name')!r}"
        v = op["value"]
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 0xFF:
            problems.append(f"{where}: value {v!r} does not fit the u8 an "
                            f"opcode travels in")
        if op["name"] in seen:
            problems.append(f"{where}: named twice")
        seen.add(op["name"])
        if "response" not in op:
            problems.append(f"{where}: declares no response detail")
        if "capability" not in op:
            problems.append(f"{where}: declares no owning capability; use null "
                            f"for one every device answers")
        elif op["capability"] is not None:
            caps = {b["name"]
                    for b in schema["bitmasks"]["capabilities"]["bits"]}
            if op["capability"] not in caps:
                problems.append(f"{where}: capability {op['capability']!r} is "
                                f"not a bit of `capabilities`")
        spec = op.get("params", "")
        if not isinstance(spec, str):
            problems.append(f"{where}: params must be a string")
            continue
        if spec.strip() and spec.strip() != spec:
            problems.append(f"{where}: params {spec!r} has surrounding space")
        if not spec:
            continue
        for part in spec.split(","):
            part = part.strip()
            if part.count(":") != 1:
                problems.append(
                    f"{where}: parameter {part!r} is not `name:type`. A "
                    f"parameterless opcode is the empty string, never a dash.")
                continue
            pname, ptype = part.split(":")
            if not pname.isidentifier():
                problems.append(f"{where}: parameter name {pname!r} is not an "
                                f"identifier")
            if ptype not in PARAM_TYPES:
                problems.append(f"{where}: parameter {pname!r} has unknown "
                                f"type {ptype!r}")
    return problems


def validate(schema):
    """Structural invariants the schema must satisfy to mean anything.

    Without these the "source of truth" can be internally incoherent and every
    downstream check still passes: declaring gps_fix.lat as `size: 3, type:
    i32` published a three-byte 32-bit integer in SPEC.md while both reference
    implementations, the corpus and the mutation sweep stayed green, because
    the tables read `size` and the codecs read `type` and nothing compared
    them.
    """
    problems = []
    enums, bitmasks = schema["enums"], schema["bitmasks"]

    for name, en in enums.items():
        values = [m["value"] for m in en["members"]]
        if len(set(values)) != len(values):
            problems.append(f"enum {name}: duplicate member values")
        names = [m["name"] for m in en["members"]]
        if len(set(names)) != len(names):
            problems.append(f"enum {name}: duplicate member names")

    for name, bm in bitmasks.items():
        bits = [b["bit"] for b in bm["bits"]]
        if len(set(bits)) != len(bits):
            problems.append(f"bitmask {name}: duplicate bit positions")
        limit = bm["width"] * 8
        for b in bits:
            if not 0 <= b < limit:
                problems.append(
                    f"bitmask {name}: bit {b} outside its {bm['width']}-byte width")
        if "reserved_from" in bm and bits and bm["reserved_from"] <= max(bits):
            problems.append(
                f"bitmask {name}: reserved_from {bm['reserved_from']} collides "
                f"with assigned bit {max(bits)}")

    for name, rec in schema["records"].items():
        fields = rec["fields"]

        seen = [f["name"] for f in fields]
        if len(set(seen)) != len(seen):
            problems.append(f"record {name}: duplicate field names")

        mask = rec.get("validity")
        if mask and mask not in bitmasks:
            problems.append(f"record {name}: unknown validity bitmask {mask!r}")
        valid_names = {b["name"] for b in bitmasks.get(mask, {}).get("bits", [])}

        for f in fields:
            where = f"record {name}.{f['name']}"
            if f["type"] not in WIDTH:
                problems.append(f"{where}: unknown type {f['type']!r}")
                continue
            if f["size"] != WIDTH[f["type"]]:
                problems.append(
                    f"{where}: declared size {f['size']} but type {f['type']} "
                    f"occupies {WIDTH[f['type']]} bytes")
            if f.get("enum") and f["enum"] not in enums:
                problems.append(f"{where}: unknown enum {f['enum']!r}")
            if f.get("bitmask") and f["bitmask"] not in bitmasks:
                problems.append(f"{where}: unknown bitmask {f['bitmask']!r}")
            pb = f.get("presence_bit")
            if pb is not None:
                pres = rec.get("presence")
                if not pres:
                    problems.append(
                        f"{where}: presence_bit {pb!r} but the record declares "
                        f"no presence source")
                elif pb not in pres["bits"]:
                    problems.append(
                        f"{where}: presence_bit {pb!r} is not declared in the "
                        f"record's presence bits")
            vb = f.get("valid_bit")
            if vb is not None:
                if not mask:
                    problems.append(
                        f"{where}: valid_bit {vb!r} but the record declares no "
                        f"validity bitmask")
                elif vb not in valid_names:
                    problems.append(
                        f"{where}: valid_bit {vb!r} is not a bit of {mask}")

        pres = rec.get("presence")
        if pres:
            src = schema["records"].get(pres["record"])
            if src is None:
                problems.append(
                    f"record {name}: presence names unknown record "
                    f"{pres['record']!r}")
            elif not any(g["name"] == pres["field"] for g in src["fields"]):
                problems.append(
                    f"record {name}: presence names unknown field "
                    f"{pres['record']}.{pres['field']}")

        # Layout: every byte of the record accounted for exactly once. A gap is
        # an undeclared byte and an overlap is two fields sharing storage;
        # both would encode without complaint.
        cursor = 0
        for f in sorted(fields, key=lambda g: g["offset"]):
            if f["offset"] < cursor:
                problems.append(
                    f"record {name}.{f['name']}: offset {f['offset']} overlaps "
                    f"the preceding field, which ends at {cursor}")
            elif f["offset"] > cursor:
                problems.append(
                    f"record {name}: bytes {cursor}..{f['offset'] - 1} are not "
                    f"covered by any field")
            cursor = max(cursor, f["offset"] + f["size"])
        if cursor != rec["size"]:
            problems.append(
                f"record {name}: fields cover {cursor} bytes but size is "
                f"{rec['size']}")

    opcodes = [o["value"] for o in schema["control"]["opcodes"]]
    if len(set(opcodes)) != len(opcodes):
        problems.append("control: duplicate opcode values")

    problems += _validate_profile(schema, bitmasks)
    problems += _validate_protocol(schema)
    problems += _validate_control(schema)

    if problems:
        for p in problems:
            print(f"SCHEMA: {p}", file=sys.stderr)
        sys.exit(f"\nschema/vtp1.yaml is not internally consistent "
                 f"({len(problems)} problem(s)).")


# --------------------------------------------------------------------------
# spec tables
# --------------------------------------------------------------------------

def field_rows(schema, rec):
    validity = schema["bitmasks"].get(rec.get("validity"), {})
    bit_of = {b["name"]: b["bit"] for b in validity.get("bits", [])}
    rows = []
    for f in rec["fields"]:
        notes = []
        if f.get("units"):
            notes.append(f"`{f['units']}`")
        if f.get("scale"):
            notes.append(f"scale {f['scale']:g}")
        # The description leads the qualifiers: what the field IS is what a
        # reader needs first, and a validity clause in front of it buries it.
        if f.get("desc"):
            notes.append(f["desc"])
        if f.get("enum"):
            notes.append(f"enum `{f['enum']}`")
        if f.get("bitmask"):
            notes.append(f"bitmask `{f['bitmask']}`")
        if f.get("reserved"):
            notes.append("**reserved — MUST be zero**")
        vb = f.get("valid_bit")
        if vb is not None:
            notes.append(f"valid when `validity` bit {bit_of[vb]} (`{vb}`) is set")
        pb = f.get("presence_bit")
        if pb is not None:
            pres = rec["presence"]
            notes.append(
                f"present when `{pres['record']}.{pres['field']}` bit "
                f"{pres['bits'][pb]} (`{pb}`) is set")
        rows.append(f"| {f['offset']} | {f['size']} | `{f['type']}` | `{f['name']}` | "
                    f"{'; '.join(notes) if notes else '—'} |")
    return rows


def spec_tables(schema):
    out = {}
    for name, rec in schema["records"].items():
        lines = [f"*{rec['desc']}*", ""] if rec.get("desc") else []
        size = (f"{rec['size']} bytes + `{rec['variable']}`"
                if rec.get("variable") else f"{rec['size']} bytes")
        lines += [f"Total: **{size}**. All fields little-endian.", "",
                  "| Off | Size | Type | Field | Notes |",
                  "| --- | --- | --- | --- | --- |"]
        lines += field_rows(schema, rec)
        out[name] = "\n".join(lines)

    for name, bm in schema["bitmasks"].items():
        lines = ["| Bit | Name | Meaning |", "| --- | --- | --- |"]
        for b in bm["bits"]:
            meaning = b.get("desc", "")
            if b.get("implies"):
                # Rendered here as well as in the 4.1 matrix, because a reader
                # looking up one bit must not have to know the matrix exists.
                req = ", ".join(f"`{i}`" for i in b["implies"])
                meaning = ((meaning + " ") if meaning else "") + \
                          f"**Requires {req}.**"
            lines.append(f"| {b['bit']} | `{b['name']}` | {meaning or '—'} |")
        if "reserved_from" in bm:
            lines.append(f"| {bm['reserved_from']}+ | *reserved* | MUST be zero on transmit; "
                         f"MUST be ignored on receive |")
        out[f"bitmask:{name}"] = "\n".join(lines)

    for name, en in schema["enums"].items():
        lines = ["| Value | Name | Meaning |", "| --- | --- | --- |"]
        for m in en["members"]:
            lines.append(f"| {m['value']} | `{m['name']}` | {m.get('desc', '—')} |")
        lines.append("| *other* | *unknown* | MUST decode as unknown, never as a default |")
        out[f"enum:{name}"] = "\n".join(lines)

    lines = ["| Opcode | Command | Needs | Params | Response detail | Notes |",
             "| --- | --- | --- | --- | --- | --- |"]
    for op in schema["control"]["opcodes"]:
        params = f"`{op['params']}`" if op["params"] else "—"
        # Every opcode declares its response detail. Leaving that to prose is
        # what made three of these unimplementable.
        resp = op.get("response")
        if resp is None:
            sys.exit(f"control: opcode {op['name']} declares no response detail")
        resp = f"`{resp}`" if resp else "—"
        # ...and every opcode declares the capability that owns it, for the
        # same reason: without it, "what does this bit change" had no answer.
        if "capability" not in op:
            sys.exit(f"control: opcode {op['name']} declares no capability")
        cap = f"`{op['capability']}`" if op["capability"] else "—"
        lines.append(f"| `0x{op['value']:02X}` | `{op['name']}` | {cap} | "
                     f"{params} | {resp} | {op.get('desc', '—')} |")
    out["control"] = "\n".join(lines)

    # Which records carry an extension trailer, straight from the schema, so
    # SPEC.md cannot claim a record is extensible when the codecs disagree --
    # the exact failure the old "new fields go in extension records" wording
    # produced for eight of nine records.
    lines = ["| Record | Extensible | Appears |", "| --- | --- | --- |"]
    FREQ = {"info": "Once per connection",
            "gps_fix": "One per notification",
            "can_header": "One per notification",
            "can_record": "Up to 4000 per second",
            "imu_header": "One per notification",
            "imu_sample": "Up to 833 per second",
            "can_list_page": "One per CAN_LIST page",
            "can_subscription": "One per table entry",
            "link_params": "On request",
            "power_state": "On request"}
    for name, rec in schema["records"].items():
        mark = ("**Yes** — `ext_count` trailer (§5.5)" if rec.get("extensible")
                else "No — closed for major version 1")
        lines.append(f"| `{name}` | {mark} | {FREQ.get(name, '—')} |")
    out["extensibility"] = "\n".join(lines)

    # SPEC.md 4.1 -- the one place capability implications, the attribute
    # table, GATT properties, CCCDs, write type and direction are stated. They
    # used to be stated nowhere: the specification defined every capability bit
    # independently, conformance/run.py made CAN and Monitor imply Control
    # anyway, and nothing at all said which characteristic had which
    # properties or who wrote to it.
    caps = {b["name"]: b for b in schema["bitmasks"]["capabilities"]["bits"]}
    profile = schema["profile"]
    capacity = profile["capacity"]

    lines = ["| Characteristic | Capability | Properties | CCCD | Written by | "
             "Read by | When the capability bit is clear |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for ch in profile["characteristics"]:
        cap = ch["capability"]
        bit = f"bit {caps[cap]['bit']} (`{cap}`)" if cap else "— always present"
        props = ", ".join(f"`{x}`" for x in ch["properties"])
        if ch.get("write_type"):
            props += f" (write {ch['write_type']})"
        # A CCCD is an attribute, so it is part of the fixed table like every
        # other one: always present, whatever the capability bit says. The
        # column says who enables it and when, not whether it exists.
        cccd = {"none": "—",
                "notify": "always present; client enables it for a set bit",
                "indicate": "always present; client enables it for a set bit"
                }[ch["cccd"]]
        lines.append(f"| `{ch['name']}` | {bit} | {props} | {cccd} | "
                     f"{ch['written_by']} | {ch['read_by']} | {ch['inert']} |")
    out["profile:attributes"] = "\n".join(lines)

    lines = ["| Bit | Capability | Requires | Capacity fields that MUST be zero "
             "when clear |", "| --- | --- | --- | --- |"]
    for b in schema["bitmasks"]["capabilities"]["bits"]:
        implies = b.get("implies") or []
        req = ", ".join(f"bit {caps[i]['bit']} (`{i}`)" for i in implies) or "—"
        zeroed = ", ".join(f"`{f}`" for f in capacity.get(b["name"], [])) or "—"
        lines.append(f"| {b['bit']} | `{b['name']}` | {req} | {zeroed} |")
    out["profile:capabilities"] = "\n".join(lines)

    # Appendix A. Hand-written until now, and wrong: it listed fix_flags bits
    # 4-7 as reserved after bit 4 was assigned to `solution_epoch`. A table
    # restating what the schema already says is a table that drifts, so it is
    # derived instead.
    lines = ["| Location | Reserved | Purpose |", "| --- | --- | --- |"]
    for name, bm in schema["bitmasks"].items():
        rf = bm.get("reserved_from")
        if rf is None:
            continue
        top = bm["width"] * 8 - 1
        where = [f"`{r}.{f['name']}`" for r, rec in schema["records"].items()
                 for f in rec["fields"] if f.get("bitmask") == name]
        span = f"bit {rf}" if rf == top else f"bits {rf}–{top}"
        lines.append(f"| {', '.join(where) or '`' + name + '`'} | {span} | "
                     f"{bm.get('reserved_purpose', '—')} |")
    for rname, rec in schema["records"].items():
        for f in rec["fields"]:
            if not f.get("reserved"):
                continue
            size = f"{f['size']} byte" + ("s" if f["size"] != 1 else "")
            lines.append(f"| `{rname}.{f['name']}` | {size} | "
                         f"{f.get('desc', '—')} |")
    lines.append("| Extension types | `0x80`–`0xFF` | Vendor-private; this "
                 "specification MUST NOT assign them (§5.5) |")
    out["reserved_space"] = "\n".join(lines)

    uu = json.loads(UUIDS.read_text())

    # Derived from the allocation itself: hand-written hex in prose is exactly
    # the thing that silently disagrees with the table three commits later.
    svc4 = bytes.fromhex(uu["service"]["vtp1"][:8])
    chr4 = bytes.fromhex(next(iter(uu["characteristics"].values()))[:8])
    out["family_prefix"] = (
        f"Every VTP service UUID begins with the four bytes "
        f"`{' '.join(f'{b:02X}' for b in svc4[:3])} MM`, where "
        f"`{' '.join(f'{b:02X}' for b in svc4[:3])}` is ASCII "
        f'`"{svc4[:3].decode()}"` and `MM` is the major version. '
        f"Characteristic UUIDs begin "
        f"`{' '.join(f'{b:02X}' for b in chr4[:3])} NN` "
        f'(ASCII `"{chr4[:3].decode()}"` and an index) and share the '
        f"service's remaining twelve bytes.")

    lines = ["| Role | UUID |", "| --- | --- |",
             f"| Service (VTP/1) | `{uu['service']['vtp1']}` |"]
    for n, v in uu["characteristics"].items():
        lines.append(f"| Characteristic `{n}` | `{v}` |")
    out["uuids"] = "\n".join(lines)
    return out


def substitute(path: pathlib.Path, blocks: dict) -> None:
    text = path.read_text()
    used = set()

    def repl(m):
        key = m.group(1)
        used.add(key)
        if key not in blocks:
            sys.exit(f"{path.name}: no generated block named '{key}'")
        return f"<!-- BEGIN GENERATED: {key} -->\n{blocks[key]}\n<!-- END GENERATED: {key} -->"

    text = re.sub(r"<!-- BEGIN GENERATED: (\S+) -->.*?<!-- END GENERATED: \1 -->",
                  repl, text, flags=re.S)
    missing = set(blocks) - used
    if missing:
        sys.exit(f"{path.name}: generated blocks never placed: {sorted(missing)}")
    emit(path, text)


# --------------------------------------------------------------------------
# C header
# --------------------------------------------------------------------------

def c_header(schema):
    uu = json.loads(UUIDS.read_text())
    p = schema["protocol"]
    L = ["/* Generated by tools/generate.py from schema/vtp1.yaml. Do not edit. */",
         "#ifndef VTP1_GENERATED_H", "#define VTP1_GENERATED_H", "",
         f"#define VTP_MAJOR {p['major']}", f"#define VTP_MINOR {p['minor']}",
         f"#define VTP_MIN_ATT_MTU {p['min_att_mtu']}", "",
         f'#define VTP_SERVICE_UUID "{uu["service"]["vtp1"]}"']
    for n, v in uu["characteristics"].items():
        L.append(f'#define VTP_CHAR_{n.upper()}_UUID "{v}"')
    L.append("")
    for name, rec in schema["records"].items():
        U = name.upper()
        L.append(f"/* {rec.get('desc', name)} */")
        L.append(f"#define VTP_{U}_SIZE {rec['size']}")
        for f in rec["fields"]:
            L.append(f"#define VTP_{U}_OFF_{f['name'].upper()} {f['offset']}")
        L.append("")
    for name, en in schema["enums"].items():
        L.append(f"typedef enum {{")
        for m in en["members"]:
            L.append(f"    VTP_{name.upper()}_{m['name'].upper()} = {m['value']},")
        L.append(f"}} vtp_{name}_t;")
        L.append("")
    for name, bm in schema["bitmasks"].items():
        for b in bm["bits"]:
            L.append(f"#define VTP_{name.upper()}_{b['name'].upper()} (1u << {b['bit']})")
        if "reserved_from" in bm:
            # SPEC.md 2 -- reserved bits are zero on transmit. Generated as a
            # mask rather than written by hand in each encoder, so a bit
            # assigned in a later minor leaves the reserved region by editing
            # the schema and nothing else.
            width = bm["width"] * 8
            assigned = ((1 << width) - 1) & ~((1 << bm["reserved_from"]) - 1)
            suffix = "u" if width <= 32 else "ull"
            L.append(f"#define VTP_{name.upper()}_RESERVED "
                     f"0x{assigned:0{width // 4}X}{suffix}")
            L.append(f"#define VTP_{name.upper()}_KNOWN "
                     f"(~(uint32_t)VTP_{name.upper()}_RESERVED)"
                     if width <= 32 else
                     f"#define VTP_{name.upper()}_KNOWN "
                     f"(~(uint64_t)VTP_{name.upper()}_RESERVED)")
        L.append("")

    # SPEC.md 4.1 -- the capability matrix, as data a codec can loop over. Both
    # references used to have no expression of it at all, so an Info claiming
    # CAN without Control encoded and decoded without complaint.
    caps = {b["name"]: b for b in schema["bitmasks"]["capabilities"]["bits"]}
    L.append("/* SPEC.md 4.1 -- a capability bit and every bit it requires. */")
    L.append("typedef struct { uint32_t bit, requires_; const char *name; }"
             " vtp_capability_rule_t;")
    L.append("#define VTP_CAPABILITY_RULES { \\")
    for b in schema["bitmasks"]["capabilities"]["bits"]:
        req = 0
        for i in b.get("implies") or []:
            req |= 1 << caps[i]["bit"]
        L.append(f'    {{ (1u << {b["bit"]}), 0x{req:08X}u, "{b["name"]}" }}, \\')
    L.append("}")
    L.append(f"#define VTP_CAPABILITY_RULE_COUNT "
             f"{len(schema['bitmasks']['capabilities']['bits'])}")
    L.append("")

    L.append("/* SPEC.md 4.1 -- info fields that MUST be zero when their")
    L.append(" * capability bit is clear. Offset and size, so one loop covers all. */")
    L.append("typedef struct { uint32_t bit; uint8_t offset, size; const char *field; }"
             " vtp_capacity_rule_t;")
    info_fields = {f["name"]: f for f in schema["records"]["info"]["fields"]}
    rules = []
    for cap, fields in schema["profile"]["capacity"].items():
        for fname in fields:
            f = info_fields[fname]
            rules.append(f'    {{ (1u << {caps[cap]["bit"]}), {f["offset"]}, '
                         f'{f["size"]}, "{fname}" }}, \\')
    L.append("#define VTP_CAPACITY_RULES { \\")
    L += rules
    L.append("}")
    L.append(f"#define VTP_CAPACITY_RULE_COUNT {len(rules)}")
    L.append("")
    L += ["#endif /* VTP1_GENERATED_H */", ""]
    return "\n".join(L)


# --------------------------------------------------------------------------
# conformance vectors
# --------------------------------------------------------------------------

def encode(schema, record, values):
    rec = schema["records"][record]
    buf = bytearray(rec["size"])
    for f in rec["fields"]:
        v = values.get(f["name"], 0)
        struct.pack_into("<" + PACK[f["type"]], buf, f["offset"], v)
    return bytes(buf)


def _bit_of(schema, record):
    """name -> bit, for the validity bitmask governing `record`."""
    bm = schema["records"][record]["validity"]
    return {b["name"]: b["bit"] for b in schema["bitmasks"][bm]["bits"]}


def _gated_fields(schema, record, values):
    """Fields of `record` whose validity bit is clear, i.e. MUST read absent."""
    validity = values.get("validity", 0)
    bit_of = _bit_of(schema, record)
    return sorted(f["name"] for f in schema["records"][record]["fields"]
                  if f.get("valid_bit") is not None
                  and not (validity & (1 << bit_of[f["valid_bit"]])))


def _gate(schema, record, values):
    """Zero every field of `record` whose validity bit is clear."""
    out = dict(values)
    for name in _gated_fields(schema, record, values):
        out[name] = 0
    return out


def reserved_mask(schema, bitmask):
    """The bits of `bitmask` this version has assigned a meaning to.

    SPEC.md 2 -- reserved bits are zero on transmit. Whole reserved FIELDS were
    already zeroed by both encoders; the reserved portion of a bitmask was not,
    so an encoder handed a capabilities word with bit 19 set, or a gps validity
    word with bit 30 set, transmitted it. Every conforming receiver is required
    to ignore those bits, which is exactly why writing them is forbidden: they
    are the only bytes on the wire that a later minor version may redefine.
    """
    spec = schema["bitmasks"][bitmask]
    rf = spec.get("reserved_from")
    if rf is None:
        return (1 << (spec["width"] * 8)) - 1
    return (1 << rf) - 1


def _normalise(schema, record, values):
    """What a conforming encoder MUST turn these values into on transmit.

    Three rules in one place, so "canonical" means one thing everywhere: a
    field behind a cleared validity bit is zero (5.1), a reserved field is zero
    (2), and the reserved portion of a bitmask is zero (2). The last of the
    three had no expression anywhere and neither reference encoder applied it.
    """
    out = dict(values)
    rec = schema["records"][record]
    if rec.get("validity"):
        for name in _gated_fields(schema, record, values):
            out[name] = 0
    for f in rec["fields"]:
        if f.get("reserved"):
            out[f["name"]] = 0
        elif f.get("bitmask"):
            out[f["name"]] = (out.get(f["name"], 0)
                              & reserved_mask(schema, f["bitmask"]))
    return out


def case(schema, record, name, values, desc, *, extra=b"", reject=None, note=None,
         canonical=True):
    # SPEC.md 5.1: a field whose validity bit is clear MUST be written as zero.
    # Applied here so the corpus cannot hold a non-conforming vector by
    # accident -- it already did once, and only the encoder round-trip found it.
    #
    # `canonical=False` keeps the ungated bytes on the wire, modelling a device
    # that leaves stale data behind a cleared bit. Such a case is NOT exempt
    # from the round-trip: it asserts that re-encoding NORMALISES those bytes to
    # zero, which is the only coverage the encoder's gating rule gets. Exempting
    # it left that rule completely untested.
    gated = _normalise(schema, record, values)
    if canonical:
        # A canonical vector IS its own normal form. Asserting that rather
        # than assuming it means a case cannot claim to be canonical while
        # carrying a stale value or a reserved bit -- the corpus held one such
        # case, and only the encoder round-trip found it.
        values = gated

    raw = encode(schema, record, values) + extra
    rec = schema["records"][record]
    expect = {f["name"]: values.get(f["name"], 0) for f in rec["fields"]}
    scaled = {}
    for f in rec["fields"]:
        if f.get("scale"):
            scaled[f["name"]] = round(expect[f["name"]] * float(f["scale"]), 9)
    # Derived assertions the schema cannot express as a field, but which the
    # specification states as a requirement. Without these the corpus checks
    # only what a field holds, never what a decoder must CONCLUDE from it --
    # and SPEC.md 11.4's "an unknown enum value MUST stay unknown" is exactly
    # such a conclusion. It went unasserted in the first version of this
    # corpus, so all three reference decoders could have coerced an unknown
    # fix_type to 3D and still passed.
    # The set a conforming decoder MUST report as absent. This is the
    # protocol's central rule (SPEC.md 1.1) and without this it was
    # asserted only by prose: every implementation could return zero for a
    # gated field and the corpus would not notice.
    #
    # Driven off the record's `validity` key rather than its name, so a record
    # that gains a validity mask in a later minor is covered without editing
    # this -- the rule is the protocol's, not gps_fix's.
    c_absent = None
    if rec.get("validity"):
        c_absent = _gated_fields(schema, record, values)

    # Asserted on EVERY case with an enum field, not just the unknown-value
    # one, so a decoder that reports everything as unknown fails too.
    for f in rec["fields"]:
        if f.get("enum"):
            known = {m["value"] for m in SCHEMA_ENUMS[f["enum"]]}
            expect[f["name"] + "_known"] = expect[f["name"]] in known

    c = {"name": name, "desc": desc, "record": record, "hex": raw.hex()}
    if reject:
        c["must_reject"] = reject
    else:
        c["expect"] = expect
        if c_absent is not None:
            c["expect_absent"] = c_absent
        if scaled:
            c["expect_scaled"] = scaled
    if note:
        c["note"] = note
    if not canonical and not reject:
        c["canonical"] = False
        # What a conforming encoder MUST turn these bytes into: SPEC.md 5.1's
        # gating, SPEC.md 2's reserved fields, and SPEC.md 2's reserved bitmask
        # bits.
        c["expect_roundtrip_hex"] = (encode(schema, record, gated) + extra).hex()
    return c


# SPEC.md 2, in the producer direction: the reserved portion of a bitmask is
# ZERO on transmit. Generated per bitmask FIELD rather than hand-picked.
#
# The hand-picked version covered four of the eight, and the C encoder then
# shipped three unmasked fields -- can_header.flags, imu_header.flags and
# info.clock_flags -- while Python masked them, so the two references produced
# DIFFERENT BYTES from the same input. That is the one defect class this whole
# repository exists to prevent, and it survived because the corpus was asking
# about the fields somebody remembered.
#
# The asymmetry that caused it is permanent: the Python encoder applies the
# rule generically by walking the schema, and the C encoder writes it out per
# field because it is a separate translation unit with no reflection. So the
# corpus closes it instead. A bitmask field added to the schema gets a case
# here automatically, and an encoder that forgets to mask it fails.
def _reserved_case(schema, record, field, value):
    """The producer input and the bytes it MUST produce, for one field."""
    if record == "gps_fix":
        clean = dict(seq=1, validity=0, ext_count=0)
        return ({"fix": dict(clean, **{field: value})},
                encode(schema, "gps_fix", dict(clean, **{field: value})))
    if record == "can_header":
        # One record: SPEC.md 6.2 forbids an empty batch, so a case about the
        # reserved bits of `flags` still has to be a batch that could exist.
        clean = dict(seq=1, dropped=0, t_base=0, count=1, reserved=0)
        frame = dict(dt=0, id=0x1A0, extended=False, fd=False, rtr=False,
                     len=1, payload="00")
        return ({"header": dict(clean, **{field: value}), "records": [frame]},
                encode(schema, "can_header", dict(clean, **{field: value}))
                + encode(schema, "can_record", dict(dt=0, id=0x1A0, len=1))
                + b"\x00")
    if record == "imu_header":
        clean = dict(seq=1, dropped=0, t_base=0, period=1000, count=1, reserved=0)
        sample = dict(ax=1, ay=2, az=3, gx=4, gy=5, gz=6)
        hdr = dict(clean, **{field: value})
        # An imu_sample is gated by the presence flags, so the expected bytes
        # depend on which of them survive the mask.
        gated = dict(sample)
        if not hdr["flags"] & 0x01:
            gated.update(ax=0, ay=0, az=0)
        if not hdr["flags"] & 0x02:
            gated.update(gx=0, gy=0, gz=0)
        return ({"header": hdr, "samples": [sample]},
                encode(schema, "imu_header", hdr)
                + encode(schema, "imu_sample", gated))
    if record == "info":
        clean = dict(protocol_major=1, protocol_minor=0, capabilities=1,
                     gps_rate_hz=10, gps_max_rate_hz=10, max_notify_bytes=244)
        return (dict(clean, **{field: value}),
                encode(schema, "info", dict(clean, **{field: value})))
    if record == "monitor_value":
        hdr = dict(seq=1, count=1, reserved=0)
        val = dict(slot=2, value=42, **{field: value})
        # The value is gated by the present bit, exactly as on any other record.
        gated = dict(val)
        if not value & 0x01:
            gated["value"] = 0
        return ({"header": hdr, "values": [val]},
                encode(schema, "monitor_header", hdr)
                + encode(schema, "monitor_value", gated))
    if record in ("link_params", "power_state"):
        return ({field: value}, encode(schema, record, {field: value}))
    sys.exit(f"reserved_bit_cases: no builder for record {record!r}; a bitmask "
             f"field was added and its producer case cannot be generated")


# Where a producer case for a record is filed, when the runner contract names
# the batch rather than the header.
# A bitmask whose assigned bits cannot all be set at once, and the largest
# combination that can.
LEGAL_ASSIGNED = {
    "fix_flags": 0b0001_1101,   # differential, rtk_fixed, disciplined, epoch
}


RESERVED_CASE_RECORD = {
    "can_header": "can_batch", "imu_header": "imu_batch",
    "monitor_value": "monitor_update",
}


def reserved_bit_cases(schema):
    """One producer case per bitmask field: set a reserved bit, require zero."""
    cases = []
    for rname, rec in schema["records"].items():
        for f in rec["fields"]:
            bm = f.get("bitmask")
            if not bm:
                continue
            spec = schema["bitmasks"][bm]
            reserved_from = spec.get("reserved_from")
            top = spec["width"] * 8 - 1
            if reserved_from is None or reserved_from > top:
                continue
            # Every assigned bit, unless the bitmask has a cross-field rule
            # that "all of them at once" would break. fix_flags does:
            # rtk_float and rtk_fixed are mutually exclusive (SPEC.md 5.3), so
            # the baseline here is differential + rtk_fixed + clock_disciplined
            # + solution_epoch, which is a real receiver's flags rather than an
            # impossible one. The case is about the RESERVED bits, and a
            # baseline the encoder must refuse for another reason tests nothing.
            assigned = LEGAL_ASSIGNED.get(bm, (1 << reserved_from) - 1)
            # The TOP reserved bit, because an encoder masking at the wrong
            # width passes on the lowest reserved bit and fails on the highest.
            dirty = assigned | (1 << top)
            payload, expect = _reserved_case(schema, rname, f["name"], dirty)
            _, clean = _reserved_case(schema, rname, f["name"], assigned)
            cases.append({
                "name": f"reserved-bits-{rname}-{f['name']}".replace("_", "-"),
                "record": RESERVED_CASE_RECORD.get(rname, rname),
                "must_refuse": False,
                "desc": f"SPEC.md 2 -- {rname}.{f['name']} bits "
                        f"{reserved_from}-{top} are reserved in VTP/1.0, so an "
                        f"encoder MUST zero them rather than publish a meaning "
                        f"this version has not assigned. Bit {top} is set "
                        f"because an encoder masking at the wrong width passes "
                        f"on the lowest reserved bit and fails on the highest.",
                "input": payload,
                "expect_hex": clean.hex(),
            })
            assert expect is not None
    return cases


def vectors(schema):
    files = {}

    # ---- GPS -------------------------------------------------------------
    V = {b["name"]: 1 << b["bit"] for b in schema["bitmasks"]["gps_validity"]["bits"]}
    full = (V["t_utc"] | V["t_utc_resolved"] | V["position"] | V["alt_msl"] |
            V["alt_ellipsoid"] | V["velocity"] | V["head_mot"] | V["h_acc"] |
            V["v_acc"] | V["s_acc"] | V["p_dop"] | V["num_sv"])
    nominal = dict(
        seq=1, dropped=0, validity=full, t_device=123_456_789, t_utc=1_766_000_000_000,
        lat=515_074_000, lon=-1_397_000, alt_msl=35_000, alt_ellipsoid=80_500,
        vel_n=25_000, vel_e=-3_200, vel_d=150, head_mot=35_400_000,
        h_acc=850, v_acc=1_400, s_acc=90, p_dop=140, fix_type=3, num_sv=17,
        fix_flags=0b1001, ext_count=0)
    gps = [
        case(schema, "gps_fix", "3d-fix-nominal", nominal,
             "A complete 3D fix with every validity bit set."),
        case(schema, "gps_fix", "no-fix-all-absent",
             dict(seq=2, dropped=0, validity=0, fix_type=0),
             "No solution. Every optional field is zero AND its validity bit is clear. "
             "A decoder MUST report absent, not 0.0 degrees at the equator."),
        case(schema, "gps_fix", "position-without-accuracy",
             dict(nominal, seq=3, validity=V["position"] | V["t_utc"],
                  h_acc=0, v_acc=0, s_acc=0, p_dop=0, num_sv=0),
             "Position valid, accuracy fields absent. A decoder MUST NOT grade this fix "
             "on a zeroed accuracy field.",
             note="This is the VTP/1 answer to the zero-fill ambiguity in the protocol "
                  "it replaces, where 0 and the sentinel were indistinguishable."),
        case(schema, "gps_fix", "stale-values-behind-cleared-bits",
             dict(nominal, seq=10, validity=V["position"] | V["t_utc"]),
             "A non-conforming device that clears validity bits but leaves the "
             "previous fix's values in those bytes. A decoder MUST report every "
             "gated field as absent on the strength of the bit alone, and MUST "
             "NOT consult the value.",
             canonical=False,
             note="Not byte-canonical, so exempt from the encoder round-trip: a "
                  "conforming encoder normalises these bytes to zero. This is "
                  "the one vector that asserts a decoder trusts the bitmask "
                  "rather than the payload."),
        case(schema, "gps_fix", "every-bit-cleared-values-retained",
             dict(nominal, seq=11, validity=0),
             "The same firmware bug as above taken to its limit: every validity bit "
             "clear, every byte still carrying the previous fix. A decoder MUST report "
             "all eleven gated fields absent, including position and t_utc.",
             canonical=False,
             note="The vector above leaves position and t_utc VALID, so it cannot "
                  "exercise their encoder gates. Mutation testing found that hole: "
                  "dropping the POSITION or T_UTC gate passed the entire corpus."),
        case(schema, "gps_fix", "southern-western-hemisphere",
             dict(nominal, seq=4, lat=-337_000_000, lon=1_511_000_000),
             "Negative latitude and large positive longitude; catches unsigned reads."),
        case(schema, "gps_fix", "velocity-negative-all-axes",
             dict(nominal, seq=5, vel_n=-31_000, vel_e=-12_500, vel_d=-2_000),
             "All three NED components negative; catches sign handling per axis."),
        case(schema, "gps_fix", "seq-wrap", dict(nominal, seq=65535),
             "seq at its maximum. The next fix is seq 0; a decoder MUST NOT treat that as loss."),
        case(schema, "gps_fix", "dropped-nonzero", dict(nominal, seq=9, dropped=12),
             "The device discarded 12 fixes. A decoder MUST surface this, not ignore it."),
        case(schema, "gps_fix", "dropped-saturated",
             dict(nominal, seq=12, dropped=65535),
             "dropped at its ceiling. SPEC.md 8.3: the counter saturates and MUST NOT "
             "wrap, so a receiver reads this as 'at least 65535', never as exactly "
             "that many.",
             note="A wrapping drop counter reads 0 after 65536 discards -- perfect "
                  "health at the moment the device is losing data fastest. Saturation "
                  "is the 1.1 rule applied to a counter."),
        case(schema, "gps_fix", "unknown-fix-type", dict(nominal, seq=6, fix_type=200),
             "An enum value from a future minor. A decoder MUST report unknown, "
             "and MUST NOT fall back to 3D.",
             note="Falling back to a plausible default is the sentinel mistake in a "
                  "different costume."),
        # SPEC.md 5.3 -- the two RTK bits are exclusive, and either implies
        # differential. Both were legal until this existed, and the natural
        # client reading of both-set is "fixed wins", which upgrades a device's
        # accuracy claim on the strength of a bug.
        case(schema, "gps_fix", "rtk-float-and-fixed",
             dict(nominal, seq=11, fix_flags=0b0000_0111),
             "Both RTK bits set. MUST be rejected: a carrier-phase solution "
             "has either resolved its integer ambiguities or it has not, so "
             "the pair is a quality claim that means nothing.",
             reject="rtk-both"),
        case(schema, "gps_fix", "rtk-without-differential",
             dict(nominal, seq=12, fix_flags=0b0000_0100),
             "rtk_fixed without differential. MUST be rejected: an RTK "
             "solution IS a differentially corrected one, so this describes "
             "no receiver.",
             reject="rtk-without-differential"),
        case(schema, "gps_fix", "rtk-fixed-well-formed",
             dict(nominal, seq=13, fix_flags=0b0000_1101),
             "rtk_fixed with differential and a disciplined clock, which is "
             "what a working RTK receiver reports."),
        case(schema, "gps_fix", "reserved-validity-bits-set",
             dict(nominal, seq=7, validity=full | (1 << 20)),
             "A future minor set validity bit 20. A decoder MUST ignore the unknown bit "
             "and decode every known field normally. Rejecting here breaks forward "
             "compatibility. A VTP/1.0 encoder MUST NOT reproduce the bit: SPEC.md 2 "
             "reserves it, and re-encoding therefore normalises it away.",
             canonical=False,
             note="Both halves matter and they pull opposite ways. On RECEIVE a "
                  "reserved bit is ignored, so the known fields decode. On TRANSMIT it "
                  "is zero, because those bits are the only ones a later minor may "
                  "redefine and a 1.0 encoder that emits one has published a claim it "
                  "cannot make. Neither reference encoder masked them until this "
                  "vector stopped being marked canonical."),
        case(schema, "gps_fix", "with-unknown-extension",
             dict(nominal, seq=8, ext_count=1),
             "One extension record of an unknown type. A decoder MUST skip it by its "
             "length byte and still decode the base record.",
             extra=bytes([0x7F, 0x04, 0xDE, 0xAD, 0xBE, 0xEF])),
        {"name": "short-payload", "desc": "73 bytes. A truncated notification MUST be rejected, "
                                          "never decoded as a prefix.",
         "record": "gps_fix", "hex": encode(schema, "gps_fix", nominal)[:-1].hex(),
         "must_reject": "length"},
        {"name": "long-payload-no-ext", "desc": "75 bytes with ext_count 0. Trailing bytes that "
                                                "no ext_count accounts for MUST be rejected.",
         "record": "gps_fix", "hex": (encode(schema, "gps_fix", nominal) + b"\x00").hex(),
         "must_reject": "length"},
        case(schema, "gps_fix", "coordinate-extremes",
             dict(nominal, seq=30, lat=900_000_000, lon=-1_800_000_000,
                  head_mot=0),
             "The poles and the antimeridian: lat +90, lon -180, heading due "
             "north. All three are the last legal value on their side and MUST "
             "decode."),
        case(schema, "gps_fix", "heading-just-below-360",
             dict(nominal, seq=31, head_mot=35_999_999),
             "SPEC.md 5.4 -- heading is 0 to 360 exclusive of 360, so this is "
             "the largest legal value and 36000000 is not."),
        {"name": "latitude-beyond-the-pole",
         "desc": "A latitude of 91 degrees with the position bit set. MUST be "
                 "rejected, never clamped: 91 degrees is not a place a clamp "
                 "could move closer to, it is a corrupted field -- and every "
                 "other field in the record came from the same bytes. Clamping "
                 "to 90 puts the vehicle at the pole and lets the client draw "
                 "it there. SPEC.md 5.4.",
         "record": "gps_fix",
         "hex": encode(schema, "gps_fix",
                       dict(nominal, seq=32, lat=910_000_000)).hex(),
         "must_reject": "lat-out-of-range"},
        {"name": "longitude-beyond-the-antimeridian",
         "desc": "A longitude of 181 degrees. MUST be rejected. SPEC.md 5.4.",
         "record": "gps_fix",
         "hex": encode(schema, "gps_fix",
                       dict(nominal, seq=33, lon=1_810_000_000)).hex(),
         "must_reject": "lon-out-of-range"},
        {"name": "heading-at-360",
         "desc": "A heading of exactly 360 degrees. MUST be rejected: 360 and 0 "
                 "are the same bearing, and a range that admits both has two "
                 "encodings for one direction. SPEC.md 5.4.",
         "record": "gps_fix",
         "hex": encode(schema, "gps_fix",
                       dict(nominal, seq=34, head_mot=36_000_000)).hex(),
         "must_reject": "head-out-of-range"},
        case(schema, "gps_fix", "out-of-range-but-not-claimed",
             dict(nominal, seq=35, validity=V["t_utc"], lat=910_000_000),
             "A latitude of 91 degrees with the position bit CLEAR. The range "
             "rule of SPEC.md 5.4 applies only where a validity bit claims the "
             "field means something, so this MUST decode and MUST report "
             "position absent. A receiver that rejected it would be refusing a "
             "fix over a field it has already been told to ignore, and SPEC.md "
             "5.1 puts the duty to write zero on the device rather than the "
             "duty to police it on the receiver.",
             canonical=False,
             note="Not byte-canonical, so exempt from the round-trip: a "
                  "conforming encoder normalises the ungated latitude to zero, "
                  "which is also why this vector cannot be built through one."),
        {"name": "ext-count-exceeds-payload",
         "desc": "A well-formed 74-byte fix declaring one extension that is not "
                 "there. The record MUST be rejected: a decoder that trusts "
                 "ext_count without checking the buffer reads past the end. The "
                 "mirror of long-payload-no-ext -- the same disagreement about "
                 "where the record ends, in the opposite direction.",
         "record": "gps_fix",
         "hex": encode(schema, "gps_fix", dict(nominal, ext_count=1)).hex(),
         "must_reject": "ext-truncated"},
    ]
    files["gps-fix.json"] = gps

    # ---- CAN -------------------------------------------------------------
    def can_rec(dt, cid, payload, ext=False, fd=False, rtr=False):
        idf = cid | (1 << 29 if ext else 0) | (1 << 30 if fd else 0) | (1 << 31 if rtr else 0)
        return struct.pack("<HIB", dt, idf, len(payload)) + payload

    def can_batch(name, desc, hdr, recs, expect_recs, **kw):
        raw = encode(schema, "can_header", hdr) + b"".join(recs)
        c = {"name": name, "desc": desc, "record": "can_batch", "hex": raw.hex(),
             "expect": {"header": {f["name"]: hdr.get(f["name"], 0)
                                   for f in schema["records"]["can_header"]["fields"]},
                        "records": expect_recs}}
        c.update(kw)
        return c

    files["can-batch.json"] = [
        can_batch("single-classic-frame",
                  "One 8-byte classic frame.",
                  dict(seq=1, dropped=0, t_base=1_000_000, count=1, flags=0),
                  [can_rec(0, 0x1A0, bytes.fromhex("0011223344556677"))],
                  [{"dt": 0, "id": 0x1A0, "extended": False, "fd": False, "rtr": False,
                    "len": 8, "payload": "0011223344556677",
                    "t_device_us": 1_000_000}]),
        can_batch("three-frames-timestamped",
                  "Three frames at distinct bus-arrival times. dt is in 10 us ticks, so "
                  "t_device = t_base + dt*10.",
                  dict(seq=2, dropped=0, t_base=5_000_000, count=3, flags=0),
                  [can_rec(0, 0x100, bytes.fromhex("01")),
                   can_rec(23, 0x101, bytes.fromhex("0203")),
                   can_rec(65535, 0x102, bytes.fromhex("040506"))],
                  [{"dt": 0, "id": 0x100, "extended": False, "fd": False, "rtr": False,
                    "len": 1, "payload": "01", "t_device_us": 5_000_000},
                   {"dt": 23, "id": 0x101, "extended": False, "fd": False, "rtr": False,
                    "len": 2, "payload": "0203", "t_device_us": 5_000_230},
                   {"dt": 65535, "id": 0x102, "extended": False, "fd": False, "rtr": False,
                    "len": 3, "payload": "040506", "t_device_us": 5_655_350}],
                  note="dt 65535 is the end of the batch window. A device MUST flush before "
                       "this wraps, which is what bounds batch latency at 655.35 ms."),
        can_batch("extended-id",
                  "A 29-bit extended identifier. The extended flag lives in bit 29 of `id`, "
                  "and the arbitration id is bits 0-28 only.",
                  dict(seq=3, dropped=0, t_base=7_000_000, count=1, flags=0),
                  [can_rec(0, 0x18DAF110, bytes.fromhex("AABB"), ext=True)],
                  [{"dt": 0, "id": 0x18DAF110, "extended": True, "fd": False, "rtr": False,
                    "len": 2, "payload": "aabb", "t_device_us": 7_000_000}]),
        can_batch("can-fd-64-byte",
                  "A CAN FD frame carrying the maximum 64-byte payload.",
                  dict(seq=4, dropped=0, t_base=9_000_000, count=1, flags=0),
                  [can_rec(0, 0x2F0, bytes(range(64)), fd=True)],
                  [{"dt": 0, "id": 0x2F0, "extended": False, "fd": True, "rtr": False,
                    "len": 64, "payload": bytes(range(64)).hex(),
                    "t_device_us": 9_000_000}]),
        can_batch("t-base-near-wrap",
                  "t_base within 100 us of the u64 ceiling. Record 0 sits ON t_base "
                  "(SPEC.md 6.1), so the wrap is carried by record 1: t_base + 200 us "
                  "exceeds the ceiling. "
                  "SPEC.md 8 defines the arithmetic as modulo 2^64; a decoder using "
                  "arbitrary-precision integers MUST still report the wrapped value.",
                  dict(seq=11, dropped=0, t_base=(1 << 64) - 100, count=2, flags=0),
                  [can_rec(0, 0x1A0, bytes.fromhex("00")),
                   can_rec(20, 0x1A0, bytes.fromhex("01"))],
                  [{"dt": 0, "id": 0x1A0, "extended": False, "fd": False, "rtr": False,
                    "len": 1, "payload": "00",
                    "t_device_us": (1 << 64) - 100},
                   {"dt": 20, "id": 0x1A0, "extended": False, "fd": False, "rtr": False,
                    "len": 1, "payload": "01",
                    "t_device_us": ((1 << 64) - 100 + 200) & ((1 << 64) - 1)}],
                  note="Unreachable on real hardware -- a microsecond clock takes over "
                       "half a million years to get here -- but the two reference "
                       "decoders disagreed on it, which is a specification gap rather "
                       "than a hardware one."),
        can_batch("seq-wrap-and-saturated-loss",
                  "seq at 65535 with dropped at its ceiling. The next notification is "
                  "seq 0, which a receiver MUST NOT read as a gap, and dropped MUST be "
                  "read as 'at least 65535'.",
                  dict(seq=65535, dropped=65535, t_base=17_000_000, count=1, flags=0x01),
                  [can_rec(0, 0x1A0, bytes.fromhex("00"))],
                  [{"dt": 0, "id": 0x1A0, "extended": False, "fd": False, "rtr": False,
                    "len": 1, "payload": "00", "t_device_us": 17_000_000}]),
        can_batch("empty-batch",
                  "count 0. MUST be rejected: SPEC.md 6.2 defines t_base as the "
                  "bus-arrival time of record 0, so a batch with no record 0 "
                  "timestamps a frame that does not exist. A quiet bus is reported "
                  "by sending nothing, exactly as an idle IMU is (SPEC.md 7). This "
                  "vector used to assert the opposite, which made CAN and IMU "
                  "disagree about a field with one definition.",
                  dict(seq=5, dropped=0, t_base=11_000_000, count=0, flags=0), [], [],
                  must_reject="empty-batch"),
        can_batch("shedding-load",
                  "The device dropped 400 frames and is signalling overload in flags bit 0. "
                  "A decoder MUST surface both.",
                  dict(seq=6, dropped=400, t_base=13_000_000, count=1, flags=0x01),
                  [can_rec(0, 0x1A0, bytes.fromhex("00"))],
                  [{"dt": 0, "id": 0x1A0, "extended": False, "fd": False, "rtr": False,
                    "len": 1, "payload": "00", "t_device_us": 13_000_000}]),
        can_batch("remote-frame",
                  "A remote frame: RTR set, len 0, no payload. SPEC.md 6.5 -- the "
                  "length such a frame REQUESTS is not carried in major version 1, "
                  "only the fact that it occurred.",
                  dict(seq=12, dropped=0, t_base=19_000_000, count=1, flags=0),
                  [can_rec(0, 0x1A0, b"", rtr=True)],
                  [{"dt": 0, "id": 0x1A0, "extended": False, "fd": False, "rtr": True,
                    "len": 0, "payload": "", "t_device_us": 19_000_000}]),
        can_batch("standard-id-at-maximum",
                  "The largest legal standard identifier, 0x7FF. One more is a "
                  "different frame entirely and MUST be rejected.",
                  dict(seq=13, dropped=0, t_base=21_000_000, count=1, flags=0),
                  [can_rec(0, 0x7FF, bytes.fromhex("01"))],
                  [{"dt": 0, "id": 0x7FF, "extended": False, "fd": False, "rtr": False,
                    "len": 1, "payload": "01", "t_device_us": 21_000_000}]),
        {"name": "standard-id-too-large",
         "desc": "A standard frame carrying 0x800: an eleven-bit identifier that does "
                 "not fit in eleven bits. MUST be rejected, never truncated -- "
                 "truncation yields a different identifier that looks entirely valid.",
         "record": "can_batch",
         "hex": (encode(schema, "can_header",
                        dict(seq=14, dropped=0, t_base=1, count=1, flags=0))
                 + can_rec(0, 0x800, bytes.fromhex("01"))).hex(),
         "must_reject": "bad-standard-id"},
        {"name": "fd-and-rtr-together",
         "desc": "Both the CAN FD and RTR bits set. CAN FD has no remote frames, so "
                 "this describes a frame that cannot exist and MUST be rejected.",
         "record": "can_batch",
         "hex": (encode(schema, "can_header",
                        dict(seq=15, dropped=0, t_base=1, count=1, flags=0))
                 + can_rec(0, 0x1A0, b"", fd=True, rtr=True)).hex(),
         "must_reject": "fd-rtr"},
        {"name": "remote-frame-with-payload",
         "desc": "RTR set with a one-byte payload. A remote frame carries no data, so "
                 "len MUST be zero and this MUST be rejected.",
         "record": "can_batch",
         "hex": (encode(schema, "can_header",
                        dict(seq=16, dropped=0, t_base=1, count=1, flags=0))
                 + can_rec(0, 0x1A0, bytes.fromhex("FF"), rtr=True)).hex(),
         "must_reject": "rtr-with-payload"},
        {"name": "len-above-maximum",
         "desc": "A record declaring a 100-byte payload on a Classic frame. No bus "
                 "carries it -- Classic stops at 8 and the CAN FD ladder at 64 -- so "
                 "the record is malformed and the batch MUST be rejected, not clamped "
                 "and not read as 100 bytes.",
         "record": "can_batch",
         "hex": (encode(schema, "can_header",
                        dict(seq=10, dropped=0, t_base=1, count=1, flags=0))
                 + struct.pack("<HIB", 0, 0x1A0, 100) + bytes(100)).hex(),
         "must_reject": "classic-length",
         "note": "The corpus cannot reach this rule by construction — every legal "
                 "vector has len <= 64, so removing the bound check changed nothing "
                 "and all 43 vectors still passed. Found by source mutation, which is "
                 "why tools/mutate.py earns its place alongside "
                 "tools/check_corpus.py."},
        can_batch("can-fd-twelve-byte",
                  "A CAN FD frame at the first rung above eight. SPEC.md 6.10 -- "
                  "twelve is representable, eleven is not, and a decoder that "
                  "accepts any length up to 64 cannot tell them apart.",
                  dict(seq=17, dropped=0, t_base=9_000_000, count=1, flags=0),
                  [can_rec(0, 0x2F0, bytes(range(12)), fd=True)],
                  [{"dt": 0, "id": 0x2F0, "extended": False, "fd": True, "rtr": False,
                    "len": 12, "payload": bytes(range(12)).hex(),
                    "t_device_us": 9_000_000}]),
        {"name": "first-record-dt-nonzero",
         "desc": "A batch whose first record claims a 50 us offset from t_base. "
                 "MUST be rejected: SPEC.md 6.1 defines t_base AS record 0's "
                 "arrival time, so a non-zero first dt means the sender and the "
                 "receiver disagree about what t_base is, and the receiver "
                 "cannot tell which reading to trust. Four vectors in this "
                 "corpus carried non-zero first offsets while the specification "
                 "said they could not, and both decoders accepted them.",
         "record": "can_batch",
         "hex": (encode(schema, "can_header",
                        dict(seq=23, dropped=0, t_base=1_000_000, count=1, flags=0))
                 + can_rec(5, 0x1A0, bytes.fromhex("00"))).hex(),
         "must_reject": "first-dt-nonzero"},
        {"name": "classic-length-nine",
         "desc": "A Classic frame declaring nine payload bytes. A Classic frame "
                 "carries 0..8, so this length is impossible rather than merely "
                 "large, and the batch MUST be rejected. SPEC.md 6.10.",
         "record": "can_batch",
         "hex": (encode(schema, "can_header",
                        dict(seq=18, dropped=0, t_base=1, count=1, flags=0))
                 + can_rec(0, 0x1A0, bytes(9))).hex(),
         "must_reject": "classic-length"},
        {"name": "fd-length-nine",
         "desc": "A CAN FD frame declaring nine payload bytes. Above eight the FD "
                 "DLC ladder jumps to twelve, so nine is a length no controller can "
                 "produce and the batch MUST be rejected. SPEC.md 6.10.",
         "record": "can_batch",
         "hex": (encode(schema, "can_header",
                        dict(seq=19, dropped=0, t_base=1, count=1, flags=0))
                 + can_rec(0, 0x1A0, bytes(9), fd=True)).hex(),
         "must_reject": "fd-length"},
        {"name": "trailing-bytes-after-batch",
         "desc": "A complete, well-formed batch followed by two bytes the header "
                 "does not account for. The notification MUST be rejected: the "
                 "surplus means the reader and the writer disagree about the batch, "
                 "so the records already read cannot be trusted either. Distinct "
                 "from count-exceeds-payload, which is the same disagreement in the "
                 "opposite direction -- and until this vector existed, relaxing the "
                 "trailing-byte check to `off > len` passed all 79 vectors.",
         "record": "can_batch",
         "hex": (encode(schema, "can_header",
                        dict(seq=20, dropped=0, t_base=1, count=1, flags=0))
                 + can_rec(0, 0x1A0, bytes.fromhex("0011")) + b"\xAA\xBB").hex(),
         "must_reject": "length"},
        {"name": "short-payload",
         "desc": "15 bytes: shorter than the batch header itself. MUST be rejected "
                 "rather than read past the end.",
         "record": "can_batch",
         "hex": encode(schema, "can_header",
                       dict(seq=9, dropped=0, t_base=1, count=0, flags=0))[:-1].hex(),
         "must_reject": "length"},
        {"name": "count-exceeds-payload",
         "desc": "Header claims 4 records but only one is present. MUST be rejected.",
         "record": "can_batch",
         "hex": (encode(schema, "can_header",
                        dict(seq=7, dropped=0, t_base=1, count=4, flags=0))
                 + can_rec(0, 0x1A0, b"\x00")).hex(),
         "must_reject": "truncated-record"},
        {"name": "reserved-nonzero",
         "desc": "Header reserved bytes carry a value assigned by a future minor. "
                 "A decoder MUST ignore them, not reject -- and an encoder MUST "
                 "normalise them to zero, because SPEC.md 2 requires reserved "
                 "fields to be zero ON TRANSMIT. The two halves of that rule "
                 "pull in opposite directions, which is why this vector is "
                 "deliberately non-canonical: it is the only one that asserts "
                 "both at once.",
         "record": "can_batch",
         # One record, because SPEC.md 6.2 forbids an empty batch: t_base names
         # record 0. This vector is about the reserved BYTES and carries the
         # minimum that lets it be about only those.
         "hex": (encode(schema, "can_header",
                        dict(seq=8, dropped=0, t_base=1, count=1, flags=0, reserved=0xBEEF))
                 + can_rec(0, 0x1A0, bytes.fromhex("00"))).hex(),
         "expect": {"header": {"seq": 8, "dropped": 0, "t_base": 1, "count": 1,
                               "flags": 0, "reserved": 0xBEEF},
                    "records": [{"dt": 0, "id": 0x1A0, "extended": False,
                                 "fd": False, "rtr": False, "len": 1,
                                 "payload": "00", "t_device_us": 1}]},
         "canonical": False,
         "expect_roundtrip_hex": (encode(schema, "can_header",
                        dict(seq=8, dropped=0, t_base=1, count=1, flags=0, reserved=0))
                 + can_rec(0, 0x1A0, bytes.fromhex("00"))).hex()},
    ]

    # ---- IMU -------------------------------------------------------------
    def imu_batch(name, desc, hdr, samples, *, canonical=True, **kw):
        # SPEC.md 7: a sample group whose presence flag is clear MUST be zero on
        # the wire and MUST be reported absent. Derived from the schema's
        # `presence` declaration rather than restated here, so this stays true
        # if a group is added.
        rec = schema["records"]["imu_sample"]
        pres = rec["presence"]
        flags = hdr.get("flags", 0)
        absent = sorted(f["name"] for f in rec["fields"]
                        if f.get("presence_bit") is not None
                        and not (flags & (1 << pres["bits"][f["presence_bit"]])))

        def gate(s):
            return {f["name"]: (0 if f["name"] in absent else s.get(f["name"], 0))
                    for f in rec["fields"]}

        gated = [gate(s) for s in samples]
        # A canonical vector carries the gated bytes; a non-canonical one keeps
        # the stale values, and its round-trip asserts the encoder normalises.
        wire = gated if canonical else [dict(s) for s in samples]

        raw = encode(schema, "imu_header", hdr) + b"".join(
            encode(schema, "imu_sample", s) for s in wire)
        exp = []
        for i, s in enumerate(wire):
            e = {f["name"]: s.get(f["name"], 0) for f in rec["fields"]}
            e["t_device_us"] = (hdr["t_base"] + i * hdr["period"]) & ((1 << 64) - 1)
            e["absent"] = absent
            exp.append(e)
        c = {"name": name, "desc": desc, "record": "imu_batch", "hex": raw.hex(),
             "expect": {"header": dict(
                            {f["name"]: hdr.get(f["name"], 0)
                             for f in schema["records"]["imu_header"]["fields"]},
                            # SPEC.md 7.2 -- reported explicitly so the corpus
                            # checks both references agree, rather than each
                            # deriving it privately from flags.
                            saturated=bool(hdr.get("flags", 0) & 0x04)),
                        "samples": exp}}
        if not canonical:
            c["canonical"] = False
            c["expect_roundtrip_hex"] = (
                encode(schema, "imu_header", hdr) + b"".join(
                    encode(schema, "imu_sample", s) for s in gated)).hex()
        c.update(kw)
        return c

    files["imu-batch.json"] = [
        imu_batch("stationary-1g-down",
                  "A level, stationary device: 1 g on Z, no rotation.",
                  dict(seq=1, dropped=0, t_base=2_000_000, period=5000, count=2, flags=0b011),
                  [dict(ax=0, ay=0, az=1000, gx=0, gy=0, gz=0),
                   dict(ax=2, ay=-1, az=999, gx=1, gy=0, gz=-1)],
                  note="period 5000 us is 200 Hz. Samples are evenly spaced, so only the "
                       "batch carries a timestamp."),
        imu_batch("cornering-and-yaw",
                  "Lateral acceleration with yaw rate. gx/gy/gz scale by 0.05 deg/s, so "
                  "gz 1200 is 60 deg/s.",
                  dict(seq=2, dropped=0, t_base=4_000_000, period=1200, count=2, flags=0b011),
                  [dict(ax=-150, ay=980, az=1010, gx=-20, gy=15, gz=1200),
                   dict(ax=-180, ay=1020, az=1005, gx=-25, gy=18, gz=1310)],
                  note="period 1200 us is 833 Hz — an ODR that is not an integer number of "
                       "hertz, which is why period is microseconds and not a rate."),
        imu_batch("negative-full-scale",
                  "Extremes on every axis; catches signed 16-bit handling.",
                  dict(seq=3, dropped=0, t_base=6_000_000, period=5000, count=1, flags=0b011),
                  [dict(ax=-32768, ay=32767, az=-32768, gx=32767, gy=-32768, gz=32767)]),
        imu_batch("low-rate-10hz",
                  "A 10 Hz IMU: period 100000 us. This rate was unrepresentable while "
                  "period was a u16, whose ceiling of 65535 us put a floor of 15.26 Hz "
                  "on every conforming device.",
                  dict(seq=8, dropped=0, t_base=20_000_000, period=100_000, count=2,
                       flags=0b011),
                  [dict(ax=0, ay=0, az=1000, gx=0, gy=0, gz=0),
                   dict(ax=5, ay=-5, az=1002, gx=1, gy=-1, gz=2)]),
        imu_batch("stale-values-behind-cleared-flags",
                  "flags clears both accel and gyro, but every sample byte still "
                  "carries the previous reading. A decoder MUST report all six "
                  "fields absent on the strength of the flags alone, and MUST NOT "
                  "read 1 g and 60 deg/s out of them.",
                  dict(seq=6, dropped=0, t_base=12_000_000, period=5000, count=1, flags=0),
                  [dict(ax=-150, ay=980, az=1010, gx=-20, gy=15, gz=1200)],
                  canonical=False,
                  note="The only vector that exercises the IMU presence gates. Every "
                       "other case either sets the flag or has zeroes behind the "
                       "cleared one, so removing the gate changed nothing and the "
                       "whole corpus still passed. Found by tools/check_corpus.py."),
        {"name": "short-payload",
         "desc": "15 bytes: shorter than the batch header itself. MUST be rejected "
                 "rather than read past the end.",
         "record": "imu_batch",
         "hex": encode(schema, "imu_header",
                       dict(seq=7, dropped=0, t_base=1, period=5000,
                            count=0, flags=0b011))[:-1].hex(),
         "must_reject": "length"},
        imu_batch("saturated-batch",
                  "A batch flagged saturated (flags bit 2) with a sample at the "
                  "i16 rail. SPEC.md 7.2 -- the reading is 'at least this much', "
                  "not 'this much', so a client MUST treat it as a lower bound "
                  "and MUST NOT integrate it. Note the presence bits stay SET: "
                  "the sensor is fitted and did report, which is a different "
                  "state from absent.",
                  dict(seq=21, dropped=0, t_base=5_000_000, period=1_200,
                       count=1, flags=0b111),
                  [dict(ax=32767, ay=-32768, az=1000,
                        gx=32767, gy=0, gz=-32768)]),
        {"name": "period-zero",
         "desc": "A period of zero says every sample in the batch was taken at "
                 "the same instant, which describes no measurement -- and a "
                 "client recovering a rate from it divides by zero. SPEC.md 7 "
                 "forbids emitting it; this vector makes a decoder reject it, "
                 "which neither reference did.",
         "record": "imu_batch",
         "hex": (encode(schema, "imu_header",
                        dict(seq=22, dropped=0, t_base=1, period=0, count=1,
                             flags=0b011))
                 + encode(schema, "imu_sample",
                          dict(ax=1, ay=2, az=3, gx=4, gy=5, gz=6))).hex(),
         "must_reject": "period-zero"},
        {"name": "empty-batch",
         "desc": "A header with no samples. SPEC.md 7 -- t_base IS the "
                 "acquisition time of sample 0, so a batch with no sample 0 "
                 "carries a timestamp naming a sample that does not exist. A "
                 "device with nothing to report sends nothing. This is where "
                 "IMU differs from CAN, whose count MAY be zero because a CAN "
                 "t_base describes an observed bus rather than a sample.",
         "record": "imu_batch",
         "hex": encode(schema, "imu_header",
                       dict(seq=23, dropped=0, t_base=7_000_000, period=1_200,
                            count=0, flags=0b011)).hex(),
         "must_reject": "empty-batch"},
        {"name": "count-exceeds-payload",
         "desc": "Header declares four samples, one is present. MUST be rejected: "
                 "a decoder that trusts count without checking the buffer reads "
                 "three samples of whatever follows the notification.",
         "record": "imu_batch",
         "hex": (encode(schema, "imu_header",
                        dict(seq=20, dropped=0, t_base=1, period=5000, count=4,
                             flags=0b011))
                 + encode(schema, "imu_sample",
                          dict(ax=1, ay=2, az=3, gx=4, gy=5, gz=6))).hex(),
         "must_reject": "length"},
        {"name": "long-payload",
         "desc": "One sample declared, one sample present, plus a trailing byte. The "
                 "length MUST equal the header plus count samples exactly.",
         "record": "imu_batch",
         "hex": (encode(schema, "imu_header",
                        dict(seq=5, dropped=0, t_base=1, period=5000, count=1, flags=0b011))
                 + encode(schema, "imu_sample", dict(ax=1, ay=2, az=3, gx=4, gy=5, gz=6))
                 + b"\x00").hex(),
         "must_reject": "length"},
        imu_batch("seq-wrap",
                  "seq at 65535 on the IMU stream. SPEC.md 8.2: seq counts "
                  "notifications on its own characteristic and wraps, so the next one "
                  "is 0 and that is not loss.",
                  dict(seq=65535, dropped=3, t_base=14_000_000, period=5000, count=1,
                       flags=0b011),
                  [dict(ax=1, ay=2, az=1000, gx=3, gy=4, gz=5)]),
        imu_batch("accel-only",
                  "flags bit1 clear: the device has no gyro. Gyro fields are zero and MUST "
                  "be reported absent, not as zero rotation.",
                  dict(seq=4, dropped=0, t_base=8_000_000, period=10000, count=1, flags=0b001),
                  [dict(ax=10, ay=20, az=1000, gx=0, gy=0, gz=0)]),
    ]

    # ---- Info ------------------------------------------------------------
    C = {b["name"]: 1 << b["bit"] for b in schema["bitmasks"]["capabilities"]["bits"]}
    files["info.json"] = [
        case(schema, "info", "gps-and-can-device",
             dict(protocol_major=1, protocol_minor=0,
                  capabilities=C["gps"] | C["can"] | C["control"] | C["on_change_subscriptions"],
                  gps_rate_hz=25, gps_max_rate_hz=25, can_subscription_slots=64,
                  can_max_frames_per_s=4000, imu_rate_hz=0, imu_max_rate_hz=0,
                  clock_flags=0b01, max_notify_bytes=244),
             "A typical dual-role module."),
        case(schema, "info", "gps-only-no-control",
             dict(protocol_major=1, protocol_minor=0, capabilities=C["gps"],
                  gps_rate_hz=10, gps_max_rate_hz=10,
                  max_notify_bytes=185),
             "A GPS-only board with no control channel. Every CAN capacity figure is zero "
             "and the largest CAN payload follows from the capability bits "
             "(SPEC.md 4.2) rather than from a field -- and a "
             "client MUST NOT infer a default."),
        case(schema, "info", "future-minor-unknown-capability",
             dict(protocol_major=1, protocol_minor=7,
                  capabilities=(C["gps"] | C["can"] | C["imu"] | C["can_fd"]
                                | C["control"] | (1 << 19)),
                  gps_rate_hz=25, gps_max_rate_hz=25, can_subscription_slots=32,
                  can_max_frames_per_s=4000, imu_rate_hz=833, imu_max_rate_hz=833,
                  clock_flags=0b11, max_notify_bytes=498),
             "Minor 7 with a capability bit this client has never heard of. A client MUST "
             "ignore the unknown bit and use everything it does understand, and a "
             "VTP/1.0 encoder MUST NOT reproduce it (SPEC.md 2).",
             canonical=False,
             note="This vector used to declare CAN with no Control bit, which SPEC.md "
                  "4.1 now forbids and which conformance/run.py had been quietly "
                  "assuming the opposite of. `can_fd` requires `can` and `can` requires "
                  "`control`, so the whole chain is present here."),
        case(schema, "info", "rate-below-maximum",
             dict(protocol_major=1, protocol_minor=0,
                  capabilities=C["gps"] | C["imu"] | C["control"],
                  gps_rate_hz=10, gps_max_rate_hz=25,
                  imu_rate_hz=100, imu_max_rate_hz=833, max_notify_bytes=244),
             "A device running below its ceiling: 10 Hz of a possible 25, 100 Hz of a "
             "possible 833. Current rate and maximum rate are separate fields and a "
             "client MUST NOT read one for the other.",
             note="The only vector where the current and maximum rates differ. Without "
                  "it a decoder can read gps_rate_hz from gps_max_rate_hz's offset and "
                  "pass the whole corpus -- found by tools/mutate.py, not by review."),
        # SPEC.md 4.1 -- the capability matrix, in the direction that catches a
        # device rather than a client. Each of these decoded happily before the
        # matrix existed.
        case(schema, "info", "can-without-control",
             dict(protocol_major=1, protocol_minor=0,
                  capabilities=C["gps"] | C["can"],
                  gps_rate_hz=10, gps_max_rate_hz=10, can_subscription_slots=32,
                  can_max_frames_per_s=2000,
                  max_notify_bytes=244),
             "SPEC.md 4.1 -- `can` requires `control`. A CAN device with no Control "
             "characteristic forwards nothing, because CAN_SUBSCRIBE is the only way "
             "to ask it to, so this advertises a role no client can use.",
             reject="capabilities"),
        case(schema, "info", "monitor-without-control",
             dict(protocol_major=1, protocol_minor=0,
                  capabilities=C["gps"] | C["monitor"],
                  gps_rate_hz=10, gps_max_rate_hz=10, max_notify_bytes=244),
             "SPEC.md 4.1 -- `monitor` requires `control`. MONITOR_LIST is the only "
             "way a device can say which channels it wants.",
             reject="capabilities"),
        case(schema, "info", "can-fd-without-can",
             dict(protocol_major=1, protocol_minor=0,
                  capabilities=C["can_fd"] | C["control"],
                  max_notify_bytes=244),
             "SPEC.md 4.1 -- `can_fd` qualifies how CAN frames are carried, and "
             "qualifies nothing on a device with no CAN.",
             reject="capabilities"),
        case(schema, "info", "capacity-without-capability",
             dict(protocol_major=1, protocol_minor=0, capabilities=C["gps"],
                  gps_rate_hz=10, gps_max_rate_hz=10,
                  can_subscription_slots=32, can_max_frames_per_s=4000,
                  max_notify_bytes=185),
             "SPEC.md 4.1 -- every CAN capacity MUST be zero while the `can` bit is "
             "clear. A client sizing a buffer from can_max_frames_per_s here has been "
             "told something false about a role the device does not have.",
             reject="capabilities"),
        {"name": "reserved-byte-nonzero",
         "desc": "Byte 20 carries a value assigned by a future minor. It held "
                 "can_max_payload until SPEC.md 4.2 derived the CAN payload "
                 "ceiling from the capability bits instead, so it is reserved "
                 "now -- and both halves of SPEC.md 2 apply: a decoder MUST "
                 "ignore it, and an encoder MUST normalise it to zero.",
         "record": "info",
         "hex": encode(schema, "info",
                       dict(protocol_major=1, protocol_minor=0,
                            capabilities=C["gps"], gps_rate_hz=10,
                            gps_max_rate_hz=10, reserved_20=0x40,
                            max_notify_bytes=244)).hex(),
         "expect": {f["name"]: dict(protocol_major=1, protocol_minor=0,
                                    capabilities=C["gps"], gps_rate_hz=10,
                                    gps_max_rate_hz=10, reserved_20=0x40,
                                    max_notify_bytes=244).get(f["name"], 0)
                    for f in schema["records"]["info"]["fields"]},
         "canonical": False,
         "expect_roundtrip_hex": encode(schema, "info",
                       dict(protocol_major=1, protocol_minor=0,
                            capabilities=C["gps"], gps_rate_hz=10,
                            gps_max_rate_hz=10, max_notify_bytes=244)).hex()},
        {"name": "short-payload",
         "desc": "23 bytes. Info is fixed-size; a truncated read MUST be rejected.",
         "record": "info",
         "hex": encode(schema, "info", dict(protocol_major=1))[:-1].hex(),
         "must_reject": "length"},
        {"name": "long-payload",
         "desc": "25 bytes. Info has no extension mechanism, so trailing bytes MUST be "
                 "rejected rather than ignored.",
         "record": "info",
         "hex": (encode(schema, "info", dict(protocol_major=1)) + b"\x00").hex(),
         "must_reject": "length"},
    ]

    # ---- CAN subscription table -----------------------------------------
    EXACT = 0x3FFFFFFF  # SPEC.md 9.2

    def sub(handle, cid, mask, mode, arg):
        return dict(handle=handle, id=cid, mask=mask, mode=mode, arg=arg)

    def can_list(name, desc, page, entries, **kw):
        raw = encode(schema, "can_list_page", page) + b"".join(
            encode(schema, "can_subscription", e) for e in entries)
        exp_entries = []
        for e in entries:
            row = {f["name"]: e.get(f["name"], 0)
                   for f in schema["records"]["can_subscription"]["fields"]}
            known = {m["value"] for m in SCHEMA_ENUMS["sub_mode"]}
            row["mode_known"] = row["mode"] in known
            exp_entries.append(row)
        c = {"name": name, "desc": desc, "record": "can_list", "hex": raw.hex(),
             "expect": {"page": {f["name"]: page.get(f["name"], 0)
                                 for f in schema["records"]["can_list_page"]["fields"]},
                        "entries": exp_entries}}
        c.update(kw)
        return c

    files["can-list.json"] = [
        can_list("empty-table",
                 "No subscriptions installed. total 0, count 0 -- a legal answer, "
                 "and the state a device MUST be in after CAN_RESET or a reconnect.",
                 dict(total=0, index=0, count=0), []),
        can_list("one-exact-id",
                 "A single exact-id subscription. CAN_SUBSCRIBE is CAN_SUBSCRIBE_MASK "
                 "with mask 0x3FFFFFFF, so that is what the table reports.",
                 dict(total=1, index=0, count=1),
                 [sub(1, 0x0C0, EXACT, 0, 0)]),
        can_list("mask-and-exact-overlapping",
                 "A mask covering 0x100-0x10F alongside an exact subscription for "
                 "0x105. Both match frame 0x105; SPEC.md 9.3 says the more specific "
                 "one governs, and both terms are visible here so a client can work "
                 "out which.",
                 dict(total=2, index=0, count=2),
                 [sub(7, 0x100, 0x1FFFFFF0, 1, 100),
                  sub(9, 0x105, EXACT, 0, 0)]),
        can_list("first-page-of-many",
                 "Six entries of fourteen: the most that fit beside a page header in "
                 "a 97-byte response at the minimum ATT MTU. The client repeats from "
                 "index + count.",
                 dict(total=14, index=0, count=6),
                 [sub(h, 0x200 + h, EXACT, 3, 4) for h in range(1, 7)]),
        can_list("later-page",
                 "The continuation: index 6 of the same fourteen. `total` is the whole "
                 "table, not the page.",
                 dict(total=14, index=6, count=6),
                 [sub(h, 0x200 + h, EXACT, 3, 4) for h in range(7, 13)]),
        can_list("start-beyond-end",
                 "A client asked for index 99 of a 2-entry table. Not an error: ok, "
                 "count 0, and the true total so the client can tell it overshot.",
                 dict(total=2, index=99, count=0), []),
        can_list("unknown-mode-in-table",
                 "The device reports a subscription mode from a later minor. A client "
                 "MUST report it unknown and MUST NOT read it as every_frame.",
                 dict(total=1, index=0, count=1),
                 [sub(3, 0x1A0, EXACT, 200, 0)]),
        {"name": "short-payload",
         "desc": "5 bytes: shorter than the page header. MUST be rejected.",
         "record": "can_list",
         "hex": encode(schema, "can_list_page", dict(total=0, index=0, count=0))[:-1].hex(),
         "must_reject": "length"},
        {"name": "long-payload",
         "desc": "One entry declared, one present, plus a trailing byte. The length "
                 "MUST equal the header plus count entries exactly.",
         "record": "can_list",
         "hex": (encode(schema, "can_list_page", dict(total=1, index=0, count=1))
                 + encode(schema, "can_subscription", sub(1, 0x0C0, EXACT, 0, 0))
                 + b"\x00").hex(),
         "must_reject": "length"},
        {"name": "count-exceeds-payload",
         "desc": "Header claims three entries, one is present. MUST be rejected.",
         "record": "can_list",
         "hex": (encode(schema, "can_list_page", dict(total=3, index=0, count=3))
                 + encode(schema, "can_subscription", sub(1, 0x0C0, EXACT, 0, 0))).hex(),
         "must_reject": "truncated-record"},
    ]

    # ---- Monitor ---------------------------------------------------------
    def monitor_list(name, desc, page, entries, **kw):
        raw = encode(schema, "monitor_declaration", page) + b"".join(
            encode(schema, "monitor_channel", e) for e in entries)
        known = {m["value"] for m in SCHEMA_ENUMS["channel"]}
        exp = []
        for e in entries:
            row = {f["name"]: e.get(f["name"], 0)
                   for f in schema["records"]["monitor_channel"]["fields"]}
            row["channel_known"] = row["channel"] in known
            exp.append(row)
        c = {"name": name, "desc": desc, "record": "monitor_list", "hex": raw.hex(),
             "expect": {"declaration": {f["name"]: page.get(f["name"], 0)
                                        for f in schema["records"]["monitor_declaration"]["fields"]},
                        "entries": exp}}
        c.update(kw)
        return c

    def monitor_update(name, desc, hdr, values, *, canonical=True, **kw):
        rec = schema["records"]["monitor_value"]
        bit = {b["name"]: b["bit"]
               for b in schema["bitmasks"]["monitor_validity"]["bits"]}

        def gate(v):
            present = v.get("validity", 0) & (1 << bit["present"])
            return {**v, "value": v.get("value", 0) if present else 0}

        gated = [gate(v) for v in values]
        wire = gated if canonical else [dict(v) for v in values]
        raw = encode(schema, "monitor_header", hdr) + b"".join(
            encode(schema, "monitor_value", v) for v in wire)
        exp = []
        for v in wire:
            row = {f["name"]: v.get(f["name"], 0) for f in rec["fields"]}
            row["absent"] = ([] if v.get("validity", 0) & (1 << bit["present"])
                             else ["value"])
            exp.append(row)
        c = {"name": name, "desc": desc, "record": "monitor_update", "hex": raw.hex(),
             "expect": {"header": {f["name"]: hdr.get(f["name"], 0)
                                   for f in schema["records"]["monitor_header"]["fields"]},
                        "values": exp}}
        if not canonical:
            c["canonical"] = False
            c["expect_roundtrip_hex"] = (
                encode(schema, "monitor_header", hdr) + b"".join(
                    encode(schema, "monitor_value", v) for v in gated)).hex()
        c.update(kw)
        return c

    CH = {m["name"]: m["value"] for m in SCHEMA_ENUMS["channel"]}
    PRESENT = 1

    files["monitor.json"] = [
        monitor_list("dash-asks-for-four",
                     "A display device asking for the four values a lap timer shows. "
                     "It names channels; it does not send an expression to evaluate.",
                     dict(count=4),
                     # Every channel carries a deadline (SPEC.md 13.5). This
                     # vector used to leave max_age defaulted to 0 on all four
                     # -- the canonical declaration violating the section that
                     # governs it, in a corpus that checked everything else.
                     [dict(slot=0, channel=CH["lap_time"], max_age=20),
                      dict(slot=1, channel=CH["last_lap_time"], max_age=255),
                      dict(slot=2, channel=CH["delta_best"], max_age=20),
                      dict(slot=3, channel=CH["lap_number"], max_age=255)]),
        monitor_list("no-channels-requested",
                     "A device that implements the role but currently wants nothing. "
                     "Legal, and the state before it has configured itself.",
                     dict(count=0), []),
        monitor_list("unknown-channel-requested",
                     "A device asking for a channel from a later minor. A client MUST "
                     "report it unknown, MUST NOT substitute another, and MUST answer "
                     "the slot as absent rather than omitting it.",
                     dict(count=1),
                     [dict(slot=9, channel=4242, max_age=20)]),
        monitor_update("first-lap-nothing-to-report",
                       "Mid first lap: elapsed time is known, but there is no last lap "
                       "and no delta yet. Those slots are present-bit clear and zero -- "
                       "a device that renders 0.000 for a last lap that has not "
                       "happened has been told something false.",
                       dict(seq=1, count=3),
                       [dict(slot=0, validity=PRESENT, value=42_318),
                        dict(slot=1, validity=0),
                        dict(slot=2, validity=0)]),
        monitor_update("second-lap-all-known",
                       "A lap later: every slot carries a measurement, and delta_best "
                       "is negative because this lap is ahead.",
                       dict(seq=2, count=3),
                       [dict(slot=0, validity=PRESENT, value=12_004),
                        dict(slot=1, validity=PRESENT, value=87_340),
                        dict(slot=2, validity=PRESENT, value=-1_250)]),
        monitor_update("stale-value-behind-cleared-bit",
                       "A client that clears the present bit but leaves the previous "
                       "value in the bytes. A device MUST report the slot absent on the "
                       "strength of the bit alone, and a conforming encoder normalises "
                       "these bytes to zero.",
                       dict(seq=3, count=1),
                       [dict(slot=1, validity=0, value=87_340)],
                       canonical=False),
        monitor_update("empty-update",
                       "count 0. MUST be rejected: SPEC.md 13.4 makes every "
                       "write a COMPLETE statement of what the client can "
                       "supply, and a write naming no slots is the one thing a "
                       "complete statement cannot be. A client with nothing to "
                       "supply writes every slot with the present bit clear; a "
                       "client with nothing to say does not write at all. This "
                       "vector said the opposite of the section governing it, "
                       "and the reference device rejected it.",
                       dict(seq=4, count=0), [],
                       must_reject="empty-update"),
        {"name": "short-payload",
         "desc": "3 bytes: shorter than the update header. MUST be rejected.",
         "record": "monitor_update",
         "hex": encode(schema, "monitor_header", dict(seq=5, count=0))[:-1].hex(),
         "must_reject": "length"},
        {"name": "long-payload",
         "desc": "One value declared, one present, plus a trailing byte. MUST be "
                 "rejected rather than ignored.",
         "record": "monitor_update",
         "hex": (encode(schema, "monitor_header", dict(seq=6, count=1))
                 + encode(schema, "monitor_value",
                          dict(slot=0, validity=PRESENT, value=1))
                 + b"\x00").hex(),
         "must_reject": "length"},
        {"name": "count-exceeds-payload",
         "desc": "Header claims two values, one is present. MUST be rejected.",
         "record": "monitor_update",
         "hex": (encode(schema, "monitor_header", dict(seq=7, count=2))
                 + encode(schema, "monitor_value",
                          dict(slot=0, validity=PRESENT, value=1))).hex(),
         "must_reject": "truncated-record"},
        {"name": "short-declaration",
         "desc": "1 byte: shorter than the declaration header. MUST be rejected.",
         "record": "monitor_list",
         "hex": encode(schema, "monitor_declaration", dict(count=0))[:-1].hex(),
         "must_reject": "length"},
        {"name": "count-exceeds-declaration",
         "desc": "The declaration claims three entries, one is present. MUST be rejected: "
                 "a decoder that trusts count without checking the buffer reads "
                 "two channel assignments out of adjacent memory.",
         "record": "monitor_list",
         "hex": (encode(schema, "monitor_declaration", dict(count=3))
                 + encode(schema, "monitor_channel",
                            dict(slot=0, channel=1, max_age=20))).hex(),
         "must_reject": "length"},
        {"name": "long-declaration",
         "desc": "One entry declared, one present, plus a trailing byte. MUST be "
                 "rejected.",
         "record": "monitor_list",
         "hex": (encode(schema, "monitor_declaration", dict(count=1))
                 + encode(schema, "monitor_channel",
                            dict(slot=0, channel=1, max_age=20))
                 + b"\x00").hex(),
         "must_reject": "length"},
        monitor_list("per-channel-expiry",
                     "Three channels with three different deadlines. SPEC.md "
                     "13.5 -- a lap time ticking up is wrong within a second "
                     "of going stale while a best lap stays true until it is "
                     "beaten, so the deadline is per channel. Every channel "
                     "has one.",
                     dict(count=3),
                     [dict(slot=0, channel=1, max_age=20),    # lap_time, 2 s
                      dict(slot=1, channel=3, max_age=255),   # best_lap, 25.5 s
                      dict(slot=2, channel=7, max_age=5)]),   # speed, 500 ms
        monitor_list("max-age-at-ceiling",
                     "max_age 255 is 25.5 seconds, the longest deadline this "
                     "field can express. A channel that changes rarely takes "
                     "this rather than none: SPEC.md 13.5 has no `never`.",
                     dict(count=1),
                     [dict(slot=0, channel=3, max_age=255)]),
        monitor_list("zero-max-age",
                     "A channel declaring max_age 0. MUST be rejected: SPEC.md "
                     "13.5 gives every declared channel a deadline, so a value "
                     "the client stops sending always stops being shown. Zero "
                     "used to mean `no deadline of its own`, reconciled by a "
                     "derived device-wide liveness bound -- two rules, and a "
                     "canonical vector that satisfied neither.",
                     dict(count=2),
                     [dict(slot=0, channel=1, max_age=20),
                      dict(slot=1, channel=3, max_age=0)],
                     must_reject="zero-max-age"),
        monitor_list("duplicate-slot-in-declaration",
                     "Two entries claiming slot 0. MUST be rejected: the slot is "
                     "how a value is addressed, so every later update would be "
                     "ambiguous. SPEC.md 13.3.",
                     dict(count=2),
                     [dict(slot=0, channel=1, max_age=10),
                      dict(slot=0, channel=7, max_age=10)],
                     must_reject="duplicate-slot"),
        monitor_list("more-channels-than-fit",
                     "A declaration of 16 channels. SPEC.md 13.4 requires every "
                     "write to carry every slot, and 16 values do not fit beside "
                     "a header in one write at the minimum ATT MTU -- so the "
                     "device has made its own rule unsatisfiable and this MUST "
                     "be rejected. All 16 are really here: `total` used to carry "
                     "the count for a declaration that arrived a page at a time, "
                     "and with paging gone the only number is the one in front "
                     "of the entries.",
                     dict(count=16),
                     [dict(slot=i, channel=1, max_age=10) for i in range(16)],
                     must_reject="too-many-channels"),
    ]

    files["monitor.json"].append(
        monitor_update("duplicate-slot-in-update",
                       "Two values for slot 0 in one write. MUST be rejected: "
                       "nothing says which wins, so a device choosing either "
                       "chooses on every client's behalf. SPEC.md 13.4.",
                       dict(seq=40, count=2),
                       [dict(slot=0, validity=PRESENT, value=1),
                        dict(slot=0, validity=PRESENT, value=2)],
                       must_reject="duplicate-slot"))

    # ---- Link params -----------------------------------------------------
    L = {b["name"]: 1 << b["bit"] for b in schema["bitmasks"]["link_validity"]["bits"]}
    all_valid = L["att_mtu"] | L["ll_data_length"] | L["conn_params"] | L["phy"]
    files["link-params.json"] = [
        case(schema, "link_params", "well-configured-link",
             dict(validity=all_valid, att_mtu=247, ll_max_tx_octets=251,
                  ll_max_rx_octets=251, conn_interval=12, peripheral_latency=0,
                  supervision_timeout=500, phy_tx=2, phy_rx=2),
             "A device that did everything SPEC.md 2 asks: link-layer payload raised to "
             "match the MTU, 2M PHY, 15 ms interval.",
             note="conn_interval is in 1.25 ms units, so 12 is 15 ms."),
        case(schema, "link_params", "mtu-without-data-length",
             dict(validity=all_valid, att_mtu=247, ll_max_tx_octets=27,
                  ll_max_rx_octets=27, conn_interval=24, peripheral_latency=0,
                  supervision_timeout=500, phy_tx=1, phy_rx=1),
             "A large ATT MTU over the default 27-octet link-layer payload. Conforming, "
             "decodable, and roughly three times the radio airtime per byte.",
             note="This is the condition SPEC.md 2.1 exists to prevent and which a "
                  "client cannot observe from its own BLE stack on any platform. It "
                  "is a diagnostic, not a reject: the device is not malformed, it is "
                  "expensive."),
        case(schema, "link_params", "asymmetric-data-length",
             dict(validity=all_valid, att_mtu=247, ll_max_tx_octets=251,
                  ll_max_rx_octets=27, conn_interval=12, peripheral_latency=0,
                  supervision_timeout=500, phy_tx=2, phy_rx=2),
             "Link-layer payload negotiated asymmetrically: the device may send 251 "
             "octets but may only receive 27. Legal, and the two fields are distinct.",
             note="The only vector where ll_max_tx_octets and ll_max_rx_octets differ. "
                  "Without it, a decoder that reads both from the same offset passes "
                  "the whole corpus -- the tx/rx pair is otherwise symmetric in every "
                  "case, which is exactly the kind of hole mutation testing finds and "
                  "review does not."),
        case(schema, "link_params", "phy-not-determinable",
             dict(validity=L["att_mtu"] | L["ll_data_length"] | L["conn_params"],
                  att_mtu=185, ll_max_tx_octets=251, ll_max_rx_octets=251,
                  conn_interval=24, peripheral_latency=0, supervision_timeout=500),
             "A controller that does not expose its PHY. The phy bit is clear, so phy_tx "
             "and phy_rx MUST be reported absent -- NOT decoded as LE 1M.",
             note="LE 1M is 1 and there is no zero member, precisely so that a zeroed "
                  "byte cannot pass for the most common PHY."),
        case(schema, "link_params", "stale-values-behind-cleared-bits",
             dict(validity=0, att_mtu=247, ll_max_tx_octets=251,
                  ll_max_rx_octets=251, conn_interval=12, peripheral_latency=4,
                  supervision_timeout=500, phy_tx=2, phy_rx=2),
             "A non-conforming device that clears every validity bit but leaves the "
             "previous values in the bytes. A decoder MUST report every field absent on "
             "the strength of the mask alone, and MUST NOT read LE 2M or a 247-byte MTU "
             "out of them.",
             canonical=False,
             note="Not byte-canonical, so the round-trip asserts that a conforming "
                  "encoder NORMALISES these bytes to zero. Every validity bit is clear "
                  "in one case deliberately: this is the only coverage the link_params "
                  "encoder's gating rule gets, and a case that cleared just one bit "
                  "would leave the other three gates untested — which is exactly how "
                  "the gps_fix encoder gate went uncovered in the first corpus."),
        case(schema, "link_params", "nothing-determinable",
             dict(validity=0),
             "A stack that exposes none of it. Every field is zero AND every validity "
             "bit is clear; a client MUST conclude 'unknown', not 'MTU 0 on no PHY'."),
        case(schema, "link_params", "unknown-phy-value",
             dict(validity=all_valid, att_mtu=247, ll_max_tx_octets=251,
                  ll_max_rx_octets=251, conn_interval=12, peripheral_latency=0,
                  supervision_timeout=500, phy_tx=9, phy_rx=2),
             "A PHY value from a future Bluetooth revision. A decoder MUST report it "
             "unknown and MUST NOT fall back to LE 1M."),
        case(schema, "link_params", "reserved-validity-bit-set",
             dict(validity=all_valid | (1 << 9), att_mtu=247, ll_max_tx_octets=251,
                  ll_max_rx_octets=251, conn_interval=12, peripheral_latency=0,
                  supervision_timeout=500, phy_tx=2, phy_rx=2),
             "A future minor set link_validity bit 9. A decoder MUST ignore the unknown "
             "bit and decode every known field normally, and a VTP/1.0 encoder MUST "
             "normalise the bit away on transmit (SPEC.md 2).",
             canonical=False),
        {"name": "short-payload",
         "desc": "15 bytes. A truncated control response MUST be rejected whole.",
         "record": "link_params",
         "hex": encode(schema, "link_params", dict(validity=all_valid))[:-1].hex(),
         "must_reject": "length"},
        {"name": "long-payload",
         "desc": "17 bytes. link_params is a fixed-size record with no extension "
                 "mechanism, so trailing bytes MUST be rejected.",
         "record": "link_params",
         "hex": (encode(schema, "link_params", dict(validity=all_valid)) + b"\x00").hex(),
         "must_reject": "length"},
    ]
    # ---- Power -----------------------------------------------------------
    P = {b["name"]: 1 << b["bit"]
         for b in schema["bitmasks"]["power_validity"]["bits"]}
    all_power = P["source"] | P["percent"]
    files["power.json"] = [
        case(schema, "power_state", "running-on-its-battery",
             dict(validity=all_power, source=2, percent=63),
             "A logger on its own pack, a little under two thirds through it. "
             "Both fields valid, which is the ordinary answer."),
        case(schema, "power_state", "external-supply-no-battery",
             dict(validity=P["source"], source=1),
             "A logger wired to the car's ignition feed with no pack at all. "
             "It reports `external` and clears the percent bit; a client MUST "
             "render the charge as unavailable and MUST NOT read 0% out of the "
             "byte.",
             note="This is the case `source` exists for. Without it such a "
                  "device has to report 100% forever -- a magic value meaning "
                  "`not applicable` in the one field a client draws as a gauge, "
                  "which is what SPEC.md 1.1 forbids everywhere else. The "
                  "absent percent is what says there is no battery; `external` "
                  "on its own does not."),
        case(schema, "power_state", "charged-on-external",
             dict(validity=all_power, source=4, percent=100),
             "External supply present and the pack no longer taking charge. "
             "`charged` is a distinct member so a device with the information "
             "does not have to keep saying `charging`.",
             note="percent is 100, the top of the field's range and a legal "
                  "value; 101 is the first that is not (SPEC.md 9.9)."),
        case(schema, "power_state", "gauge-failed-mid-session",
             dict(validity=P["source"], source=2),
             "A device that knows it is on its battery and has lost the gauge "
             "that says how much is left. The honest answer is one bit set and "
             "one clear, which is why percent has a validity bit at all.",
             note="A device that answered 0% here would be reporting a flat "
                  "pack, and a device that answered 100% a full one. Both are "
                  "measurements it does not have."),
        case(schema, "power_state", "nothing-determinable",
             dict(validity=0),
             "Both bits clear. Decodable, and a device that declares the "
             "`power` capability MUST NOT answer this way -- with nothing valid "
             "it has said what a device without the capability says by not "
             "declaring it.",
             note="Not a reject: the record is well formed, and SPEC.md 1.1 is "
                  "about a receiver never producing a plausible wrong value, "
                  "which an all-absent decode is the opposite of. The rule this "
                  "breaks is the device's, and the harness is what asks it."),
        case(schema, "power_state", "stale-values-behind-cleared-bits",
             dict(validity=0, source=2, percent=63),
             "A non-conforming device that clears both validity bits and leaves "
             "the last reading in the bytes. A decoder MUST report both absent "
             "on the strength of the mask alone, and MUST NOT read 63% out of "
             "them.",
             canonical=False,
             note="Not byte-canonical, so the round-trip asserts that a "
                  "conforming encoder NORMALISES these bytes to zero. Both bits "
                  "are clear in one case deliberately -- this is the only "
                  "coverage the power_state encoder's gating rule gets, and a "
                  "case clearing one would leave the other gate untested."),
        case(schema, "power_state", "unknown-source-value",
             dict(validity=all_power, source=9, percent=63),
             "A source member from a later minor version. A decoder MUST report "
             "it unknown and MUST NOT fall back to `discharging` -- it still "
             "has the percentage, and inventing the one field it does not have "
             "is what SPEC.md 1.1 forbids."),
        case(schema, "power_state", "reserved-byte-nonzero",
             dict(validity=all_power, source=2, percent=63, reserved=64),
             "Byte 3 carries something a future minor assigned. Both halves of "
             "SPEC.md 2 apply: a decoder MUST ignore it, and a 1.0 encoder MUST "
             "normalise it to zero.",
             canonical=False),
        case(schema, "power_state", "reserved-validity-bit-set",
             dict(validity=all_power | (1 << 7), source=2, percent=63),
             "A future minor set power_validity bit 7. A decoder MUST ignore the "
             "unknown bit and decode both known fields normally, and a VTP/1.0 "
             "encoder MUST normalise the bit away on transmit (SPEC.md 2).",
             canonical=False),
        case(schema, "power_state", "external-supply-with-a-pack",
             dict(validity=all_power, source=1, percent=40),
             "Plugged in, with a battery at 40%. `external` claims nothing "
             "about a battery, so this is an ordinary state rather than a "
             "contradiction, and both fields are valid together.",
             note="The build this exists for is a USB-C input and a fuel gauge "
                  "with no charge-status pin: it knows it is on external power "
                  "and knows the pack is at 40%, and cannot honestly claim "
                  "`charging` or `charged`. A device that CAN tell reports one "
                  "of those instead."),
        case(schema, "power_state", "percent-above-full",
             dict(validity=all_power, source=2, percent=200),
             "200%. Rejected whole rather than clamped to 100: the byte came out "
             "of the same four as the source, so neither is trustworthy, and a "
             "client that clamps shows a full battery on a device that has lost "
             "track of its own pack.",
             reject="percent-out-of-range"),
        {"name": "short-payload",
         "desc": "3 bytes. A truncated control response MUST be rejected whole.",
         "record": "power_state",
         "hex": encode(schema, "power_state", dict(validity=0))[:-1].hex(),
         "must_reject": "length"},
        {"name": "long-payload",
         "desc": "5 bytes. power_state is a fixed-size record with no extension "
                 "mechanism, so trailing bytes MUST be rejected.",
         "record": "power_state",
         "hex": (encode(schema, "power_state", dict(validity=0))
                 + b"\x00").hex(),
         "must_reject": "length"},
    ]

    # ---- Control response envelope ---------------------------------------
    def resp(opcode, tag, status, detail=b""):
        return bytes([opcode, tag, status]) + detail

    def cr(name, desc, opcode, tag, status, detail=b"", **kw):
        known = {m["value"] for m in schema["enums"]["status"]["members"]}
        c = {"name": name, "desc": desc, "record": "control_response",
             "hex": resp(opcode, tag, status, detail).hex()}
        if "must_reject" not in kw:
            c["expect"] = {"opcode": opcode, "tag": tag, "status": status,
                           "status_known": status in known,
                           "detail_hex": detail.hex()}
        c.update(kw)
        return c

    files["control-response.json"] = [
        cr("ok-with-detail",
           "A successful CAN_SUBSCRIBE: three envelope bytes and the handle it "
           "assigned. SPEC.md 9 -- detail is present because status is ok.",
           0x02, 1, 0, struct.pack("<H", 7)),
        cr("ok-without-detail",
           "A successful CAN_RESET. The opcode has no response detail, so an ok "
           "response is three bytes and that is not a truncated one.",
           0x01, 2, 0),
        cr("refused-is-three-bytes",
           "CAN_SUBSCRIBE refused with bad_params. Exactly three bytes: a client "
           "reading the five it would get on success takes a handle from a "
           "request that failed.",
           0x02, 3, 2),
        cr("busy-is-not-a-refusal",
           "busy says nothing about the request itself. SPEC.md 9 -- a client "
           "MUST retry rather than treat it as refused, and the envelope is the "
           "same three bytes as any other non-ok status.",
           0x02, 4, 5),
        cr("table-full",
           "table_full on a subscription install. SPEC.md 9.2 -- the device is "
           "out of slots, which is a property of the device rather than of the "
           "request, and carries no detail.",
           0x03, 5, 3),
        cr("unknown-status",
           "A status this build does not recognise. SPEC.md 11.4 -- it MUST "
           "decode as unknown and MUST NOT be substituted for a default; in "
           "particular it is neither ok nor a specific failure.",
           0x02, 6, 200),
        cr("unknown-opcode-detail-is-opaque",
           "An ok response to an opcode this build does not implement, carrying "
           "four bytes of detail. SPEC.md 11.3 lets a minor version add opcodes "
           "with any payload, so the envelope decoder MUST carry the detail "
           "rather than reject it.",
           0x77, 7, 0, bytes.fromhex("DEADBEEF")),
        cr("detail-on-error",
           "bad_params carrying two bytes of detail. MUST be rejected: detail is "
           "present if and only if status is ok, and a client that has already "
           "decided the request succeeded would read those bytes as a handle.",
           0x02, 8, 2, struct.pack("<H", 7), must_reject="detail-on-error"),
        cr("short-payload",
           "Two bytes: an opcode and a tag with no status. MUST be rejected "
           "rather than read as a status of whatever follows.",
           0x02, 9, 0, must_reject="length"),
    ]
    files["control-response.json"][-1]["hex"] = resp(0x02, 9, 0)[:2].hex()

    # ---- TIME_SYNC ------------------------------------------------------
    def ts(name, desc, rx, tx, **kw):
        c = {"name": name, "desc": desc, "record": "time_sync",
             "hex": struct.pack("<QQ", rx, tx).hex()}
        if "must_reject" not in kw:
            c["expect"] = {"t_device_rx": rx, "t_device_tx": tx,
                           "processing_us": tx - rx}
        c.update(kw)
        return c

    files["time-sync.json"] = [
        ts("typical-processing",
           "A request answered 1.2 ms after it arrived. SPEC.md 9.7 -- the gap "
           "is the device's own processing time, which is the term a client "
           "takes out of the round trip to bound its error.",
           4_000_000_000, 4_000_001_200),
        ts("answered-immediately",
           "Both readings equal: the device answered within one tick of its "
           "own clock. Legal, and NOT the same as a device reporting one "
           "timestamp twice because it never took the second.",
           7_500_000, 7_500_000),
        ts("clock-near-wrap",
           "Both readings within a microsecond of the u64 ceiling. SPEC.md 8.1 "
           "-- the clock will not reach this in half a million years, but a "
           "decoder that sign-extends or overflows here is wrong now.",
           (1 << 64) - 2, (1 << 64) - 1),
        ts("tx-before-rx",
           "The device reports finishing its answer before the request "
           "arrived. MUST be rejected: a client computing delay from it gets a "
           "negative round trip, and halved into an offset that is a "
           "confidently wrong clock rather than an obviously broken one.",
           9_000_000, 8_999_000, must_reject="tx-before-rx"),
        ts("short-payload",
           "Eight bytes: one timestamp where two are required. MUST be "
           "rejected rather than read as a device that answered instantly.",
           1, 2, must_reject="length"),
        ts("long-payload",
           "Seventeen bytes. time_sync is fixed-size with no extension "
           "mechanism, so trailing bytes MUST be rejected.",
           1, 2, must_reject="length"),
    ]
    files["time-sync.json"][-2]["hex"] = struct.pack("<Q", 1).hex()
    files["time-sync.json"][-1]["hex"] = (struct.pack("<QQ", 1, 2) + b"\x00").hex()

    # ---- Producer contract ------------------------------------------------
    # Everything above tests DECODING. These test the other direction: inputs a
    # conforming encoder must refuse rather than reshape. They cannot be
    # expressed as byte vectors, because the whole point is that the bytes are
    # never produced -- an encoder given an out-of-range identifier used to
    # emit a perfectly valid frame for a DIFFERENT one, and no decode input
    # reaches that.
    files["encoders.json"] = [
        {"name": "can-id-above-arbitration-field",
         "record": "can_batch", "must_refuse": True,
         "desc": "An identifier of 0x3FFFFFFF. MUST be refused, not masked: "
                 "masking silently produced 0x1FFFFFFF, a frame the caller "
                 "never asked for, on the field a client uses to decide what "
                 "the payload means. The format is carried by `extended`, not "
                 "by high bits of `id`.",
         "input": {"header": dict(seq=0, dropped=0, t_base=0, count=1, flags=0),
                   "records": [dict(dt=0, id=0x3FFFFFFF, extended=True, fd=False,
                                    rtr=False, len=1, payload="00")]}},
        {"name": "can-id-negative",
         "record": "can_batch", "must_refuse": True,
         "desc": "An identifier of -1. Masking turned it into 0x1FFFFFFF -- the "
                 "same frame an over-large identifier became, so two different "
                 "mistakes produced one wrong answer.",
         "input": {"header": dict(seq=0, dropped=0, t_base=0, count=1, flags=0),
                   "records": [dict(dt=0, id=-1, extended=True, fd=False,
                                    rtr=False, len=1, payload="00")]}},
        {"name": "can-first-record-dt-nonzero",
         "record": "can_batch", "must_refuse": True,
         "desc": "SPEC.md 6.1 -- t_base IS record 0's arrival time.",
         "input": {"header": dict(seq=0, dropped=0, t_base=0, count=1, flags=0),
                   "records": [dict(dt=5, id=0x1A0, extended=False, fd=False,
                                    rtr=False, len=1, payload="00")]}},
        {"name": "can-classic-nine-bytes",
         "record": "can_batch", "must_refuse": True,
         "desc": "SPEC.md 6.10 -- a Classic frame carries 0..8.",
         "input": {"header": dict(seq=0, dropped=0, t_base=0, count=1, flags=0),
                   "records": [dict(dt=0, id=0x1A0, extended=False, fd=False,
                                    rtr=False, len=9, payload="00" * 9)]}},
        # `len` is the field a receiver uses to walk the batch, so a `len`
        # that disagrees with the payload behind it is a frame whose remaining
        # records are at the wrong offsets. Both directions, because an encoder
        # that pads and an encoder that truncates are the same defect with
        # different symptoms -- and the C adapter did one of each before these
        # existed, answering `ok` to both.
        {"name": "can-len-longer-than-payload",
         "record": "can_batch", "must_refuse": True,
         "desc": "SPEC.md 6 -- len 8 with one byte of payload. Padding to "
                 "eight publishes seven bytes the caller never supplied, on a "
                 "bus signal a client will decode as a measurement.",
         "input": {"header": dict(seq=0, dropped=0, t_base=0, count=1, flags=0),
                   "records": [dict(dt=0, id=0x1A0, extended=False, fd=False,
                                    rtr=False, len=8, payload="00")]}},
        {"name": "can-len-shorter-than-payload",
         "record": "can_batch", "must_refuse": True,
         "desc": "SPEC.md 6 -- len 0 with one byte of payload. Discarding it "
                 "silently drops data the caller asked to send.",
         "input": {"header": dict(seq=0, dropped=0, t_base=0, count=1, flags=0),
                   "records": [dict(dt=0, id=0x1A0, extended=False, fd=False,
                                    rtr=False, len=0, payload="aa")]}},
        {"name": "gps-latitude-beyond-the-pole",
         "record": "gps_fix", "must_refuse": True,
         "desc": "SPEC.md 5.4 -- a latitude of 91 degrees, with the position "
                 "bit set so the range rule applies.",
         "input": {"fix": dict(seq=0, validity=V["position"], lat=910_000_000,
                               lon=0, ext_count=0)}},
        {"name": "gps-rtk-float-and-fixed",
         "record": "gps_fix", "must_refuse": True,
         "desc": "SPEC.md 5.3 -- the two RTK bits are exclusive.",
         "input": {"fix": dict(seq=0, validity=0, fix_flags=0b0000_0111,
                               ext_count=0)}},
        {"name": "gps-rtk-without-differential",
         "record": "gps_fix", "must_refuse": True,
         "desc": "SPEC.md 5.3 -- an RTK solution is a differentially "
                 "corrected one, so the bit is implied and not optional.",
         "input": {"fix": dict(seq=0, validity=0, fix_flags=0b0000_0010,
                               ext_count=0)}},
        {"name": "gps-ext-count-disagrees",
         "record": "gps_fix", "must_refuse": True,
         "desc": "SPEC.md 5.5 -- three extensions declared, none supplied.",
         "input": {"fix": dict(seq=0, validity=0, ext_count=3), "ext_hex": ""}},
        {"name": "imu-period-zero",
         "record": "imu_batch", "must_refuse": True,
         "desc": "SPEC.md 7 -- zero says every sample was taken at one instant.",
         "input": {"header": dict(seq=0, dropped=0, t_base=0, period=0, count=1,
                                  flags=0b011),
                   "samples": [dict(ax=1, ay=2, az=3, gx=4, gy=5, gz=6)]}},
        {"name": "can-empty-batch",
         "record": "can_batch", "must_refuse": True,
         "desc": "SPEC.md 6.2 -- t_base names record 0, so a batch with no "
                 "records timestamps a frame that does not exist.",
         "input": {"header": dict(seq=0, dropped=0, t_base=0, count=0, flags=0),
                   "records": []}},
        {"name": "imu-empty-batch",
         "record": "imu_batch", "must_refuse": True,
         "desc": "SPEC.md 7 -- t_base names sample 0, so a batch with no "
                 "samples timestamps one that does not exist.",
         "input": {"header": dict(seq=0, dropped=0, t_base=0, period=1000,
                                  count=0, flags=0b011),
                   "samples": []}},
        {"name": "monitor-declaration-repeats-a-slot",
         "record": "monitor_list", "must_refuse": True,
         "desc": "SPEC.md 13.3 -- the decoder already rejected this, so an "
                 "encoder emitting it produced a declaration its own reader "
                 "refuses.",
         "input": {"declaration": dict(count=2),
                   "entries": [dict(slot=0, channel=1, max_age=10),
                               dict(slot=0, channel=7, max_age=10)]}},
        {"name": "monitor-channel-with-no-deadline",
         "record": "monitor_list", "must_refuse": True,
         "desc": "SPEC.md 13.5 -- every declared channel carries a deadline. "
                 "A channel with none is a value a device can go on "
                 "displaying forever after the client stopped sending it.",
         "input": {"declaration": dict(count=2),
                   "entries": [dict(slot=0, channel=1, max_age=20),
                               dict(slot=1, channel=3, max_age=0)]}},
        {"name": "monitor-asks-for-more-than-fits",
         "record": "monitor_list", "must_refuse": True,
         "desc": "SPEC.md 13.4 -- more channels than fit in one complete write.",
         "input": {"declaration": dict(count=16),
                   "entries": [dict(slot=i, channel=1, max_age=10)
                               for i in range(16)]}},
        {"name": "monitor-update-with-no-values",
         "record": "monitor_update", "must_refuse": True,
         "desc": "SPEC.md 13.4 -- an empty write is not a complete statement "
                 "of what the client can supply.",
         "input": {"header": dict(seq=1, count=0, reserved=0), "values": []}},
        {"name": "monitor-update-repeats-a-slot",
         "record": "monitor_update", "must_refuse": True,
         "desc": "SPEC.md 13.4 -- nothing says which of the two wins.",
         "input": {"header": dict(seq=0, count=2, reserved=0),
                   "values": [dict(slot=3, validity=PRESENT, value=1),
                              dict(slot=3, validity=PRESENT, value=2)]}},
        {"name": "control-detail-on-a-refusal",
         "record": "control_response", "must_refuse": True,
         "desc": "SPEC.md 9 -- detail is present if and only if status is ok.",
         "input": {"opcode": 0x02, "tag": 1, "status": 2, "detail_hex": "0700"}},
        {"name": "time-sync-answered-before-asked",
         "record": "time_sync", "must_refuse": True,
         "desc": "SPEC.md 9.7 -- a negative round trip halved into an offset is "
                 "a confidently wrong clock.",
         "input": {"t_device_rx": 9_000_000, "t_device_tx": 8_999_000}},
        # Cases that MUST encode, so a harness refusing everything cannot
        # pass. Each pins the bytes as well: `expect_hex` is built here from
        # the schema's own offsets, not from either reference encoder, so two
        # implementations agreeing on it are agreeing with the source of truth
        # rather than with each other.
        {"name": "can-ordinary-batch",
         "record": "can_batch", "must_refuse": False,
         "desc": "A frame at the top of the arbitration field, which is legal.",
         "input": {"header": dict(seq=0, dropped=0, t_base=0, count=1, flags=0),
                   "records": [dict(dt=0, id=0x1FFFFFFF, extended=True, fd=False,
                                    rtr=False, len=1, payload="00")]},
         "expect_hex": (
             encode(schema, "can_header",
                    dict(seq=0, dropped=0, t_base=0, count=1, flags=0, reserved=0))
             + encode(schema, "can_record",
                      dict(dt=0, id=0x1FFFFFFF | (1 << 29), len=1))
             + b"\x00").hex()},
        {"name": "monitor-well-formed-declaration",
         "record": "monitor_list", "must_refuse": False,
         "desc": "Distinct slots, inside the channel cap.",
         "input": {"declaration": dict(count=2),
                   "entries": [dict(slot=0, channel=1, max_age=10),
                               dict(slot=1, channel=3, max_age=255)]},
         "expect_hex": (
             encode(schema, "monitor_declaration", dict(count=2, reserved=0))
             + encode(schema, "monitor_channel", dict(slot=0, channel=1, max_age=10))
             + encode(schema, "monitor_channel", dict(slot=1, channel=3, max_age=255))
         ).hex()},
        {"name": "imu-ordinary-batch",
         "record": "imu_batch", "must_refuse": False,
         "desc": "Accel and gyro both present, a non-zero period, two samples.",
         "input": {"header": dict(seq=7, dropped=0, t_base=1_000_000,
                                  period=10_000, count=2, flags=0b011),
                   "samples": [dict(ax=1, ay=-2, az=1000, gx=4, gy=-5, gz=6),
                               dict(ax=2, ay=-3, az=1001, gx=5, gy=-6, gz=7)]},
         "expect_hex": (
             encode(schema, "imu_header",
                    dict(seq=7, dropped=0, t_base=1_000_000, period=10_000,
                         count=2, flags=0b011, reserved=0))
             + encode(schema, "imu_sample",
                      dict(ax=1, ay=-2, az=1000, gx=4, gy=-5, gz=6))
             + encode(schema, "imu_sample",
                      dict(ax=2, ay=-3, az=1001, gx=5, gy=-6, gz=7))
         ).hex()},
        {"name": "time-sync-simultaneous",
         "record": "time_sync", "must_refuse": False,
         "desc": "SPEC.md 9.7 -- t_device_tx MUST NOT be EARLIER than "
                 "t_device_rx, so equal readings are legal. A device whose "
                 "clock cannot resolve the two instants apart reports them "
                 "equal rather than inventing a gap.",
         "input": {"t_device_rx": 9_000_000, "t_device_tx": 9_000_000},
         "expect_hex": encode(schema, "time_sync",
                              dict(t_device_rx=9_000_000,
                                   t_device_tx=9_000_000)).hex()},
        # SPEC.md 4.1, in the producer direction.
        {"name": "info-can-without-control",
         "record": "info", "must_refuse": True,
         "desc": "SPEC.md 4.1 -- `can` requires `control`. A device that "
                 "publishes this has advertised a role no client can use, "
                 "because CAN_SUBSCRIBE is the only way to install one.",
         "input": dict(protocol_major=1, protocol_minor=0,
                       capabilities=C["gps"] | C["can"],
                       gps_rate_hz=10, gps_max_rate_hz=10,
                       can_subscription_slots=32, can_max_frames_per_s=2000,
                       max_notify_bytes=244)},
        {"name": "info-capacity-without-capability",
         "record": "info", "must_refuse": True,
         "desc": "SPEC.md 4.1 -- every CAN capacity is zero while the `can` "
                 "bit is clear. Masking the capacity instead would publish a "
                 "different device from the one the caller described.",
         "input": dict(protocol_major=1, protocol_minor=0,
                       capabilities=C["gps"], gps_rate_hz=10,
                       gps_max_rate_hz=10, can_max_frames_per_s=4000,
                       max_notify_bytes=185)},
        *reserved_bit_cases(schema),
        {"name": "control-detail-on-ok",
         "record": "control_response", "must_refuse": False,
         "desc": "SPEC.md 9 -- detail accompanies `ok`, and only `ok`. The "
                 "refusal case above is only half the rule.",
         "input": {"opcode": 0x02, "tag": 1, "status": 0, "detail_hex": "0700"},
         "expect_hex": (encode(schema, "control_response",
                               dict(opcode=0x02, tag=1, status=0))
                        + bytes.fromhex("0700")).hex()},
    ]

    return files


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if artefacts are stale")
    args = ap.parse_args()

    schema = yaml.safe_load(SCHEMA.read_text())
    validate(schema)
    global SCHEMA_ENUMS
    SCHEMA_ENUMS = {n: e["members"] for n, e in schema["enums"].items()}
    substitute(ROOT / "SPEC.md", spec_tables(schema))
    emit(ROOT / "reference" / "c" / "vtp1_generated.h", c_header(schema))
    # A JSON mirror of the schema, so consumers without a YAML parser (the
    # repository's Node CI guards, browser tooling) read the same source of
    # truth rather than a second copy that drifts.
    emit(ROOT / "schema" / "vtp1.json", json.dumps(schema, indent=2, sort_keys=False) + "\n")
    for fname, cases in vectors(schema).items():
        # The producer corpus is not a decode corpus and must not sit in
        # vectors/: its cases carry structured input rather than bytes, and
        # every tool that walks vectors/*.json expects `hex`.
        where = (ROOT / "conformance" if fname == "encoders.json"
                 else ROOT / "conformance" / "vectors")
        emit(where / fname,
             json.dumps({"protocol": "VTP/1", "generated_by": "tools/generate.py",
                         "cases": cases}, indent=2) + "\n")

    stale = flush(args.check)
    if args.check and stale:
        print(f"\n{stale} artefact(s) out of date. Run: python3 tools/generate.py",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
