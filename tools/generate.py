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
        if f.get("desc"):
            notes.append(f["desc"])
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
            lines.append(f"| {b['bit']} | `{b['name']}` | {b.get('desc', '—')} |")
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

    lines = ["| Opcode | Command | Params | Response detail | Notes |",
             "| --- | --- | --- | --- | --- |"]
    for op in schema["control"]["opcodes"]:
        params = f"`{op['params']}`" if op["params"] else "—"
        # Every opcode declares its response detail. Leaving that to prose is
        # what made three of these unimplementable.
        resp = op.get("response")
        if resp is None:
            sys.exit(f"control: opcode {op['name']} declares no response detail")
        resp = f"`{resp}`" if resp else "—"
        lines.append(f"| `0x{op['value']:02X}` | `{op['name']}` | {params} | "
                     f"{resp} | {op.get('desc', '—')} |")
    out["control"] = "\n".join(lines)

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
    gated = values
    if schema["records"][record].get("validity"):
        gated = _gate(schema, record, values)
        if canonical:
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
    # and SPEC.md 11.3's "an unknown enum value MUST stay unknown" is exactly
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
        # What a conforming encoder MUST turn these bytes into.
        c["expect_roundtrip_hex"] = (encode(schema, record, gated) + extra).hex()
    return c


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
        case(schema, "gps_fix", "unknown-fix-type", dict(nominal, seq=6, fix_type=200),
             "An enum value from a future minor. A decoder MUST report unknown, "
             "and MUST NOT fall back to 3D.",
             note="Falling back to a plausible default is the sentinel mistake in a "
                  "different costume."),
        case(schema, "gps_fix", "reserved-validity-bits-set",
             dict(nominal, seq=7, validity=full | (1 << 20)),
             "A future minor set validity bit 20. A decoder MUST ignore the unknown bit "
             "and decode every known field normally. Rejecting here breaks forward compatibility."),
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
                  [can_rec(5, 0x18DAF110, bytes.fromhex("AABB"), ext=True)],
                  [{"dt": 5, "id": 0x18DAF110, "extended": True, "fd": False, "rtr": False,
                    "len": 2, "payload": "aabb", "t_device_us": 7_000_050}]),
        can_batch("can-fd-64-byte",
                  "A CAN FD frame carrying the maximum 64-byte payload.",
                  dict(seq=4, dropped=0, t_base=9_000_000, count=1, flags=0),
                  [can_rec(1, 0x2F0, bytes(range(64)), fd=True)],
                  [{"dt": 1, "id": 0x2F0, "extended": False, "fd": True, "rtr": False,
                    "len": 64, "payload": bytes(range(64)).hex(),
                    "t_device_us": 9_000_010}]),
        can_batch("t-base-near-wrap",
                  "t_base within 100 us of the u64 ceiling, so t_base + dt*10 wraps. "
                  "SPEC.md 8 defines the arithmetic as modulo 2^64; a decoder using "
                  "arbitrary-precision integers MUST still report the wrapped value.",
                  dict(seq=11, dropped=0, t_base=(1 << 64) - 100, count=1, flags=0),
                  [can_rec(20, 0x1A0, bytes.fromhex("00"))],
                  [{"dt": 20, "id": 0x1A0, "extended": False, "fd": False, "rtr": False,
                    "len": 1, "payload": "00",
                    "t_device_us": ((1 << 64) - 100 + 200) & ((1 << 64) - 1)}],
                  note="Unreachable on real hardware -- a microsecond clock takes over "
                       "half a million years to get here -- but the two reference "
                       "decoders disagreed on it, which is a specification gap rather "
                       "than a hardware one."),
        can_batch("empty-batch",
                  "count 0. Legal, and means the bus is quiet — NOT an error and NOT a "
                  "disconnect. A decoder MUST accept it.",
                  dict(seq=5, dropped=0, t_base=11_000_000, count=0, flags=0), [], []),
        can_batch("shedding-load",
                  "The device dropped 400 frames and is signalling overload in flags bit 0. "
                  "A decoder MUST surface both.",
                  dict(seq=6, dropped=400, t_base=13_000_000, count=1, flags=0x01),
                  [can_rec(0, 0x1A0, bytes.fromhex("00"))],
                  [{"dt": 0, "id": 0x1A0, "extended": False, "fd": False, "rtr": False,
                    "len": 1, "payload": "00", "t_device_us": 13_000_000}]),
        {"name": "len-above-maximum",
         "desc": "A record declaring a 100-byte payload. `len` is 0..64 even for CAN "
                 "FD, so the record is malformed and the batch MUST be rejected — not "
                 "clamped, and not read as 100 bytes.",
         "record": "can_batch",
         "hex": (encode(schema, "can_header",
                        dict(seq=10, dropped=0, t_base=1, count=1, flags=0))
                 + struct.pack("<HIB", 0, 0x1A0, 100) + bytes(100)).hex(),
         "must_reject": "bad-length",
         "note": "The corpus cannot reach this rule by construction — every legal "
                 "vector has len <= 64, so removing the bound check changed nothing "
                 "and all 43 vectors still passed. Found by source mutation, which is "
                 "why tools/mutate.py earns its place alongside "
                 "tools/check_corpus.py."},
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
                 "A decoder MUST ignore them, not reject.",
         "record": "can_batch",
         "hex": (encode(schema, "can_header",
                        dict(seq=8, dropped=0, t_base=1, count=0, flags=0, reserved=0xBEEF)).hex()),
         "expect": {"header": {"seq": 8, "dropped": 0, "t_base": 1, "count": 0,
                               "flags": 0, "reserved": 0xBEEF}, "records": []}},
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
             "expect": {"header": {f["name"]: hdr.get(f["name"], 0)
                                   for f in schema["records"]["imu_header"]["fields"]},
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
        {"name": "long-payload",
         "desc": "One sample declared, one sample present, plus a trailing byte. The "
                 "length MUST equal the header plus count samples exactly.",
         "record": "imu_batch",
         "hex": (encode(schema, "imu_header",
                        dict(seq=5, dropped=0, t_base=1, period=5000, count=1, flags=0b011))
                 + encode(schema, "imu_sample", dict(ax=1, ay=2, az=3, gx=4, gy=5, gz=6))
                 + b"\x00").hex(),
         "must_reject": "length"},
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
                  can_max_payload=8, clock_flags=0b01, max_notify_bytes=244),
             "A typical dual-role module."),
        case(schema, "info", "gps-only-no-control",
             dict(protocol_major=1, protocol_minor=0, capabilities=C["gps"],
                  gps_rate_hz=10, gps_max_rate_hz=10, can_max_payload=0,
                  max_notify_bytes=185),
             "A GPS-only board with no control channel. Every CAN capacity figure is zero "
             "-- including can_max_payload, which is a capacity like any other -- and a "
             "client MUST NOT infer a default."),
        case(schema, "info", "future-minor-unknown-capability",
             dict(protocol_major=1, protocol_minor=7,
                  capabilities=C["gps"] | C["can"] | C["imu"] | C["can_fd"] | (1 << 19),
                  gps_rate_hz=25, gps_max_rate_hz=25, can_subscription_slots=32,
                  can_max_frames_per_s=4000, imu_rate_hz=833, imu_max_rate_hz=833,
                  can_max_payload=64, clock_flags=0b11, max_notify_bytes=498),
             "Minor 7 with a capability bit this client has never heard of. A client MUST "
             "ignore the unknown bit and use everything it does understand."),
        case(schema, "info", "rate-below-maximum",
             dict(protocol_major=1, protocol_minor=0,
                  capabilities=C["gps"] | C["imu"] | C["control"],
                  gps_rate_hz=10, gps_max_rate_hz=25, can_max_payload=8,
                  imu_rate_hz=100, imu_max_rate_hz=833, max_notify_bytes=244),
             "A device running below its ceiling: 10 Hz of a possible 25, 100 Hz of a "
             "possible 833. Current rate and maximum rate are separate fields and a "
             "client MUST NOT read one for the other.",
             note="The only vector where the current and maximum rates differ. Without "
                  "it a decoder can read gps_rate_hz from gps_max_rate_hz's offset and "
                  "pass the whole corpus -- found by tools/mutate.py, not by review."),
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
    EXACT = 0x1FFFFFFF

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
                 "with mask 0x1FFFFFFF, so that is what the table reports.",
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
             "bit and decode every known field normally."),
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
        emit(ROOT / "conformance" / "vectors" / fname,
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
