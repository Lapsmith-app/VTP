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


def _pack(name, values):
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
    # `reserved` is written through rather than forced to zero: a device built
    # against a later minor may have been assigned those bytes, and an encoder
    # must not silently erase a field it does not know about.
    if records and records[0].get("dt", 0) != 0:
        raise EncodeError(
            f"can_record[0].dt is {records[0]['dt']}; t_base is record 0's "
            f"arrival time, so its offset from t_base is zero (SPEC.md §6.1)")
    out = bytearray(_pack("can_header", header))
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
        payload = _payload_bytes(r)
        raw = r["id"] & CAN_ID_MASK
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
    flags = header.get("flags", 0)
    accel = bool(flags & IMU_HAS_ACCEL)
    gyro = bool(flags & IMU_HAS_GYRO)

    out = bytearray(_pack("imu_header", header))
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


def encode_info(info):
    """SPEC.md §4. No field is gated; a capacity of zero means none."""
    return _pack("info", info)


def encode_can_list(page, entries):
    """SPEC.md §9.5. One page header followed by `count` subscription entries."""
    if len(entries) != page.get("count", 0):
        raise EncodeError(
            f"can_list_page.count is {page.get('count', 0)} but "
            f"{len(entries)} entr(ies) were supplied")
    out = bytearray(_pack("can_list_page", page))
    for e in entries:
        out += _pack("can_subscription", e)
    return bytes(out)


def encode_monitor_list(page, entries):
    """SPEC.md §13.3."""
    if len(entries) != page.get("count", 0):
        raise EncodeError(
            f"monitor_page.count is {page.get('count', 0)} but "
            f"{len(entries)} entr(ies) were supplied")
    out = bytearray(_pack("monitor_page", page))
    for e in entries:
        out += _pack("monitor_channel", e)
    return bytes(out)


def encode_monitor_update(header, values):
    """SPEC.md §13.4. A value whose present bit is clear is written as zero."""
    if len(values) != header.get("count", 0):
        raise EncodeError(
            f"monitor_header.count is {header.get('count', 0)} but "
            f"{len(values)} value(s) were supplied")
    out = bytearray(_pack("monitor_header", header))
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
    """SPEC.md §9.7. An encoder must not emit what its own decoder rejects."""
    if ts.get("t_device_tx", 0) < ts.get("t_device_rx", 0):
        raise EncodeError(
            "time_sync: t_device_tx precedes t_device_rx, so the device "
            "answered before it was asked")
    return _pack("time_sync", ts)


def encode_link_params(link_params):
    """SPEC.md §9.1. The detail of a GET_LINK_PARAMS response."""
    return _pack("link_params", _gate("link_params", link_params))


# Keyed by the runner-contract record name, so a harness can round-trip a
# decode without knowing which record it holds.
ENCODERS = {
    "gps_fix": lambda d: encode_gps_fix(d, bytes.fromhex(d.get("ext_hex", ""))),
    "can_batch": lambda d: encode_can_batch(d["header"], d["records"]),
    "imu_batch": lambda d: encode_imu_batch(d["header"], d["samples"]),
    "info": encode_info,
    "link_params": encode_link_params,
    "can_list": lambda d: encode_can_list(d["page"], d["entries"]),
    "monitor_list": lambda d: encode_monitor_list(d["page"], d["entries"]),
    "monitor_update": lambda d: encode_monitor_update(d["header"], d["values"]),
    "control_response": encode_control_response,
    "time_sync": encode_time_sync,
}
