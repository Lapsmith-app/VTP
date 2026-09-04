#!/usr/bin/env python3
"""VTP/1 reference encoder — Python, schema-driven.

The device-side half, and the counterpart to reference/c/vtp1_encode.c. It
lives in its own module for the same reason the C encoder is its own
translation unit: a client needs only the decoder, a device needs only the
encoder, and neither should have to carry the other.

Like the Python decoder this derives every offset from schema/vtp1.yaml at
runtime, where the C encoder compiles generated constants in. Both producing
byte-identical output for the whole corpus is therefore also a check that the
schema and the generated C header agree.

**The encoder enforces the specification rather than trusting its caller.** A
field whose validity bit is clear is written as zero whatever the caller left in
the dict (SPEC.md §5.1), and an IMU triple whose presence flag is clear is
written as zero whatever the caller passed (SPEC.md §7). Firmware that computes
a stale altitude and then clears the bit cannot leak the stale value onto the
wire.

Every function accepts exactly what the matching decoder in vtp1.py returns, so
a decode/encode round-trip is a single call and needs no adapter.
"""
import pathlib, struct, sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = yaml.safe_load((ROOT / "schema" / "vtp1.yaml").read_text())

PACK = {"u8": "B", "i8": "b", "u16": "H", "i16": "h",
        "u32": "I", "i32": "i", "u64": "Q", "i64": "q"}

# SPEC.md §7 — imu_header.flags. Presence, not validity: the sample record has
# no validity mask of its own, so the gating lives in the batch header.
IMU_HAS_ACCEL = 0x01
IMU_HAS_GYRO = 0x02

CAN_ID_MASK = 0x1FFFFFFF
# SPEC.md §6.10 -- the payload lengths a CAN FD DLC can express.
FD_LENGTHS = frozenset((0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64))
CAN_EXTENDED = 1 << 29
CAN_FD = 1 << 30
CAN_RTR = 1 << 31


class EncodeError(Exception):
    """Values that cannot be represented on the wire."""


def _record(name):
    return SCHEMA["records"][name]


def _gate(name, values):
    """Zero every field of `name` whose validity bit is clear.

    Driven off the record's `validity` key rather than its name, so a record
    that gains a mask in a later minor is covered without editing this.
    """
    rec = _record(name)
    mask = rec.get("validity")
    if not mask:
        return values
    bit_of = {b["name"]: b["bit"] for b in SCHEMA["bitmasks"][mask]["bits"]}
    validity = values.get("validity", 0)
    gated = dict(values)
    for f in rec["fields"]:
        bit = f.get("valid_bit")
        if bit is not None and not (validity & (1 << bit_of[bit])):
            gated[f["name"]] = 0
    return gated


def _known_bits(bitmask):
    """The bits of `bitmask` this version has assigned a meaning to.

    SPEC.md §2 — reserved bits are ZERO on transmit. `_zero_reserved` below
    already covered whole reserved FIELDS; the reserved PORTION of a bitmask
    had no expression anywhere, so a caller handing this encoder a capabilities
    word with bit 19 set, or a gps validity word with bit 30 set, had it
    transmitted verbatim. Every conforming receiver is required to ignore those
    bits, which is exactly why writing them is forbidden: they are the only
    bits on the wire a later minor version may redefine, and a 1.0 device that
    sets one has published a claim it cannot make.
    """
    spec = SCHEMA["bitmasks"][bitmask]
    if "reserved_from" not in spec:
        return (1 << (spec["width"] * 8)) - 1
    # From the NAMED bits, not from reserved_from: a bit retired below the
    # boundary -- capabilities bit 7, assigned by a pre-1.0 draft -- is
    # reserved exactly like the range above it.
    known = 0
    for b in spec["bits"]:
        known |= 1 << b["bit"]
    return known


def _normalise_bitmasks(name, values):
    """Mask the reserved portion of every bitmask field of `name`."""
    rec = _record(name)
    masked = None
    for f in rec["fields"]:
        bm = f.get("bitmask")
        if not bm:
            continue
        if masked is None:
            masked = dict(values)
        masked[f["name"]] = values.get(f["name"], 0) & _known_bits(bm)
    return values if masked is None else masked


def _zero_reserved(name, values):
    """SPEC.md §2 — a reserved field is zero on transmit.

    This encoder used to write the caller's value through, reasoning that a
    later minor might have assigned those bytes and that erasing them would be
    worse. But this is a 1.0 encoder: a build that knows what the bytes mean is
    a build that names them, and until then "MUST be zero on transmit" is the
    rule. Writing them through let a caller put arbitrary content into a field
    every conforming receiver is required to ignore.
    """
    rec = _record(name)
    reserved = [f["name"] for f in rec["fields"] if f.get("reserved")]
    if not reserved:
        return values
    return dict(values, **{f: 0 for f in reserved})


def _pack(name, values):
    # Every record goes through BOTH normalisations on its way to the wire, so
    # no encoder function has to remember either.
    #
    # `_zero_reserved` used to be called by hand, per record, by the functions
    # that happened to have a reserved field when they were written --
    # `encode_info` did not, because Info had none. It gained one the moment
    # `can_max_payload` became derivable, and the encoder went on transmitting
    # whatever the caller left there. A rule applied by remembering is a rule
    # that lapses when the schema changes.
    values = _zero_reserved(name, _normalise_bitmasks(name, values))
    rec = _record(name)
    buf = bytearray(rec["size"])
    for f in rec["fields"]:
        try:
            struct.pack_into("<" + PACK[f["type"]], buf, f["offset"],
                             values.get(f["name"], 0))
        except struct.error as exc:
            raise EncodeError(f"{name}.{f['name']}: {exc}") from None
    return bytes(buf)


def _payload_bytes(record):
    raw = record.get("payload", b"")
    data = bytes.fromhex(raw) if isinstance(raw, str) else bytes(raw)
    if len(data) != record["len"]:
        raise EncodeError(
            f"can_record.len is {record['len']} but the payload is "
            f"{len(data)} bytes")
    return data


def _check_gps_ranges(fix):
    """SPEC.md §5.4. Only where the validity bit claims the field means
    something -- a cleared bit is written as zero, which is always in range."""
    bit = {b["name"]: b["bit"] for b in SCHEMA["bitmasks"]["gps_validity"]["bits"]}
    validity = fix.get("validity", 0)
    if validity & (1 << bit["position"]):
        if abs(fix.get("lat", 0)) > 900_000_000:
            raise EncodeError(f"gps_fix.lat {fix['lat']} is outside ±90°")
        if abs(fix.get("lon", 0)) > 1_800_000_000:
            raise EncodeError(f"gps_fix.lon {fix['lon']} is outside ±180°")
    if validity & (1 << bit["head_mot"]):
        if not 0 <= fix.get("head_mot", 0) < 36_000_000:
            raise EncodeError(
                f"gps_fix.head_mot {fix['head_mot']} is outside 0°..360°")


FIX_FLAG = {b["name"]: 1 << b["bit"]
            for b in SCHEMA["bitmasks"]["fix_flags"]["bits"]}


def _check_fix_flags(fix):
    """SPEC.md 5.3 -- a carrier-phase solution has either resolved its
integer ambiguities or it has not, so both RTK bits at once is a claim about
solution quality that means nothing. The natural client reading of the pair is
"fixed wins", which upgrades a device's accuracy claim on the strength of a
bug. And either RTK bit implies `differential`, because an RTK solution IS a
differentially corrected one."""
    flags = fix.get("fix_flags", 0)
    rtk = flags & (FIX_FLAG["rtk_float"] | FIX_FLAG["rtk_fixed"])
    if rtk == (FIX_FLAG["rtk_float"] | FIX_FLAG["rtk_fixed"]):
        raise EncodeError(
            "gps_fix.fix_flags sets both rtk_float and rtk_fixed; a "
            "carrier-phase solution is one or the other")
    if rtk and not flags & FIX_FLAG["differential"]:
        raise EncodeError(
            "gps_fix.fix_flags claims an RTK solution without differential; "
            "an RTK solution is a differentially corrected one")


FIX_TYPE = {m["name"]: m["value"] for m in SCHEMA["enums"]["fix_type"]["members"]}


def _check_solution_scoped_bits(fix):
    """SPEC.md 5.2 -- num_sv counts the satellites used in the solution
`fix_type` NAMES, and p_dop describes a position's geometry, so the two
adjacent bits do not move together. A time-only solution is computed from real
satellites and has no position for a dilution of precision to describe, and a
`fix_type` of `none` reached no solution for a satellite to have been used in.
Either published is a plausible wrong value: a PDOP a client reads as evidence
of a position, or the count of satellites TRACKED wearing the name of the count
USED."""
    bit = {b["name"]: 1 << b["bit"]
           for b in SCHEMA["bitmasks"]["gps_validity"]["bits"]}
    validity = fix.get("validity", 0)
    fix_type = fix.get("fix_type", 0)
    positionless = (fix_type in (FIX_TYPE["none"], FIX_TYPE["time_only"])
                    or not validity & bit["position"])
    if validity & bit["p_dop"] and positionless:
        raise EncodeError(
            "gps_fix claims a p_dop on a fix reporting no position "
            "(fix_type {}); dilution of precision describes a position's "
            "geometry".format(fix_type))
    if validity & bit["num_sv"] and fix_type == FIX_TYPE["none"]:
        raise EncodeError(
            "gps_fix claims a num_sv beside fix_type none; no solution was "
            "reached for a satellite to have been used in, and the tracked "
            "count has no field in VTP/1")
    if (validity & bit["position"]
            and fix_type in (FIX_TYPE["none"], FIX_TYPE["time_only"])):
        raise EncodeError(
            "gps_fix carries a valid position beside fix_type {}, which names "
            "no position solution; the record would say both and nothing on "
            "the wire says which half is the defect".format(fix_type))


def encode_gps_fix(fix, ext=b""):
    """SPEC.md §5. `ext` is appended verbatim and MUST match `ext_count`.

    "Must" had been a docstring rather than a check, so this encoder happily
    produced a fix declaring three extensions and carrying none -- a record its
    own decoder rejects as `ext-truncated`. An encoder that emits what it
    cannot read hands the defect to whoever is on the other end of the link.

    The extensions are walked rather than counted, because §5.5 defines them as
    `[type][len][value]` and the only way to know that `ext_count` is right is
    to follow them to the end.
    """
    _check_gps_ranges(fix)
    _check_fix_flags(fix)
    _check_solution_scoped_bits(fix)
    ext = bytes(ext)
    declared = fix.get("ext_count", 0)
    off, seen = 0, 0
    while off < len(ext):
        if off + 2 > len(ext):
            raise EncodeError(
                f"gps_fix extension {seen} is truncated: a header needs two "
                f"bytes and {len(ext) - off} remain")
        end = off + 2 + ext[off + 1]
        if end > len(ext):
            raise EncodeError(
                f"gps_fix extension {seen} declares {ext[off + 1]} value "
                f"byte(s) and only {len(ext) - off - 2} remain")
        off, seen = end, seen + 1
    if seen != declared:
        raise EncodeError(
            f"gps_fix.ext_count is {declared} but the extension bytes hold "
            f"{seen}")
    return _pack("gps_fix", _gate("gps_fix", fix)) + ext


def encode_can_batch(header, records):
    """SPEC.md §6. One batch header followed by `count` frame records."""
    if len(records) != header.get("count", 0):
        raise EncodeError(
            f"can_header.count is {header.get('count', 0)} but "
            f"{len(records)} record(s) were supplied")
    if not records:
        raise EncodeError(
            "can_header.count is zero, but t_base is the bus-arrival time of "
            "record 0; a quiet bus is reported by sending nothing")
    if records[0].get("dt", 0) != 0:
        raise EncodeError(
            f"can_record[0].dt is {records[0]['dt']}; t_base is record 0's "
            f"arrival time, so its offset from t_base is zero (SPEC.md §6.1)")
    out = bytearray(_pack("can_header", _zero_reserved("can_header", header)))
    for r in records:
        # An encoder must not emit a frame its own decoder rejects. SPEC.md
        # §6.4 and §6.10.
        if not r.get("extended") and r["id"] > 0x7FF:
            raise EncodeError(
                f"can_record.id is {r['id']:#x}, but a standard frame's "
                f"identifier is eleven bits")
        if r.get("fd") and r.get("rtr"):
            raise EncodeError("can_record: CAN FD has no remote frames")
        if r.get("rtr") and r["len"]:
            raise EncodeError("can_record: a remote frame carries no payload")
        if not r.get("fd") and r["len"] > 8:
            raise EncodeError(
                f"can_record.len is {r['len']}; a Classic frame carries 0..8")
        if r.get("fd") and r["len"] not in FD_LENGTHS:
            raise EncodeError(
                f"can_record.len is {r['len']}, which no CAN FD DLC can "
                f"express; the ladder is {sorted(FD_LENGTHS)}")
        # An identifier outside the arbitration field is not an identifier to
        # be trimmed to fit. Masking turned 0x3FFFFFFF into 0x1FFFFFFF and -1
        # into 0x1FFFFFFF as well -- two different requests silently becoming
        # one frame the caller never asked for, on the field a client uses to
        # decide what the bytes mean.
        if isinstance(r["id"], bool) or not isinstance(r["id"], int):
            raise EncodeError(f"can_record.id must be an integer, got {r['id']!r}")
        if not 0 <= r["id"] <= CAN_ID_MASK:
            raise EncodeError(
                f"can_record.id is {r['id']}, outside the 29-bit arbitration "
                f"field; the format is carried by `extended`, not by high bits")
        payload = _payload_bytes(r)
        raw = r["id"]
        if r.get("extended"):
            raw |= CAN_EXTENDED
        if r.get("fd"):
            raw |= CAN_FD
        if r.get("rtr"):
            raw |= CAN_RTR
        out += _pack("can_record", {"dt": r["dt"], "id": raw, "len": r["len"]})
        out += payload
    return bytes(out)


def encode_imu_batch(header, samples):
    """SPEC.md §7. Samples are evenly spaced, so only the header is stamped."""
    if len(samples) != header.get("count", 0):
        raise EncodeError(
            f"imu_header.count is {header.get('count', 0)} but "
            f"{len(samples)} sample(s) were supplied")
    # An encoder must not emit what its own decoder rejects. SPEC.md §7.
    if not header.get("period"):
        raise EncodeError(
            "imu_header.period is zero, which says every sample was taken at "
            "the same instant")
    if not header.get("count"):
        raise EncodeError(
            "imu_header.count is zero, but t_base is the acquisition time of "
            "sample 0; a device with nothing to report sends nothing")
    flags = header.get("flags", 0)
    accel = bool(flags & IMU_HAS_ACCEL)
    gyro = bool(flags & IMU_HAS_GYRO)

    out = bytearray(_pack("imu_header", _zero_reserved("imu_header", header)))
    for s in samples:
        # A cleared presence flag means the sensor is absent, so its triple is
        # zero on the wire whatever the caller supplied.
        gated = dict(s)
        if not accel:
            gated.update(ax=0, ay=0, az=0)
        if not gyro:
            gated.update(gx=0, gy=0, gz=0)
        out += _pack("imu_sample", gated)
    return bytes(out)


CAP_BIT = {b["name"]: b["bit"]
           for b in SCHEMA["bitmasks"]["capabilities"]["bits"]}
CAP_IMPLIES = {b["name"]: b.get("implies") or []
               for b in SCHEMA["bitmasks"]["capabilities"]["bits"]}
CAP_CAPACITY = SCHEMA["profile"]["capacity"]
CAP_CAPACITY_REQUIRED = SCHEMA["profile"].get("capacity_required", {})


def encode_info(info):
    """SPEC.md §4. No field is gated; a capacity of zero means none.

    SPEC.md §4.1 is enforced here, because an encoder must not emit what its
    own decoder rejects — and the profile matrix is now something the decoder
    rejects. Checked against the NORMALISED capability word, since that is what
    reaches the wire: a reserved bit cannot satisfy an implication it was never
    allowed to be set for.
    """
    caps = info.get("capabilities", 0) & _known_bits("capabilities")
    for name, implies in CAP_IMPLIES.items():
        if not caps & (1 << CAP_BIT[name]):
            continue
        for req in implies:
            if not caps & (1 << CAP_BIT[req]):
                raise EncodeError(
                    f"info.capabilities sets `{name}` without `{req}`, which "
                    f"SPEC.md §4.1 requires it to imply")
    for cap, fields in CAP_CAPACITY.items():
        if caps & (1 << CAP_BIT[cap]):
            continue
        for field in fields:
            if info.get(field):
                raise EncodeError(
                    f"info.{field} is {info[field]} while capability `{cap}` "
                    f"is clear; SPEC.md §4.1 requires a capacity behind a "
                    f"cleared bit to be zero")
    # SPEC.md §15 -- and the OBD pair MUST be non-zero while the bit is SET:
    # a declared role no conforming exchange can use. Driven by the schema's
    # capacity_required table, exactly as the rule above is by capacity.
    for cap, fields in CAP_CAPACITY_REQUIRED.items():
        if not caps & (1 << CAP_BIT[cap]):
            continue
        for field in fields:
            if not info.get(field):
                raise EncodeError(
                    f"info.{field} is 0 while capability `{cap}` is set; "
                    f"SPEC.md §15 requires it non-zero -- the declared role "
                    f"admits no conforming exchange")
    return _pack("info", info)


MONITOR_MAX_CHANNELS = (
    (SCHEMA["protocol"]["min_att_mtu"] - 3
     - SCHEMA["records"]["monitor_header"]["size"])
    // SCHEMA["records"]["monitor_value"]["size"])


def encode_monitor_list(declaration, entries):
    """SPEC.md §13.3 — the whole declaration, never a page of it."""
    if len(entries) != declaration.get("count", 0):
        raise EncodeError(
            f"monitor_declaration.count is {declaration.get('count', 0)} but "
            f"{len(entries)} entr(ies) were supplied")
    # SPEC.md §13.3, §13.4 — both already enforced by the decoder, so emitting
    # either produced a declaration this repository's own reader refuses.
    slots = [e["slot"] for e in entries]
    if len(set(slots)) != len(slots):
        raise EncodeError(
            "monitor_list: a slot appears twice, so every later update naming "
            "it would be ambiguous")
    if declaration.get("count", 0) > MONITOR_MAX_CHANNELS:
        raise EncodeError(
            f"monitor_declaration.count is {declaration.get('count')}, more "
            f"than the {MONITOR_MAX_CHANNELS} that fit in one complete write "
            f"at the minimum ATT MTU")
    # SPEC.md §13.5 — every declared channel carries a deadline.
    for e in entries:
        if not e.get("max_age"):
            raise EncodeError(
                f"monitor_channel slot {e.get('slot')} declares max_age 0; "
                f"a channel with no deadline is a value that can be displayed "
                f"forever after the client stopped sending it")
    out = bytearray(_pack("monitor_declaration",
                          _zero_reserved("monitor_declaration", declaration)))
    for e in entries:
        out += _pack("monitor_channel", _zero_reserved("monitor_channel", e))
    return bytes(out)


def encode_monitor_update(header, values):
    """SPEC.md §13.4. A value whose present bit is clear is written as zero."""
    if len(values) != header.get("count", 0):
        raise EncodeError(
            f"monitor_header.count is {header.get('count', 0)} but "
            f"{len(values)} value(s) were supplied")
    if not values:
        raise EncodeError(
            "monitor_update carries no values; SPEC.md §13.4 makes a write a "
            "complete statement of what the client can supply, and a client "
            "with nothing to supply clears the present bit on every slot")
    slots = [v["slot"] for v in values]
    if len(set(slots)) != len(slots):
        raise EncodeError(
            "monitor_update: a slot appears twice and nothing says which wins")
    out = bytearray(_pack("monitor_header", _zero_reserved("monitor_header", header)))
    for v in values:
        out += _pack("monitor_value", _gate("monitor_value", v))
    return bytes(out)


def encode_control_response(resp):
    """SPEC.md §9. Detail is emitted only when status is `ok`."""
    detail = bytes.fromhex(resp.get("detail_hex", ""))
    if detail and resp.get("status", 0) != 0:
        raise EncodeError(
            "control_response: detail is present only when status is ok")
    return _pack("control_response", resp) + detail


def encode_time_sync(ts):
    """SPEC.md §9.5. An encoder must not emit what its own decoder rejects."""
    if ts.get("t_device_tx", 0) < ts.get("t_device_rx", 0):
        raise EncodeError(
            "time_sync: t_device_tx precedes t_device_rx, so the device "
            "answered before it was asked")
    return _pack("time_sync", ts)


POWER_BIT = {b["name"]: 1 << b["bit"]
             for b in SCHEMA["bitmasks"]["power_validity"]["bits"]}


def encode_power_state(power):
    """SPEC.md §9.7. The detail of a GET_POWER response.

    A device MUST NOT emit a percent above 100, and the encoder is the device
    side of that rule -- checked only where the validity bit claims the byte
    means something, since a cleared bit is written as zero and zero is always
    in range. The decoder deliberately does NOT reject it: the record is well
    formed, so a receiver decodes it and SHOULD flag the value instead.
    """
    if (power.get("validity", 0) & POWER_BIT["percent"]
            and power.get("percent", 0) > 100):
        raise EncodeError(
            f"power_state.percent is {power['percent']}; the field is 0..100 "
            f"and a device MUST NOT emit a larger value (SPEC.md 9.7)")
    return _pack("power_state", _gate("power_state", power))


def encode_gnss_aid_caps(caps):
    """SPEC.md §14.2. The detail of a GNSS_AID_INFO response."""
    return _pack("gnss_aid_caps", _gate("gnss_aid_caps", caps))


def encode_aid_begin_result(begin):
    """SPEC.md §14.3. The detail of a GNSS_AID_BEGIN response."""
    # §14.3 -- MUST NOT be zero. A zero chunk size is a transfer that cannot
    # carry a byte, and the client has no way to tell it from a device that
    # simply will not say: it would write chunks of nothing until the commit
    # reported everything missing.
    if not begin.get("chunk_bytes"):
        raise EncodeError("aid_begin_result.chunk_bytes MUST NOT be zero "
                          "(SPEC.md 14.3)")
    return _pack("aid_begin_result", begin)


def encode_aid_commit_result(commit):
    """SPEC.md §14.4. The detail of a GNSS_AID_COMMIT response."""
    # §14.4 -- the first_missing bit is set if and only if the result is
    # `incomplete`. Set beside any other result it names a chunk as lost from a
    # transfer that did not lose one; clear beside `incomplete` it says
    # something is missing and refuses to say what, which is the one thing that
    # makes a write-without-response path recoverable.
    #
    # The enum VALUE is deliberately not checked: SPEC.md 11.4 lets a minor
    # version add results, and the corpus carries an unknown one on purpose.
    incomplete = next(m["value"] for m in SCHEMA["enums"]["aid_result"]["members"]
                      if m["name"] == "incomplete")
    bit = 1 << next(b["bit"] for b in SCHEMA["bitmasks"]["commit_validity"]["bits"]
                    if b["name"] == "first_missing")
    named = bool(_known_bits("commit_validity") & commit.get("validity", 0) & bit)
    if named != (commit.get("result") == incomplete):
        raise EncodeError(
            f"aid_commit_result: first_missing is "
            f"{'set' if named else 'clear'} beside result "
            f"{commit.get('result')}; SPEC.md 14.4 sets it if and only if the "
            f"result is incomplete ({incomplete})")
    return _pack("aid_commit_result", _gate("aid_commit_result", commit))


OBD_RESPONDED = next(1 << b["bit"]
                     for b in SCHEMA["bitmasks"]["obd_validity"]["bits"]
                     if b["name"] == "responded")
OBD_TRUNCATED = next(1 << b["bit"]
                     for b in SCHEMA["bitmasks"]["obd_validity"]["bits"]
                     if b["name"] == "truncated")
OBD_EXTENDED = 1 << 29
OBD_MAX_ECUS = 8               # SPEC.md 15.2 -- the most one record names


def _check_obd_identifier(field, raw):
    """SPEC.md §15.2 — bits 0-28 arbitration, b29 format, b30-31 zero.

    Refused, never masked, for §6.4's reason: masking produces a different
    identifier that looks entirely valid, on the field whose whole use is to
    become a CAN_SUBSCRIBE id."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise EncodeError(f"{field} must be an integer, got {raw!r}")
    if not 0 <= raw < (1 << 30):
        raise EncodeError(
            f"{field} is {raw:#x}; bits 30-31 of an OBD identifier MUST be "
            f"zero (SPEC.md 15.2)")
    if not raw & OBD_EXTENDED and (raw & 0x1FFFFFFF) > 0x7FF:
        raise EncodeError(
            f"{field} is {raw:#x}, a standard-format identifier above eleven "
            f"bits (SPEC.md 15.2 via 6.4)")


def encode_obd_info(probe, ecus):
    """SPEC.md §15.2 — the detail of an OBD_INFO response.

    The decoder deliberately accepts what most of this refuses: count
    disagreeing with `responded`, duplicate or unordered entries, more than
    eight of them. Those are content rules — the device MUST NOT emit them,
    a receiver decodes and flags them — so the refusals live here, on the
    device side, and conformance/encoders.json holds each one."""
    if len(ecus) != probe.get("count", 0):
        raise EncodeError(
            f"obd_probe.count is {probe.get('count', 0)} but {len(ecus)} "
            f"entr(ies) were supplied")
    responded = bool(_known_bits("obd_validity")
                     & probe.get("validity", 0) & OBD_RESPONDED)
    if responded and not ecus:
        raise EncodeError(
            "obd_probe: `responded` set with no entries says something "
            "answered and lists nothing that did (SPEC.md 15.2)")
    if ecus and not responded:
        raise EncodeError(
            "obd_probe: an ECU is listed on a probe that says nothing "
            "answered (SPEC.md 15.2)")
    if len(ecus) > OBD_MAX_ECUS:
        raise EncodeError(
            f"obd_probe.count is {len(ecus)}; the record names at most "
            f"{OBD_MAX_ECUS} ECUs, and a probe more than that answered "
            f"reports the {OBD_MAX_ECUS} lowest with `truncated` set "
            f"(SPEC.md 15.2)")
    truncated = bool(_known_bits("obd_validity")
                     & probe.get("validity", 0) & OBD_TRUNCATED)
    if truncated and not responded:
        raise EncodeError(
            "obd_probe: `truncated` set with `responded` clear says "
            "responders were dropped from a probe nothing answered "
            "(SPEC.md 15.2)")
    if truncated and len(ecus) != OBD_MAX_ECUS:
        raise EncodeError(
            f"obd_probe: `truncated` set with {len(ecus)} entr(ies); a "
            f"device that dropped a responder while naming fewer than "
            f"{OBD_MAX_ECUS} dropped one it had room for (SPEC.md 15.2)")
    # §15.2 -- the identifier rule is scoped to a probe that answered: with
    # `responded` clear the field is gated to zero on the wire below, so a
    # stale invalid value in the caller's struct is normalised away, exactly
    # as the decoder tolerates it.
    if responded:
        _check_obd_identifier("obd_probe.request_id",
                              probe.get("request_id", 0))
    prev = None
    for e in ecus:
        _check_obd_identifier("obd_ecu.id", e.get("id", 0))
        # Strictly ascending over bits 0-29; bits 30-31 are already zero, so
        # the raw comparison is the identity comparison.
        if prev is not None and e["id"] <= prev:
            raise EncodeError(
                f"obd_ecu entries are not strictly ascending: {e['id']:#x} "
                f"follows {prev:#x} (SPEC.md 15.2)")
        prev = e["id"]
    out = bytearray(_pack("obd_probe", _gate("obd_probe", probe)))
    for e in ecus:
        out += _pack("obd_ecu", e)
    return bytes(out)


# Keyed by the runner-contract record name, so a harness can round-trip a
# decode without knowing which record it holds.
ENCODERS = {
    "gps_fix": lambda d: encode_gps_fix(d, bytes.fromhex(d.get("ext_hex", ""))),
    "can_batch": lambda d: encode_can_batch(d["header"], d["records"]),
    "imu_batch": lambda d: encode_imu_batch(d["header"], d["samples"]),
    "info": encode_info,
    "power_state": encode_power_state,
    "monitor_list": lambda d: encode_monitor_list(d["declaration"], d["entries"]),
    "monitor_update": lambda d: encode_monitor_update(d["header"], d["values"]),
    "control_response": encode_control_response,
    "time_sync": encode_time_sync,
    "gnss_aid_caps": encode_gnss_aid_caps,
    "aid_begin_result": encode_aid_begin_result,
    "aid_commit_result": encode_aid_commit_result,
    "obd_info": lambda d: encode_obd_info(d["probe"], d["ecus"]),
}
