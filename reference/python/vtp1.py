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

    # SPEC.md §5 and §5.3 bind the DEVICE: it must not emit an out-of-range
    # coordinate or contradictory RTK flags, and the reference ENCODER refuses
    # both (vtp1_encode.py). A receiver decodes the well-formed payload as it
    # stands — flagging the violation to the user is an application concern,
    # not a decode outcome.
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
    # SPEC.md §6.2 — t_base IS the bus-arrival time of record 0, so a batch
    # with no record 0 carries a timestamp naming a frame that does not exist.
    # A quiet bus is reported by sending nothing.
    if hdr["count"] == 0:
        raise Reject("empty-batch")

    rsz = _size("can_record")
    off, records = hsz, []
    for _ in range(hdr["count"]):
        if off + rsz > len(buf):
            raise Reject("truncated-record")
        r = _unpack("can_record", buf, off)
        if off + rsz + r["len"] > len(buf):
            raise Reject("truncated-record")
        # SPEC.md §6.1 — t_base IS record 0's arrival time, so its dt is zero by
        # definition. A non-zero one means the sender and the receiver disagree
        # about what t_base is, and the receiver cannot tell which reading to
        # trust.
        if not records and r["dt"] != 0:
            raise Reject("first-dt-nonzero")
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


# SPEC.md §7.2 — imu_header.flags bit 2.
IMU_SATURATED = 0x04


def decode_imu_batch(buf):
    hsz, ssz = _size("imu_header"), _size("imu_sample")
    if len(buf) < hsz:
        raise Reject("length")
    hdr = _unpack("imu_header", buf)
    if len(buf) != hsz + hdr["count"] * ssz:
        raise Reject("length")
    # SPEC.md §7 — zero says every sample was taken at one instant, which
    # describes no measurement, and a client recovering a rate divides by it.
    if hdr["period"] == 0:
        raise Reject("period-zero")
    # SPEC.md §7 — t_base is the acquisition time of sample 0, so a batch with
    # no sample 0 timestamps one that does not exist. §6.2's CAN batch says the
    # same of record 0, for the same reason.
    if hdr["count"] == 0:
        raise Reject("empty-batch")
    # SPEC.md §7.2 — "at least this much" is not "this much".
    hdr["saturated"] = bool(hdr["flags"] & IMU_SATURATED)

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


# SPEC.md §4.1 — the profile matrix, read from the schema rather than restated.
CAP_BIT = {b["name"]: b["bit"]
           for b in SCHEMA["bitmasks"]["capabilities"]["bits"]}
CAP_IMPLIES = {b["name"]: b.get("implies") or []
               for b in SCHEMA["bitmasks"]["capabilities"]["bits"]}
CAP_CAPACITY = SCHEMA["profile"]["capacity"]


def capability_problem(info):
    """The first way `info` breaks SPEC.md §4.1, or None.

    Not a decode outcome: an Info that breaks the matrix is well-formed and
    decodes, but the device that published it is non-conforming, a client must
    not use the role or capacity it contradicts, and SPEC.md §4.1 asks the
    client to surface it. This helper is what a client (or the harness) calls
    to do that.

    Reserved bits take no part: §2 says to ignore them on receive, and a bit
    from a minor version this build has never heard of is exactly what they are
    for. Only the implications of bits this build knows are checked.
    """
    caps = info["capabilities"]
    for name, implies in CAP_IMPLIES.items():
        if not caps & (1 << CAP_BIT[name]):
            continue
        for req in implies:
            if not caps & (1 << CAP_BIT[req]):
                return (f"capabilities: `{name}` requires `{req}`, which is "
                        f"clear; SPEC.md §4.1")
    for cap, fields in CAP_CAPACITY.items():
        if caps & (1 << CAP_BIT[cap]):
            continue
        for field in fields:
            # A capacity of zero means "none" (§4). A non-zero one behind a
            # cleared capability bit is a device publishing a role it does not
            # have, and a client sizing a buffer from it has been told
            # something false.
            if info.get(field):
                return (f"capabilities: {field} is {info[field]} while `{cap}` "
                        f"is clear; SPEC.md §4.1")
    return None


def decode_info(buf):
    if len(buf) != _size("info"):
        raise Reject("length")
    return _unpack("info", buf)


# SPEC.md §13.4 — the most channels a device may ask for: as many values as fit
# beside a monitor_header in one write at the §2 minimum ATT MTU, less the
# 3-byte ATT write header. Derived so the constant cannot drift from the record
# sizes it depends on.
MONITOR_MAX_CHANNELS = (
    (SCHEMA["protocol"]["min_att_mtu"] - 3 - _size("monitor_header"))
    // _size("monitor_value"))


def decode_monitor_list(buf):
    """SPEC.md §13.3 — every channel a device asks for, in one response."""
    hsz, esz = _size("monitor_declaration"), _size("monitor_channel")
    if len(buf) < hsz:
        raise Reject("length")
    declaration = _unpack("monitor_declaration", buf)
    if len(buf) < hsz + declaration["count"] * esz:
        raise Reject("truncated-record")
    if len(buf) != hsz + declaration["count"] * esz:
        raise Reject("length")

    known = {m["value"] for m in SCHEMA["enums"]["channel"]["members"]}
    entries = []
    for i in range(declaration["count"]):
        e = _unpack("monitor_channel", buf, hsz + i * esz)
        # SPEC.md §13.2 — a channel from a later minor stays unknown, and the
        # client answers the slot absent rather than substituting another.
        e["channel_known"] = e["channel"] in known
        entries.append(e)

    # SPEC.md §13.3 — a device MUST NOT repeat a slot; the slot is how a value
    # is addressed, so two entries claiming one make every update ambiguous.
    slots = [e["slot"] for e in entries]
    if len(set(slots)) != len(slots):
        raise Reject("duplicate-slot")
    # SPEC.md §13.4 — a complete write must fit at the minimum ATT MTU, so a
    # declaration larger than that has made its own rule unsatisfiable. `count`
    # IS the whole declaration now that it is not paged, so it is the only
    # number there is to check.
    if declaration["count"] > MONITOR_MAX_CHANNELS:
        raise Reject("too-many-channels")
    # SPEC.md §13.5 — every declared channel carries a deadline, so a value the
    # client stops refreshing always stops being shown. Zero used to mean "no
    # deadline of its own", reconciled by a derived device-wide liveness bound;
    # one rule per channel replaced both.
    if any(e["max_age"] == 0 for e in entries):
        raise Reject("zero-max-age")
    return {"declaration": declaration, "entries": entries}


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
    # SPEC.md §13.4 — a write is a COMPLETE statement of what the client can
    # supply, and one naming no slots is the one thing a complete statement
    # cannot be: on a device that asked for channels it names none of them,
    # leaving every previous value standing. A client with nothing to supply
    # writes every slot with the present bit clear; a client with nothing to
    # say does not write at all.
    if hdr["count"] == 0:
        raise Reject("empty-update")

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

    # SPEC.md §13.4 — nothing says which of two values for one slot wins, so a
    # device choosing either is choosing on every client's behalf.
    slots = [v["slot"] for v in values]
    if len(set(slots)) != len(slots):
        raise Reject("duplicate-slot")
    return {"header": hdr, "values": values}


def decode_control_response(buf):
    """SPEC.md §9 — `[opcode][tag][status]` and, only when status is `ok`, a
    detail whose shape the opcode decides.

    The conditional detail is the whole rule. A refused request is answered
    with exactly three bytes, so a client that reads a fixed-width response
    takes a well-formed detail of zeroes from a request that failed. Rejecting
    the surplus is what stops that reaching an application that has already
    decided the request succeeded.
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
    """SPEC.md §9.5 — the detail of a TIME_SYNC response.

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
    # it: the device's own processing time is the term §9.5 takes out of the
    # round trip.
    ts["processing_us"] = ts["t_device_tx"] - ts["t_device_rx"]
    return ts


DECODERS = {
    "gps_fix": decode_gps_fix,
    "can_batch": decode_can_batch,
    "imu_batch": decode_imu_batch,
    "info": decode_info,
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
