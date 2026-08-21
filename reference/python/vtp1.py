#!/usr/bin/env python3
"""VTP/1 reference decoder — Python, schema-driven.

Deliberately independent of the C reference: this reads schema/vtp1.yaml at
runtime and derives every offset, where the C decoder compiles generated
constants in. Agreement between the two across the conformance corpus therefore
also proves the schema and the generated C header are consistent.

Run as a script it implements the conformance runner contract
(see conformance/README.md).
"""
import json, pathlib, struct, sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = yaml.safe_load((ROOT / "schema" / "vtp1.yaml").read_text())

PACK = {"u8": "B", "i8": "b", "u16": "H", "i16": "h",
        "u32": "I", "i32": "i", "u64": "Q", "i64": "q"}


class Reject(Exception):
    """A payload that MUST NOT be decoded. SPEC.md §1.1."""


def _unpack(record, buf, base=0):
    rec = SCHEMA["records"][record]
    out = {}
    for f in rec["fields"]:
        (out[f["name"]],) = struct.unpack_from(
            "<" + PACK[f["type"]], buf, base + f["offset"])
    return out


def _size(record):
    return SCHEMA["records"][record]["size"]


def decode_gps_fix(buf):
    if len(buf) < _size("gps_fix"):
        raise Reject("length")
    fix = _unpack("gps_fix", buf)

    # SPEC.md §5.5 — length must equal base + exactly what ext_count declares.
    off = _size("gps_fix")
    for _ in range(fix["ext_count"]):
        if off + 2 > len(buf):
            raise Reject("ext-truncated")
        ext_len = buf[off + 1]
        if off + 2 + ext_len > len(buf):
            raise Reject("ext-truncated")
        off += 2 + ext_len
    if off != len(buf):
        raise Reject("length")

    known = {m["value"] for m in SCHEMA["enums"]["fix_type"]["members"]}
    fix["fix_type_known"] = fix["fix_type"] in known

    # SPEC.md 1.1: absence is the bitmask's job, never a field value. Reported
    # explicitly so the conformance runner can check it rather than take it on
    # trust — derived from the schema, so a field gaining a validity bit in a
    # later minor is covered without editing this.
    bit_of = {b["name"]: b["bit"]
              for b in SCHEMA["bitmasks"]["gps_validity"]["bits"]}
    fix["absent"] = sorted(
        f["name"] for f in SCHEMA["records"]["gps_fix"]["fields"]
        if f.get("valid_bit") is not None
        and not (fix["validity"] & (1 << bit_of[f["valid_bit"]])))
    return fix


def decode_can_batch(buf):
    hsz = _size("can_header")
    if len(buf) < hsz:
        raise Reject("length")
    hdr = _unpack("can_header", buf)

    rsz = _size("can_record")
    off, records = hsz, []
    for _ in range(hdr["count"]):
        if off + rsz > len(buf):
            raise Reject("truncated-record")
        r = _unpack("can_record", buf, off)
        if r["len"] > 64:
            raise Reject("bad-length")
        if off + rsz + r["len"] > len(buf):
            raise Reject("truncated-record")
        raw = r["id"]
        records.append({
            "dt": r["dt"],
            "id": raw & 0x1FFFFFFF,
            "extended": bool(raw & (1 << 29)),
            "fd": bool(raw & (1 << 30)),
            "rtr": bool(raw & (1 << 31)),
            "len": r["len"],
            "payload": buf[off + rsz: off + rsz + r["len"]].hex(),
            # dt counts 10 us ticks — SPEC.md §6.1.
            "t_device_us": hdr["t_base"] + r["dt"] * 10,
        })
        off += rsz + r["len"]
    if off != len(buf):
        raise Reject("length")
    return {"header": hdr, "records": records}


def decode_imu_batch(buf):
    hsz, ssz = _size("imu_header"), _size("imu_sample")
    if len(buf) < hsz:
        raise Reject("length")
    hdr = _unpack("imu_header", buf)
    if len(buf) != hsz + hdr["count"] * ssz:
        raise Reject("length")

    samples = []
    for i in range(hdr["count"]):
        s = _unpack("imu_sample", buf, hsz + i * ssz)
        s["t_device_us"] = hdr["t_base"] + i * hdr["period"]
        samples.append(s)
    return {"header": hdr, "samples": samples}


def decode_info(buf):
    if len(buf) != _size("info"):
        raise Reject("length")
    return _unpack("info", buf)


DECODERS = {
    "gps_fix": decode_gps_fix,
    "can_batch": decode_can_batch,
    "imu_batch": decode_imu_batch,
    "info": decode_info,
}


def main():
    for line in sys.stdin:
        if "\t" not in line:
            continue
        record, hexstr = line.rstrip("\n").split("\t", 1)
        try:
            fn = DECODERS.get(record)
            if fn is None:
                raise Reject("unknown-record")
            result = fn(bytes.fromhex(hexstr))
        except Reject as e:
            print(json.dumps({"ok": False, "reason": str(e)}), flush=True)
            continue
        except ValueError:
            print(json.dumps({"ok": False, "reason": "bad-hex"}), flush=True)
            continue
        print(json.dumps({"ok": True, **result}), flush=True)


if __name__ == "__main__":
    main()
