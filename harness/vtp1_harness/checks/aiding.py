"""SPEC.md §14 -- Aiding: bulk data the client supplies to the device.

The second role that runs client-to-device, and the only one the conformance
corpus can say nothing about at all. The corpus decodes bytes a device sends;
every rule here is about what a device does with bytes it receives, and the
only way to ask is to send some and look at what comes back on Control.
"""
import struct
import zlib

from .. import refdec
from ..transport import DeviceRefused
from . import Fail, Observe, Skip, check

_HELD_UNTIL = 1 << refdec.bit("aid_validity", "held_until")
_PERSISTS = 1 << refdec.bit("aid_flags", "persists")
_FIRST_MISSING = 1 << refdec.bit("commit_validity", "first_missing")

_RESULT = refdec.enum_values("aid_result")
_RESULT_VALUE = {name: value for value, name in _RESULT.items()}
_FORMATS = refdec.enum_values("aid_format")

# SPEC.md §14.3 -- three bytes of ATT Write Command header and three of chunk
# header, both off the negotiated MTU.
_CHUNK_OVERHEAD = 6


def _control(s):
    if s.control is None:
        raise Skip("this device does not declare the control capability")
    return s.control


def _detail(response, record):
    try:
        return response.detail_as(record)
    except refdec.Reject as exc:
        raise Fail(f"the detail of a successful {response.opcode_name} did not "
                   f"decode as {record}: {exc}",
                   detail=response.detail.hex()) from None


def _caps(s):
    caps = s.state.get("aiding_caps")
    if caps is None:
        raise Skip("no declaration to work from")
    return caps


def _payload(n):
    """`n` bytes that are not all the same, so a misplaced chunk shows up.

    A run of zeroes would reassemble identically however the chunks were
    ordered, which is exactly the defect the CRC is meant to catch.
    """
    return bytes((i * 37 + (i >> 8)) & 0xFF for i in range(n))


def _split(blob, chunk_bytes):
    return [blob[i:i + chunk_bytes] for i in range(0, len(blob), chunk_bytes)]


async def _begin(s, total, fmt=None):
    c = _control(s)
    caps = _caps(s)
    fmt = caps["format"] if fmt is None else fmt
    return await c.request(refdec.OPCODE["GNSS_AID_BEGIN"],
                           struct.pack("<BI", fmt, total))


async def _write_chunk(s, session, index, body):
    """A chunk, written without a response. Nothing comes back by design."""
    payload = struct.pack("<BH", session, index) + body
    try:
        await s.transport.write(refdec.CHAR["aiding"], payload, response=False)
    except DeviceRefused as exc:
        raise Fail(f"the aiding characteristic refused a write. SPEC.md §14 "
                   f"makes it a Write Command, which carries no response of "
                   f"any kind: {exc}") from None


async def _commit(s, session, chunks, blob):
    c = _control(s)
    return await c.request(
        refdec.OPCODE["GNSS_AID_COMMIT"],
        struct.pack("<BHI", session, chunks, zlib.crc32(blob)))


async def _open_transfer(s, want=None):
    """Open a transfer sized to this device, returning (session, blob, chunks).

    Deliberately not a round number of chunks: the last chunk carries the
    remainder, and a transfer that divided exactly would never exercise
    §14.3's rule that only the last one may be short.
    """
    caps = _caps(s)
    begin = s.state.get("aiding_begin")
    chunk_bytes = begin["chunk_bytes"] if begin else None
    if chunk_bytes is None:
        raise Skip("no chunk size to work from")
    total = want or min(caps["max_bytes"], chunk_bytes * 2 + max(1, chunk_bytes // 2))
    if total > caps["max_bytes"]:
        total = caps["max_bytes"]
    response = await _begin(s, total)
    if not response.ok:
        raise Fail(f"GNSS_AID_BEGIN for {total} bytes, inside the device's own "
                   f"declared ceiling of {caps['max_bytes']}, was answered "
                   f"{response.status_name}", response=response.raw.hex())
    result = _detail(response, "aid_begin_result")
    blob = _payload(total)
    return result["session"], blob, _split(blob, result["chunk_bytes"])


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------

@check(id="aiding.declaration", section="14.2", phase="aiding", severity="MUST",
       requires=("gnss_aiding",),
       title="GNSS_AID_INFO declares a format, a ceiling and what is held")
async def aiding_declaration(s):
    c = _control(s)
    response = await c.request(refdec.OPCODE["GNSS_AID_INFO"])
    if not response.ok:
        raise Fail(f"GNSS_AID_INFO was answered {response.status_name}. A "
                   f"device declaring the aiding capability has to be able to "
                   f"say what it accepts", response=response.raw.hex())
    caps = _detail(response, "gnss_aid_caps")
    if caps["reserved_3"] != 0:
        raise Fail("gnss_aid_caps.reserved_3 is not zero; Appendix A holds it "
                   "for aiding metadata", detail=response.detail.hex())
    # SPEC.md §14.2 -- a client sizes its transfer against this before it
    # fetches anything, so zero means no transfer is ever possible.
    if caps["max_bytes"] == 0:
        raise Fail("max_bytes is zero, so every GNSS_AID_BEGIN this device can "
                   "receive is out of range and the role cannot be used")
    # SPEC.md §1.1 -- absence is the validity bit's job. A held_until behind a
    # cleared bit is the stale-value failure the whole protocol is shaped
    # against, and here it would stop a client sending aiding the device needs.
    if not caps["validity"] & _HELD_UNTIL and caps["held_until"] != 0:
        raise Fail(f"the held_until validity bit is clear and the field still "
                   f"carries {caps['held_until']}. A client MUST read that as "
                   f"'holds nothing'; a device MUST write it as zero",
                   detail=response.detail.hex())
    s.state["aiding_caps"] = caps


@check(id="aiding.format", section="14.1", phase="aiding", severity="OBSERVE",
       requires=("gnss_aiding",),
       title="Which aiding format this device accepts")
async def aiding_format(s):
    caps = _caps(s)
    name = _FORMATS.get(caps["format"])
    if name is None:
        # SPEC.md §14.1 lets a minor version add formats. Not a failure, but a
        # client that cannot fetch this product can do nothing with the role.
        s.note(f"the device asks for format value {caps['format']}, which this "
               f"version of the specification does not define. A client MUST "
               f"treat it as unknown and MUST NOT send another format.")
        raise Observe(f"unknown format {caps['format']}, ceiling "
                      f"{caps['max_bytes']} bytes", format=caps["format"])
    held = ("holds nothing" if not caps["validity"] & _HELD_UNTIL
            else f"holds data until {caps['held_until']} ms Unix")
    persists = ("survives a power cycle" if caps["flags"] & _PERSISTS
                else "does not survive a power cycle")
    return Observe(f"{name}, ceiling {caps['max_bytes']} bytes, {held}, "
                   f"{persists}",
                   format=name, max_bytes=caps["max_bytes"])


@check(id="aiding.chunk_size", section="14.3", phase="aiding", severity="MUST",
       requires=("gnss_aiding",),
       title="The chunk size a transfer is opened with fits one write")
async def aiding_chunk_size(s):
    caps = _caps(s)
    response = await _begin(s, min(caps["max_bytes"], 1024))
    if not response.ok:
        raise Fail(f"GNSS_AID_BEGIN was answered {response.status_name}",
                   response=response.raw.hex())
    result = _detail(response, "aid_begin_result")
    if result["reserved_3"] != 0:
        raise Fail("aid_begin_result.reserved_3 is not zero; Appendix A holds "
                   "it for transfer metadata", detail=response.detail.hex())
    chunk_bytes = result["chunk_bytes"]
    ceiling = s.mtu - _CHUNK_OVERHEAD
    if chunk_bytes == 0:
        raise Fail("chunk_bytes is zero, so no chunk can carry anything")
    if chunk_bytes > ceiling:
        raise Fail(f"chunk_bytes is {chunk_bytes}, above the {ceiling} a Write "
                   f"Command can carry at the negotiated ATT MTU of {s.mtu} "
                   f"(three bytes of ATT header, three of chunk header). A "
                   f"client cannot write a chunk this size")
    s.state["aiding_begin"] = result
    # Leave nothing open behind this check.
    await _control(s).request(refdec.OPCODE["GNSS_AID_ABORT"],
                              struct.pack("<B", result["session"]))


# ---------------------------------------------------------------------------
# Refusals decided before any chunk is written
# ---------------------------------------------------------------------------

@check(id="aiding.rejects_undeclared_format", section="14.1", phase="aiding",
       severity="MUST", requires=("gnss_aiding",), adversarial=True,
       title="A format the device did not declare is refused bad_params")
async def aiding_rejects_undeclared_format(s):
    caps = _caps(s)
    # A value no minor version has assigned, so it cannot be one this device
    # legitimately accepts.
    bogus = 0xFE
    if bogus == caps["format"]:
        raise Skip("the device declares the value this check probes with")
    response = await _begin(s, min(caps["max_bytes"], 1024), fmt=bogus)
    if response.ok:
        detail = _detail(response, "aid_begin_result")
        await _control(s).request(refdec.OPCODE["GNSS_AID_ABORT"],
                                  struct.pack("<B", detail["session"]))
        raise Fail(f"GNSS_AID_BEGIN naming format {bogus}, which this device "
                   f"did not declare, was accepted. The bytes of a transfer "
                   f"are opaque, so this refusal is the only place a client "
                   f"sending the wrong product can be caught at all")
    if response.status_name != "bad_params":
        raise Fail(f"an undeclared format was answered {response.status_name}, "
                   f"not bad_params. The opcode is available on this device, "
                   f"so the argument is what is wrong",
                   response=response.raw.hex())


@check(id="aiding.rejects_oversized", section="14.2", phase="aiding",
       severity="MUST", requires=("gnss_aiding",), adversarial=True,
       title="A transfer above the declared ceiling is refused bad_params")
async def aiding_rejects_oversized(s):
    caps = _caps(s)
    if caps["max_bytes"] >= 0xFFFFFFFF:
        raise Skip("the declared ceiling is the field's own maximum, so there "
                   "is no value above it to try")
    response = await _begin(s, caps["max_bytes"] + 1)
    if response.ok:
        detail = _detail(response, "aid_begin_result")
        await _control(s).request(refdec.OPCODE["GNSS_AID_ABORT"],
                                  struct.pack("<B", detail["session"]))
        raise Fail(f"a transfer of {caps['max_bytes'] + 1} bytes was accepted "
                   f"against a declared ceiling of {caps['max_bytes']}. A "
                   f"ceiling discovered by running out of memory part way "
                   f"through is not a ceiling")
    if response.status_name != "bad_params":
        raise Fail(f"an oversized transfer was answered {response.status_name}, "
                   f"not bad_params", response=response.raw.hex())


# ---------------------------------------------------------------------------
# The transfer itself
# ---------------------------------------------------------------------------

@check(id="aiding.transfer", section="14.4", phase="aiding", severity="MUST",
       requires=("gnss_aiding",),
       title="A complete transfer commits as applied")
async def aiding_transfer(s):
    session, blob, chunks = await _open_transfer(s)
    for index, body in enumerate(chunks):
        await _write_chunk(s, session, index, body)
    response = await _commit(s, session, len(chunks), blob)
    if not response.ok:
        raise Fail(f"GNSS_AID_COMMIT on an open session with well-formed "
                   f"parameters was answered {response.status_name}. SPEC.md "
                   f"§14.5 -- the status is about the request, and this request "
                   f"was applied", response=response.raw.hex())
    result = _detail(response, "aid_commit_result")
    name = _RESULT.get(result["result"], result["result"])
    if result["result"] == _RESULT_VALUE["incomplete"]:
        raise Fail(f"every one of the {len(chunks)} chunks was written and the "
                   f"device reports chunk {result['first_missing']} missing",
                   detail=response.detail.hex())
    if result["result"] == _RESULT_VALUE["bad_crc"]:
        raise Fail("the CRC-32 did not match. SPEC.md §14.4 fixes it as the "
                   "IEEE 802.3 polynomial, reflected, initial and final value "
                   "0xFFFFFFFF, over the reassembled payload and not the "
                   "chunks", detail=response.detail.hex())
    if result["result"] != _RESULT_VALUE["applied"]:
        # `rejected` is legitimate -- the receiver may refuse synthetic bytes.
        s.note(f"a complete, intact transfer of synthetic bytes was answered "
               f"{name}. That is conforming: SPEC.md §14.4's `rejected` says "
               f"the transfer arrived and the receiver would not take it, "
               f"which is the expected answer to a payload that is not real "
               f"aiding data.")
        raise Observe(f"complete transfer answered {name}", result=name)
    # SPEC.md §14.4 -- nothing is missing, so there is no index to report.
    if result["validity"] & _FIRST_MISSING:
        raise Fail("the transfer was applied and the first_missing bit is set. "
                   "Chunk 0 is a real index, so a client reads this as a lost "
                   "chunk in a transfer that succeeded",
                   detail=response.detail.hex())
    s.state["aiding_applied"] = True


@check(id="aiding.reports_missing_chunk", section="14.4", phase="aiding",
       severity="MUST", requires=("gnss_aiding",), adversarial=True,
       title="A gap is reported by index, and the transfer stays open")
async def aiding_reports_missing_chunk(s):
    session, blob, chunks = await _open_transfer(s)
    if len(chunks) < 2:
        raise Skip("this device's chunk size makes the transfer a single "
                   "chunk, so there is no gap to leave")
    # Leave exactly one hole, and not the first: a device reporting 0 whatever
    # is missing would pass a check that skipped chunk 0.
    hole = 1
    for index, body in enumerate(chunks):
        if index != hole:
            await _write_chunk(s, session, index, body)

    response = await _commit(s, session, len(chunks), blob)
    if not response.ok:
        raise Fail(f"a commit naming an open session was answered "
                   f"{response.status_name}. An incomplete transfer is a "
                   f"result, not a refused request (SPEC.md §14.5)",
                   response=response.raw.hex())
    result = _detail(response, "aid_commit_result")
    if result["result"] != _RESULT_VALUE["incomplete"]:
        raise Fail(f"chunk {hole} of {len(chunks)} was never written and the "
                   f"commit was answered "
                   f"{_RESULT.get(result['result'], result['result'])}. A "
                   f"write-without-response path with no missing-chunk report "
                   f"loses data silently", detail=response.detail.hex())
    if not result["validity"] & _FIRST_MISSING:
        raise Fail("the result is incomplete and the first_missing bit is "
                   "clear, so there is no index to resend from",
                   detail=response.detail.hex())
    if result["first_missing"] != hole:
        raise Fail(f"the device reports chunk {result['first_missing']} as the "
                   f"lowest missing; chunk {hole} was the one withheld. A "
                   f"client resends from this index, so a wrong one either "
                   f"re-sends the whole transfer or never fills the gap",
                   detail=response.detail.hex())

    # SPEC.md §14.4 -- incomplete leaves the transfer OPEN. Filling the gap and
    # committing again is the entire point of reporting an index.
    await _write_chunk(s, session, hole, chunks[hole])
    response = await _commit(s, session, len(chunks), blob)
    if not response.ok:
        raise Fail(f"the second commit was answered {response.status_name}. "
                   f"SPEC.md §14.4 keeps the transfer open after `incomplete`, "
                   f"so the session was still this device's to hold",
                   response=response.raw.hex())
    result = _detail(response, "aid_commit_result")
    if result["result"] == _RESULT_VALUE["incomplete"]:
        raise Fail(f"the gap was filled and the device still reports chunk "
                   f"{result['first_missing']} missing, so a client following "
                   f"§14.4 never converges", detail=response.detail.hex())


@check(id="aiding.detects_corruption", section="14.4", phase="aiding",
       severity="MUST", requires=("gnss_aiding",), adversarial=True,
       title="A transfer whose CRC does not match is refused bad_crc")
async def aiding_detects_corruption(s):
    session, blob, chunks = await _open_transfer(s)
    for index, body in enumerate(chunks):
        await _write_chunk(s, session, index, body)
    # Every chunk arrived; the client's CRC says the bytes are not the ones it
    # meant to send. Without this check a device could apply anything.
    response = await _control(s).request(
        refdec.OPCODE["GNSS_AID_COMMIT"],
        struct.pack("<BHI", session, len(chunks), zlib.crc32(blob) ^ 0xFFFF))
    if not response.ok:
        raise Fail(f"the commit was answered {response.status_name}; a failed "
                   f"integrity check is a result, not a refused request",
                   response=response.raw.hex())
    result = _detail(response, "aid_commit_result")
    if result["result"] == _RESULT_VALUE["applied"]:
        raise Fail("a transfer whose CRC-32 does not match the payload was "
                   "applied. The CRC is the only end-to-end check a "
                   "write-without-response path has",
                   detail=response.detail.hex())
    if result["result"] != _RESULT_VALUE["bad_crc"]:
        raise Fail(f"a CRC mismatch was reported as "
                   f"{_RESULT.get(result['result'], result['result'])}, not "
                   f"bad_crc. Nothing was missing, so a client told "
                   f"`incomplete` has no gap to fill and retries forever",
                   detail=response.detail.hex())


@check(id="aiding.abort", section="14.4", phase="aiding", severity="MUST",
       requires=("gnss_aiding",), adversarial=True,
       title="An aborted session is no longer the device's to commit")
async def aiding_abort(s):
    session, blob, chunks = await _open_transfer(s)
    await _write_chunk(s, session, 0, chunks[0])
    response = await _control(s).request(refdec.OPCODE["GNSS_AID_ABORT"],
                                         struct.pack("<B", session))
    if not response.ok:
        raise Fail(f"GNSS_AID_ABORT on an open session was answered "
                   f"{response.status_name}", response=response.raw.hex())
    # SPEC.md §14.4 -- the session is freed, so a commit naming it is a commit
    # on a session the device does not hold.
    response = await _commit(s, session, len(chunks), blob)
    if response.ok:
        raise Fail("a commit naming an aborted session was accepted. The "
                   "device is holding a transfer it was told to discard, and "
                   "§14.3 allows it only one")
    if response.status_name != "bad_params":
        raise Fail(f"a commit on an unknown session was answered "
                   f"{response.status_name}, not bad_params",
                   response=response.raw.hex())
