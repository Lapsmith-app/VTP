"""The bridge to the reference decoder, so the harness never restates a rule.

Every payload this harness sees is decoded by `reference/python/vtp1.py` — the
same implementation the conformance corpus tests, reading the same
`schema/vtp1.yaml`. A rule the decoder already enforces is therefore enforced
against live firmware for free, and the two cannot drift apart, because there is
only one of them.

What the harness adds on top is everything a decoder cannot see: what a device
did over time, and what it answered when asked.
"""
import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent


def _find_root():
    """The directory holding both `schema/` and `reference/python/`.

    Two layouts have to work. In a clone it is the repository root, some number
    of levels above this file. Installed, it is the `_ref/` directory the wheel
    build reproduces that same shape into (see pyproject.toml), which is what
    lets the decoder find its schema by its own relative path in either case.
    """
    for cand in (_HERE / "_ref", *_HERE.parents):
        if (cand / "schema" / "vtp1.yaml").is_file() and \
           (cand / "reference" / "python" / "vtp1.py").is_file():
            return cand
    raise RuntimeError(
        "cannot locate the VTP/1 schema and reference decoder. Run the harness "
        "from a clone of the specification repository, or install it with "
        "`pip install .` from the repository root.")


ROOT = _find_root()

sys.path.insert(0, str(ROOT / "reference" / "python"))
import vtp1 as _ref  # noqa: E402

DECODERS = _ref.DECODERS
Reject = _ref.Reject
SCHEMA = _ref.SCHEMA
UUIDS = json.loads((ROOT / "schema" / "uuids.json").read_text())

SERVICE_UUID = UUIDS["service"]["vtp1"].lower()
CHAR = {name: uuid.lower() for name, uuid in UUIDS["characteristics"].items()}
CHAR_NAME = {uuid: name for name, uuid in CHAR.items()}

# The Bluetooth SIG Device Information Service (SPEC.md §2, §3.4).
DIS_SERVICE = "0000180a-0000-1000-8000-00805f9b34fb"
DIS_CHARS = {
    "manufacturer_name": "00002a29-0000-1000-8000-00805f9b34fb",
    "model_number": "00002a24-0000-1000-8000-00805f9b34fb",
    "firmware_revision": "00002a26-0000-1000-8000-00805f9b34fb",
}

MIN_ATT_MTU = SCHEMA["protocol"]["min_att_mtu"]
PROTOCOL_MAJOR = SCHEMA["protocol"]["major"]
# One notification at the minimum MTU, less the three-byte ATT header.
MIN_NOTIFY_BYTES = MIN_ATT_MTU - 3


def decode(record, payload):
    """Decode `payload` as `record`, raising `Reject` exactly as the corpus does."""
    return DECODERS[record](bytes(payload))


def size(record):
    return SCHEMA["records"][record]["size"]


def offset(record, field):
    return next(f["offset"] for f in SCHEMA["records"][record]["fields"]
                if f["name"] == field)


def bit(bitmask, name):
    return next(b["bit"] for b in SCHEMA["bitmasks"][bitmask]["bits"]
                if b["name"] == name)


def bits(bitmask):
    """name -> bit, for every named bit in a bitmask."""
    return {b["name"]: b["bit"] for b in SCHEMA["bitmasks"][bitmask]["bits"]}


def reserved_mask(bitmask, width):
    """Every bit of a `width`-bit field that the schema does not name.

    Derived rather than written down, so a bit allocated in a later minor stops
    being flagged as reserved the moment the schema says it is not — SPEC.md
    Appendix A is a table of exactly this, and a hand-copied constant here would
    be the first thing to go stale.
    """
    named = 0
    for b in SCHEMA["bitmasks"][bitmask]["bits"]:
        named |= 1 << b["bit"]
    return ((1 << width) - 1) & ~named


def enum_values(enum):
    return {m["value"]: m["name"] for m in SCHEMA["enums"][enum]["members"]}


def enum_value(enum, name):
    return next(m["value"] for m in SCHEMA["enums"][enum]["members"]
                if m["name"] == name)


#: SPEC.md §4.1 -- the attribute table, the capability implications and the
#: capacity fields each bit governs, all generated from the same schema the
#: specification's own tables are. Consumed rather than restated: a hand-copy
#: here would be a third statement of a fact the repository has just finished
#: reducing to one.
PROFILE = SCHEMA["profile"]
PROFILE_CHARS = {c["name"]: c for c in PROFILE["characteristics"]}
CAPACITY_FIELDS = PROFILE["capacity"]
IMPLIES = {b["name"]: tuple(b.get("implies", ()))
           for b in SCHEMA["bitmasks"]["capabilities"]["bits"]}

OPCODE = {op["name"]: op["value"] for op in SCHEMA["control"]["opcodes"]}
OPCODE_NAME = {v: k for k, v in OPCODE.items()}
#: SPEC.md §9 -- every opcode is owned by a capability, and availability is
#: decided before parameters. None means the opcode belongs to the link or the
#: clock, which every device has.
OPCODE_CAPABILITY = {op["name"]: op.get("capability")
                     for op in SCHEMA["control"]["opcodes"]}
#: Derived from each opcode's `params`, not listed by hand. The hand-written
#: version reproduced these ten values correctly and had no way to acquire an
#: eleventh: an opcode added to the schema raised KeyError here, in a check
#: about capabilities, some distance from the omission. SPEC.md 11.3 makes new
#: opcodes the protocol's general-purpose extension point, so this table was
#: always going to be asked for one it did not hold.
_TYPE_BYTES = {"u8": 1, "i8": 1, "u16": 2, "i16": 2,
               "u32": 4, "i32": 4, "u64": 8, "i64": 8}


def _param_size(params):
    total = 0
    for part in (p.strip() for p in params.split(",")):
        if not part:
            continue
        total += _TYPE_BYTES[part.split(":")[1].strip()]
    return total


OPCODE_PARAM_SIZE = {op["name"]: _param_size(op.get("params") or "")
                     for op in SCHEMA["control"]["opcodes"]}
STATUS = enum_values("status")
STATUS_VALUE = {name: value for value, name in STATUS.items()}
CAPABILITIES = bits("capabilities")
CHANNELS = enum_values("channel")
AID_FORMATS = enum_values("aid_format")
AID_RESULT = enum_values("aid_result")
AID_RESULT_VALUE = {name: value for value, name in AID_RESULT.items()}

# SPEC.md §9.2 — CAN_SUBSCRIBE is CAN_SUBSCRIBE_MASK with this mask.
MASK_EXACT = 0x3FFFFFFF

# SPEC.md §13.4 — as many values as fit beside a monitor_header in one write at
# the minimum ATT MTU. Taken from the decoder rather than recomputed.
MONITOR_MAX_CHANNELS = _ref.MONITOR_MAX_CHANNELS


def can_max_payload(capabilities):
    """SPEC.md §4.2 — the largest CAN payload, which follows from the bits.

    Info carried this as a byte until every value it could hold turned out to be
    decided by the capability bits already, so two statements of one fact
    existed and an implementation could publish them disagreeing. A client
    computes it; so does this.
    """
    if "can" not in capabilities:
        return 0
    return 64 if "can_fd" in capabilities else 8


def absent_but_nonzero(record, payload, bitmask, base=0):
    """Fields whose validity bit is clear and whose bytes are not zero.

    SPEC.md §5.1 and 9.1 both say the same thing: if the bit is clear the device
    MUST write those fields as zero. The reference decoder reports which fields
    are absent, which is what a client needs; it does not look at what is in
    them, because a client that obeys the validity bit never reads them. A
    device that leaves a stale measurement there is nonetheless one firmware
    change away from a client that does.
    """
    import struct as _struct
    spec = SCHEMA["records"][record]
    bit_of = bits(bitmask)
    validity_field = next(f for f in spec["fields"]
                          if f["name"] == ("validity"))
    (validity,) = _struct.unpack_from(
        "<" + {"u8": "B", "u16": "H", "u32": "I"}[validity_field["type"]],
        payload, base + validity_field["offset"])
    pack = {"u8": "B", "i8": "b", "u16": "H", "i16": "h",
            "u32": "I", "i32": "i", "u64": "Q", "i64": "q"}
    out = []
    for field in spec["fields"]:
        valid_bit = field.get("valid_bit")
        if valid_bit is None or validity & (1 << bit_of[valid_bit]):
            continue
        (value,) = _struct.unpack_from("<" + pack[field["type"]], payload,
                                       base + field["offset"])
        if value:
            out.append((field["name"], value))
    return out
