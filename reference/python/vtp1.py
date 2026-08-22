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

from vtp1_encode import ENCODERS, EncodeError

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = yaml.safe_load((ROOT / "schema" / "vtp1.yaml").read_text())

PACK = {"u8": "B", "i8": "b", "u16": "H", "i16": "h",
        "u32": "I", "i32": "i", "u64": "Q", "i64": "q"}

# SPEC.md §8 — derived timestamps are computed modulo 2^64. Python's integers
# are arbitrary-precision, so without this the two reference decoders disagree
# on any payload whose t_base is near the top of the range: C wraps and Python
# does not. Neither is wrong on its own; the specification now says which.
U64 = (1 << 64) - 1


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
    # Kept so a round-trip can append them verbatim: their content is opaque to
    # this decoder by design (SPEC.md §5.5) but it is not free to discard them.
    fix["ext_hex"] = buf[_size("gps_fix"):].hex()

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


# SPEC.md §6.10 -- the payload lengths a CAN FD DLC can express. Above eight
# they are a fixed ladder, so 9, 10 and 11 are impossible lengths rather than
# short ones.
FD_LENGTHS = frozenset((0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64))


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
        if off + rsz + r["len"] > len(buf):
            raise Reject("truncated-record")
        raw = r["id"]
        extended = bool(raw & (1 << 29))
        fd, rtr = bool(raw & (1 << 30)), bool(raw & (1 << 31))
        # SPEC.md §6.4 — each of these describes a frame that cannot exist, and
        # is rejected rather than repaired: a repaired frame is a plausible
        # wrong value carrying a correct-looking identifier.
        if not extended and (raw & 0x1FFFFFFF) > 0x7FF:
            raise Reject("bad-standard-id")
        if fd and rtr:
            raise Reject("fd-rtr")
        if rtr and r["len"]:
            raise Reject("rtr-with-payload")
        # SPEC.md §6.10 -- a length no bus can carry means the reader and the
        # writer disagree about where this record ends, so every byte after it
        # is suspect, including the next frame's identifier. These subsume a
        # plain 0..64 bound: Classic stops at 8, the FD ladder at 64, RTR at 0.
        # A redundant check is worse than none, because it can be deleted
        # without any vector noticing.
        if not fd and r["len"] > 8:
            raise Reject("classic-length")
        if fd and r["len"] not in FD_LENGTHS:
            raise Reject("fd-length")
        records.append({
            "dt": r["dt"],
            "id": raw & 0x1FFFFFFF,
            "extended": extended,
            "fd": fd,
            "rtr": rtr,
            "len": r["len"],
            "payload": buf[off + rsz: off + rsz + r["len"]].hex(),
            # dt counts 10 us ticks — SPEC.md §6.1.
            "t_device_us": (hdr["t_base"] + r["dt"] * 10) & U64,
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

    # SPEC.md §7 — a sensor group whose presence flag is clear is ABSENT, not a
    # measurement of zero. Derived from the schema's `presence` declaration, so
    # a group added in a later minor is covered without editing this.
    rec = SCHEMA["records"]["imu_sample"]
    pres = rec["presence"]
    absent = sorted(f["name"] for f in rec["fields"]
                    if f.get("presence_bit") is not None
                    and not (hdr[pres["field"]] & (1 << pres["bits"][f["presence_bit"]])))

    samples = []
    for i in range(hdr["count"]):
        s = _unpack("imu_sample", buf, hsz + i * ssz)
        s["t_device_us"] = (hdr["t_base"] + i * hdr["period"]) & U64
        s["absent"] = absent
        samples.append(s)
    return {"header": hdr, "samples": samples}


def decode_info(buf):
    if len(buf) != _size("info"):
        raise Reject("length")
    return _unpack("info", buf)


def decode_can_list(buf):
    """SPEC.md §9.5 — one page of the CAN subscription table."""
    hsz = _size("can_list_page")
    if len(buf) < hsz:
        raise Reject("length")
    page = _unpack("can_list_page", buf)

    esz = _size("can_subscription")
    if len(buf) < hsz + page["count"] * esz:
        raise Reject("truncated-record")
    if len(buf) != hsz + page["count"] * esz:
        raise Reject("length")

    known = {m["value"] for m in SCHEMA["enums"]["sub_mode"]["members"]}
    entries = []
    for i in range(page["count"]):
        e = _unpack("can_subscription", buf, hsz + i * esz)
        # SPEC.md §11.4 — a mode from a later minor stays unknown. Reading it
        # as every_frame would silently misreport what the device is doing.
        e["mode_known"] = e["mode"] in known
        entries.append(e)
    return {"page": page, "entries": entries}


def decode_monitor_list(buf):
    """SPEC.md §13.3 — one page of the channels a device asks for."""
    hsz, esz = _size("monitor_page"), _size("monitor_channel")
    if len(buf) < hsz:
        raise Reject("length")
    page = _unpack("monitor_page", buf)
    if len(buf) < hsz + page["count"] * esz:
        raise Reject("truncated-record")
    if len(buf) != hsz + page["count"] * esz:
        raise Reject("length")

    known = {m["value"] for m in SCHEMA["enums"]["channel"]["members"]}
    entries = []
    for i in range(page["count"]):
        e = _unpack("monitor_channel", buf, hsz + i * esz)
        # SPEC.md §13.2 — a channel from a later minor stays unknown, and the
        # client answers the slot absent rather than substituting another.
        e["channel_known"] = e["channel"] in known
        entries.append(e)
    return {"page": page, "entries": entries}


def decode_monitor_update(buf):
    """SPEC.md §13.4 — a client-to-device batch of values."""
    hsz, esz = _size("monitor_header"), _size("monitor_value")
    if len(buf) < hsz:
        raise Reject("length")
    hdr = _unpack("monitor_header", buf)
    if len(buf) < hsz + hdr["count"] * esz:
        raise Reject("truncated-record")
    if len(buf) != hsz + hdr["count"] * esz:
        raise Reject("length")

    bit = {b["name"]: b["bit"]
           for b in SCHEMA["bitmasks"]["monitor_validity"]["bits"]}
    values = []
    for i in range(hdr["count"]):
        v = _unpack("monitor_value", buf, hsz + i * esz)
        # The one place the protocol reverses direction, and §1.1 still holds:
        # a cleared present bit means the client cannot supply the channel, not
        # that the channel is zero.
        v["absent"] = ([] if v["validity"] & (1 << bit["present"]) else ["value"])
        values.append(v)
    return {"header": hdr, "values": values}


def decode_control_response(buf):
    """SPEC.md §9 — `[opcode][tag][status]` and, only when status is `ok`, a
    detail whose shape the opcode decides.

    The conditional detail is the whole rule. A refused request is answered
    with exactly three bytes, so a client that reads a fixed-width response
    takes a well-formed handle 0 -- or a link_params of all zeroes -- from a
    request that failed. Rejecting the surplus is what stops that reaching an
    application that has already decided the request succeeded.
    """
    base = _size("control_response")
    if len(buf) < base:
        raise Reject("length")
    resp = _unpack("control_response", buf)

    known = {m["value"] for m in SCHEMA["enums"]["status"]["members"]}
    resp["status_known"] = resp["status"] in known
    ok = resp["status"] == 0

    detail = buf[base:]
    if detail and not ok:
        raise Reject("detail-on-error")
    # Opaque by design: §11.3 lets a minor version add opcodes with any
    # payload, so the envelope decoder carries the detail rather than parsing
    # it. Kept verbatim so a round-trip can re-emit it.
    resp["detail_hex"] = detail.hex()
    return resp


def decode_time_sync(buf):
    """SPEC.md §9.7 — the detail of a TIME_SYNC response.

    Two readings of one clock. `t_device_tx` before `t_device_rx` would mean
    the device finished answering before the question arrived, so it is
    rejected rather than reported: a client that computed a delay from it
    would get a negative round trip and, halved into an offset, a confidently
    wrong clock.
    """
    if len(buf) != _size("time_sync"):
        raise Reject("length")
    ts = _unpack("time_sync", buf)
    if ts["t_device_tx"] < ts["t_device_rx"]:
        raise Reject("tx-before-rx")
    # Reported so a client need not recompute it, and so the corpus can check
    # it: the device's own processing time is the term §9.7 takes out of the
    # round trip.
    ts["processing_us"] = ts["t_device_tx"] - ts["t_device_rx"]
    return ts


def decode_link_params(buf):
    """SPEC.md §9.1 — the detail of a GET_LINK_PARAMS response.

    Fixed size with no extension mechanism, so any other length is rejected.
    """
    if len(buf) != _size("link_params"):
        raise Reject("length")
    lp = _unpack("link_params", buf)

    # Same rule as gps_fix, derived the same way: absence is the bitmask's job.
    # A cleared phy bit means the controller did not report a PHY, which is not
    # the same as LE 1M -- and is why the phy enum has no zero member.
    bit_of = {b["name"]: b["bit"]
              for b in SCHEMA["bitmasks"]["link_validity"]["bits"]}
    lp["absent"] = sorted(
        f["name"] for f in SCHEMA["records"]["link_params"]["fields"]
        if f.get("valid_bit") is not None
        and not (lp["validity"] & (1 << bit_of[f["valid_bit"]])))

    known = {m["value"] for m in SCHEMA["enums"]["phy"]["members"]}
    lp["phy_tx_known"] = lp["phy_tx"] in known
    lp["phy_rx_known"] = lp["phy_rx"] in known
    return lp


DECODERS = {
    "gps_fix": decode_gps_fix,
    "can_batch": decode_can_batch,
    "imu_batch": decode_imu_batch,
    "info": decode_info,
    "link_params": decode_link_params,
    "can_list": decode_can_list,
    "monitor_list": decode_monitor_list,
    "monitor_update": decode_monitor_update,
    "control_response": decode_control_response,
    "time_sync": decode_time_sync,
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
        # The runner requires this to equal the input byte for byte, or -- for
        # a deliberately non-canonical vector -- to equal the normalised form.
        # That checks what no decode can: that the encoder agrees about the
        # layout and emits the canonical payload rather than merely one that
        # happens to decode back.
        try:
            result["roundtrip_hex"] = ENCODERS[record](result).hex()
        except EncodeError as exc:
            result["roundtrip_error"] = str(exc)
        print(json.dumps({"ok": True, **result}), flush=True)


if __name__ == "__main__":
    main()
