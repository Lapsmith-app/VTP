"""SPEC.md §13 -- Monitor: the one role that runs from the client to the device.

A device with a screen asks the client for values it cannot compute. Nothing in
the conformance corpus can test that side of it, because the corpus decodes what
a device sends and this is a thing a device receives.
"""
import struct

from .. import refdec
from ..transport import DeviceRefused, TransportError
from . import Fail, Observe, Skip, check

_MONITOR_PRESENT = 1 << refdec.bit("monitor_validity", "present")


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


def _write(seq, values):
    """`values` is a sequence of (slot, present, value)."""
    out = bytearray(struct.pack("<HBB", seq, len(values), 0))
    for slot, present, value in values:
        out += struct.pack("<BBi", slot, _MONITOR_PRESENT if present else 0,
                           value if present else 0)
    return bytes(out)


async def _send(s, payload):
    """Write to monitor_values. Returns None if accepted, or the refusal."""
    try:
        await s.transport.write(refdec.CHAR["monitor_values"], payload,
                                response=True)
        return None
    except DeviceRefused as exc:
        return exc


@check(id="monitor.declaration", section="13.3", phase="monitor", severity="MUST",
       requires=("monitor",),
       title="MONITOR_LIST returns the whole declaration in one response")
async def monitor_declaration(s):
    c = _control(s)
    # Not paged, and takes no parameters: §13.4 caps a device at 15 channels,
    # which is 62 bytes inside the 97 a response carries at the minimum ATT MTU,
    # so a page index could never be anything but zero.
    response = await c.request(refdec.OPCODE["MONITOR_LIST"])
    if not response.ok:
        raise Fail(f"MONITOR_LIST was answered {response.status_name}. A device "
                   f"declaring the Monitor capability has to be able to say "
                   f"which channels it wants", response=response.raw.hex())
    s.state["monitor_raw"] = response.detail
    declaration = _detail(response, "monitor_list")
    if declaration["declaration"]["reserved"] != 0:
        raise Fail("monitor_declaration.reserved is not zero; Appendix A holds "
                   "it for declaration metadata", detail=response.detail.hex())
    entries = declaration["entries"]
    if not entries:
        raise Fail("the Monitor capability is declared and the device asks for "
                   "no channels, so no client can supply anything")
    s.state["monitor_channels"] = entries


@check(id="monitor.channels", section="13.2", phase="monitor", severity="OBSERVE",
       requires=("monitor",),
       title="Which channels this device asks for")
async def monitor_channels(s):
    entries = s.state.get("monitor_channels")
    if not entries:
        raise Skip("no declaration to check")
    described, unknown = [], []
    for e in entries:
        name = refdec.CHANNELS.get(e["channel"])
        age = f"{e['max_age'] / 10:.1f}s"
        if name is None:
            unknown.append(e["channel"])
            described.append(f"slot {e['slot']}: unknown channel "
                             f"{e['channel']} ({age})")
        else:
            described.append(f"slot {e['slot']}: {name} ({age})")
    if unknown:
        # Not a failure: §13.2 lets a minor version add channels, and a client
        # that does not implement one reports it absent rather than omitting it.
        s.note(f"the device asks for channel value(s) {unknown}, which this "
               f"version of the specification does not define. A client MUST "
               f"report them absent rather than substitute another channel.")
    raise Observe("; ".join(described), channels=described)


@check(id="monitor.accepts_complete_write", section="13.4", phase="monitor",
       severity="MUST", requires=("monitor",),
       title="A complete write is accepted")
async def monitor_accepts_complete_write(s):
    entries = s.state.get("monitor_channels")
    if not entries:
        raise Skip("no declaration to write against")
    values = [(e["slot"], True, 1000 + i) for i, e in enumerate(entries)]
    refusal = await _send(s, _write(0, values))
    if refusal is not None:
        raise Fail(f"a write carrying every declared slot was refused: {refusal}",
                   slots=[e["slot"] for e in entries])
    s.state["monitor_seq"] = 1


@check(id="monitor.absent_is_a_state", section="13.4", phase="monitor",
       severity="MUST", requires=("monitor",),
       title="A write with the present bit clear is accepted")
async def monitor_absent_is_a_state(s):
    entries = s.state.get("monitor_channels")
    if not entries:
        raise Skip("no declaration to write against")
    # Before the first lap of a session there is no last lap time. A client
    # MUST clear the bit rather than omit the slot or send a placeholder, so a
    # device that cannot take a cleared bit has left the client no honest move.
    values = [(e["slot"], i % 2 == 0, 42) for i, e in enumerate(entries)]
    refusal = await _send(s, _write(s.state.get("monitor_seq", 1), values))
    s.state["monitor_seq"] = s.state.get("monitor_seq", 1) + 1
    if refusal is not None:
        raise Fail(f"a write with the present bit clear on some slots was "
                   f"refused: {refusal}")


@check(id="monitor.rejects_incomplete", section="13.4", phase="monitor",
       severity="MUST", requires=("monitor",), adversarial=True,
       title="A write missing a declared slot is rejected")
async def monitor_rejects_incomplete(s):
    entries = s.state.get("monitor_channels")
    if not entries:
        raise Skip("no declaration to write against")
    if len(entries) < 2:
        raise Skip("only one channel is declared, so no write can be partial "
                   "and still carry something")
    values = [(e["slot"], True, 7) for e in entries[:-1]]
    refusal = await _send(s, _write(s.state.get("monitor_seq", 1), values))
    s.state["monitor_seq"] = s.state.get("monitor_seq", 1) + 1
    if refusal is None:
        raise Fail(
            f"a write carrying {len(values)} of {len(entries)} declared slots "
            f"was accepted. Every write MUST carry every slot: merging a subset "
            f"keeps the omitted slot's previous value AND its previous "
            f"timestamp, so it stays on screen looking current while the client "
            f"has stopped saying anything about it",
            omitted=entries[-1]["slot"])


@check(id="monitor.rejects_bad_length", section="13.4", phase="monitor",
       severity="MUST", requires=("monitor",), adversarial=True,
       title="A write whose length does not match its count is rejected")
async def monitor_rejects_bad_length(s):
    entries = s.state.get("monitor_channels")
    if not entries:
        raise Skip("no declaration to write against")
    values = [(e["slot"], True, 5) for e in entries]
    payload = bytearray(_write(s.state.get("monitor_seq", 1), values))
    payload[2] = len(entries) + 1              # count says one more than follows
    s.state["monitor_seq"] = s.state.get("monitor_seq", 1) + 1
    refusal = await _send(s, bytes(payload))
    if refusal is None:
        raise Fail("a write whose count exceeds the values that follow it was "
                   "accepted; the length MUST equal the header plus exactly "
                   "count values", payload=bytes(payload).hex())


@check(id="monitor.rejects_duplicate_slot", section="13.4", phase="monitor",
       severity="MUST", requires=("monitor",), adversarial=True,
       title="A write carrying one slot twice is rejected")
async def monitor_rejects_duplicate_slot(s):
    entries = s.state.get("monitor_channels")
    if not entries:
        raise Skip("no declaration to write against")
    values = [(e["slot"], True, 3) for e in entries]
    values.append((entries[0]["slot"], True, 4))
    refusal = await _send(s, _write(s.state.get("monitor_seq", 1), values))
    s.state["monitor_seq"] = s.state.get("monitor_seq", 1) + 1
    if refusal is None:
        raise Fail(
            f"a write carrying slot {entries[0]['slot']} twice was accepted. "
            f"Nothing in the specification says which of the two wins, so a "
            f"device choosing either is choosing for every client")


@check(id="monitor.ignores_unknown_slot", section="13.1", phase="monitor",
       severity="MUST", requires=("monitor",), adversarial=True,
       title="A value for a slot the device did not ask for is ignored")
async def monitor_ignores_unknown_slot(s):
    entries = s.state.get("monitor_channels")
    if not entries:
        raise Skip("no declaration to write against")
    declared = {e["slot"] for e in entries}
    spare = next((n for n in range(256) if n not in declared), None)
    if spare is None:
        raise Skip("every slot number is declared")
    values = [(e["slot"], True, 9) for e in entries] + [(spare, True, 9)]
    refusal = await _send(s, _write(s.state.get("monitor_seq", 1), values))
    s.state["monitor_seq"] = s.state.get("monitor_seq", 1) + 1
    if refusal is not None:
        # The client may simply be a version ahead of the device, which is the
        # case 13.1 has in mind. Ignoring is required; refusing breaks it.
        raise Fail(
            f"a complete write with one extra slot ({spare}) that the device "
            f"never asked for was refused: {refusal}. A device MUST ignore a "
            f"value for a slot it did not ask for")


@check(id="monitor.freshness", section="13.5", phase="monitor", severity="OBSERVE",
       requires=("monitor",),
       title="Freshness expiry cannot be observed from here")
async def monitor_freshness(s):
    entries = s.state.get("monitor_channels") or []
    ages = sorted({e["max_age"] for e in entries if e["max_age"]})
    s.note("SPEC.md §13.5 requires a device to render a value unavailable once "
           "its max_age has passed. That happens on the device's own display "
           "and produces nothing on the wire, so no client-side harness can "
           "verify it. Check it by eye: stop writing and watch the screen.")
    if not ages:
        raise Skip("no channel declares a deadline")
    raise Observe(
        f"the shortest deadline is {min(ages) / 10:.1f}s and the longest "
        f"{max(ages) / 10:.1f}s. Verify expiry on the device's own display",
        shortest_s=min(ages) / 10, longest_s=max(ages) / 10)
