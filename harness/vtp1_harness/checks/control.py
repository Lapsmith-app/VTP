"""SPEC.md §9 -- the control plane: the half of this specification no byte
vector can reach.

The conformance corpus tests that a client decodes a control_response. Nothing
in it tests that a device produces the right one, because that needs a device
to ask. Everything here asks.
"""
import asyncio
import struct

from .. import refdec
from ..session import ControlTimeout
from ..transport import DeviceRefused, TransportError
from . import Fail, Observe, Skip, check

#: An opcode this major version does not allocate and, per 11.4, never will
#: below the reserved range. Used as a universally safe probe: every Control
#: device must answer it, and answering it changes nothing.
UNALLOCATED_OPCODE = 0x7E

#: An identifier used for the harness's own subscriptions. Standard-format and
#: high enough to be unlikely on a real bus.
PROBE_ID = 0x7A0


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


async def _raw(s, opcode, tag, params=b"", settle=1.0):
    """Write a request the correlation layer must not own, and collect whatever
    comes back. Used where the point of the test is a response that cannot be
    correlated -- a duplicated tag, or one the device failed to echo."""
    c = _control(s)
    first = len(c.history)
    await s.transport.write(refdec.CHAR["control"],
                            bytes([opcode, tag]) + bytes(params), response=True)
    await asyncio.sleep(settle)
    return c.history[first:]


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------

@check(id="control.unsupported_opcode", section="9", phase="control",
       severity="MUST", requires=("control",),
       title="An unallocated opcode is answered unsupported_opcode")
async def control_unsupported_opcode(s):
    c = _control(s)
    try:
        response = await c.request(UNALLOCATED_OPCODE)
    except ControlTimeout as exc:
        if exc.orphans:
            raise Fail(
                f"no response carrying tag {exc.tag}, but {len(exc.orphans)} "
                f"response(s) arrived with other tags. §9 makes the tag the "
                f"device's only obligation for correlation",
                received=[r.raw.hex() for r in exc.orphans]) from None
        raise Fail("no response at all to an unallocated opcode. A device MUST "
                   "respond to every request") from None
    s.state["probe_response"] = response
    if response.status != refdec.STATUS_VALUE["unsupported_opcode"]:
        raise Fail(f"answered {response.status_name}, not unsupported_opcode",
                   response=response.raw.hex())


@check(id="control.echoes_request", section="9", phase="control", severity="MUST",
       requires=("control",),
       title="The response echoes the request's opcode and tag")
async def control_echoes_request(s):
    c = _control(s)
    tag = 0x5C
    responses = await _raw(s, UNALLOCATED_OPCODE, tag)
    if not responses:
        raise Fail("no response to correlate")
    if not any(r.tag == tag and r.opcode == UNALLOCATED_OPCODE for r in responses):
        raise Fail(
            f"sent opcode 0x{UNALLOCATED_OPCODE:02x} tag 0x{tag:02x}; got "
            f"{', '.join(f'opcode 0x{r.opcode:02x} tag 0x{r.tag:02x}' for r in responses)}. "
            f"Correlation is the tag's only job, and a client cannot tell two "
            f"outstanding requests apart without it",
            responses=[r.raw.hex() for r in responses])


@check(id="control.detail_only_on_ok", section="9", phase="control",
       severity="MUST", requires=("control",), adversarial=True,
       title="A refused request is answered with exactly three bytes")
async def control_detail_only_on_ok(s):
    response = s.state.get("probe_response")
    if response is None:
        raise Skip("the unallocated-opcode probe did not return")
    if response.reject_reason == "detail-on-error" or response.detail:
        # The alternative -- a fixed-width response with the detail zeroed --
        # puts a well-formed handle 0 in front of a client that has already
        # decided the request succeeded. That is the plausible wrong value 1.1
        # exists to prevent.
        raise Fail(
            f"a refused request was answered with {len(response.detail)} extra "
            f"byte(s) of detail. Detail is present if and only if status is ok",
            response=response.raw.hex())


@check(id="control.malformed_params", section="9", phase="control",
       severity="MUST", requires=("control",), adversarial=True,
       title="Requests with wrong-length parameters are refused")
async def control_malformed_params(s):
    c = _control(s)
    cases = [
        ("CAN_RESET with a trailing byte", refdec.OPCODE["CAN_RESET"], b"\x00"),
        ("CAN_SUBSCRIBE truncated to 3 params", refdec.OPCODE["CAN_SUBSCRIBE"],
         b"\x00\x01\x00"),
        ("GPS_SET_RATE with no parameters", refdec.OPCODE["GPS_SET_RATE"], b""),
        ("TIME_SYNC with parameters", refdec.OPCODE["TIME_SYNC"], b"\x01\x02"),
    ]
    problems = []
    for label, opcode, params in cases:
        try:
            response = await c.request(opcode, params)
        except ControlTimeout:
            problems.append(f"{label}: no response")
            continue
        if response.status == refdec.STATUS_VALUE["unsupported_opcode"]:
            continue                      # the opcode itself is not implemented
        if response.ok:
            problems.append(f"{label}: answered ok")
        elif response.detail:
            problems.append(f"{label}: refused, but carried a detail")
    if problems:
        raise Fail("; ".join(problems))


@check(id="control.four_outstanding", section="9", phase="control",
       severity="MUST", requires=("control",),
       title="Four requests outstanding at once are all answered")
async def control_four_outstanding(s):
    c = _control(s)
    inflight = []
    for _ in range(4):
        inflight.append(await c.send(UNALLOCATED_OPCODE))
    answered, busy, lost = [], [], []
    for tag, future in inflight:
        try:
            response = await c.await_response(UNALLOCATED_OPCODE, tag, future,
                                              timeout=5.0)
        except ControlTimeout:
            lost.append(tag)
            continue
        (busy if response.status == refdec.STATUS_VALUE["busy"] else answered
         ).append(response)
    if lost:
        raise Fail(
            f"{len(lost)} of 4 requests went unanswered. §9 sets a floor of four "
            f"outstanding requests and requires busy rather than a silent "
            f"discard -- a client installing a table is otherwise held to one "
            f"round trip per connection interval", unanswered=lost)
    if busy:
        raise Fail(f"{len(busy)} of 4 requests were refused busy; the floor is four",
                   busy=[r.raw.hex() for r in busy])


@check(id="control.duplicate_tag", section="9", phase="control", severity="MUST",
       requires=("control",), adversarial=True,
       title="A tag already outstanding is refused bad_params")
async def control_duplicate_tag(s):
    c = _control(s)
    tag = 0x77
    first = len(c.history)
    uuid = refdec.CHAR["control"]
    for _ in range(2):
        await s.transport.write(uuid, bytes([UNALLOCATED_OPCODE, tag]),
                                response=True)
    await asyncio.sleep(1.5)
    responses = [r for r in c.history[first:] if r.tag == tag]
    if len(responses) < 2:
        raise Fail(f"two requests sharing tag 0x{tag:02x} produced "
                   f"{len(responses)} response(s); a device MUST respond to "
                   f"every request",
                   responses=[r.raw.hex() for r in responses])
    if not any(r.status == refdec.STATUS_VALUE["bad_params"] for r in responses):
        # If the device answered the first before the second arrived, the tag
        # was never actually duplicated and there was nothing to detect. That is
        # a limit of testing from a host, not a finding.
        raise Observe(
            "both requests sharing a tag were answered ok; the device answered "
            "the first before the second arrived, so the tags were never "
            "outstanding together and this could not be tested from here",
            responses=[r.raw.hex() for r in responses])


# ---------------------------------------------------------------------------
# CAN subscriptions
# ---------------------------------------------------------------------------

@check(id="can.subscribe_handle", section="9.2", phase="control", severity="MUST",
       requires=("control", "can"),
       title="Installing a subscription returns a handle")
async def can_subscribe_handle(s):
    c = _control(s)
    response = await c.subscribe_can(PROBE_ID)
    if not response.ok:
        raise Fail(f"CAN_SUBSCRIBE was answered {response.status_name}",
                   response=response.raw.hex())
    if len(response.detail) != 2:
        raise Fail(f"the handle detail is {len(response.detail)} bytes; 9 "
                   f"defines handle:u16", detail=response.detail.hex())
    handle = struct.unpack("<H", response.detail)[0]
    s.state["probe_handle"] = handle
    s.state.setdefault("installed", {})[PROBE_ID] = handle


@check(id="can.subscribe_idempotent", section="9.2", phase="control",
       severity="MUST", requires=("control", "can"),
       title="Re-installing the same id and mask updates in place")
async def can_subscribe_idempotent(s):
    c = _control(s)
    handle = s.state.get("probe_handle")
    if handle is None:
        raise Skip("the first subscription did not install")
    response = await c.subscribe_can(PROBE_ID, mode=0, arg=0)
    if not response.ok:
        raise Fail(f"re-installing an identical subscription was answered "
                   f"{response.status_name}", response=response.raw.hex())
    again = struct.unpack("<H", response.detail)[0]
    if again != handle:
        raise Fail(
            f"the same id and mask returned handle {again}, not {handle}. A "
            f"client that reprograms unconditionally on every connection -- "
            f"which §4 already forces on it -- would exhaust the table",
            first=handle, second=again)


@check(id="can.unknown_handle", section="9.2", phase="control", severity="MUST",
       requires=("control", "can"), adversarial=True,
       title="Unsubscribing a handle that names nothing is refused")
async def can_unknown_handle(s):
    c = _control(s)
    installed = set(s.state.get("installed", {}).values())
    handle = next(h for h in range(0xFFFF, 0, -1) if h not in installed)
    response = await c.request(refdec.OPCODE["CAN_UNSUBSCRIBE"],
                               struct.pack("<H", handle))
    if response.status != refdec.STATUS_VALUE["unknown_handle"]:
        raise Fail(
            f"unsubscribing handle {handle}, which was never issued, was "
            f"answered {response.status_name} rather than unknown_handle. A "
            f"client that cannot verify what it removed has not removed it",
            response=response.raw.hex())


@check(id="can.list_matches_installed", section="9.5", phase="control",
       severity="MUST", requires=("control", "can"),
       title="CAN_LIST reports the table exactly as installed")
async def can_list_matches_installed(s):
    c = _control(s)
    installed = s.state.get("installed", {})
    if not installed:
        raise Skip("no subscription was installed to list")
    entries, pages, last = await c.pages(refdec.OPCODE["CAN_LIST"], "can_list")
    if not last.ok:
        raise Fail(f"CAN_LIST was answered {last.status_name}")
    for response, page in pages:
        if page["page"]["reserved"] != 0:
            raise Fail("can_list_page.reserved is not zero; Appendix A holds it "
                       "for paging metadata", page=response.detail.hex())
    total = pages[0][1]["page"]["total"]
    if total != len(installed):
        raise Fail(f"CAN_LIST reports {total} subscription(s); {len(installed)} "
                   f"were installed", installed=sorted(installed.values()))
    by_handle = {e["handle"]: e for e in entries}
    for can_id, handle in installed.items():
        entry = by_handle.get(handle)
        if entry is None:
            raise Fail(f"handle {handle} was issued but does not appear in the "
                       f"table", listed=sorted(by_handle))
        if entry["id"] != can_id or entry["mask"] != refdec.MASK_EXACT:
            # A device that normalises, reorders or summarises here defeats the
            # only purpose CAN_LIST has.
            raise Fail(
                f"handle {handle} was installed as id 0x{can_id:x} mask "
                f"0x{refdec.MASK_EXACT:x} and is reported as id "
                f"0x{entry['id']:x} mask 0x{entry['mask']:x}", entry=entry)


@check(id="can.list_beyond_end", section="9.5", phase="control", severity="MUST",
       requires=("control", "can"), adversarial=True,
       title="A start index past the end of the table is not an error")
async def can_list_beyond_end(s):
    c = _control(s)
    response = await c.request(refdec.OPCODE["CAN_LIST"], struct.pack("<H", 0xFFF0))
    if not response.ok:
        raise Fail(f"a start index past the end was answered "
                   f"{response.status_name}; §9.5 makes it ok with count zero",
                   response=response.raw.hex())
    page = _detail(response, "can_list")["page"]
    if page["count"] != 0:
        raise Fail(f"count is {page['count']} for a start index past the end",
                   page=page)
    expected = len(s.state.get("installed", {}))
    if page["total"] != expected:
        raise Fail(f"total is {page['total']}; {expected} subscription(s) are "
                   f"installed. total MUST be the number installed at the "
                   f"moment the page was produced", page=page)


@check(id="can.table_full", section="9.2", phase="control", severity="MUST",
       requires=("control", "can"), adversarial=True,
       title="A subscription beyond the declared slot count is refused table_full")
async def can_table_full(s):
    c = _control(s)
    slots = s.info["can_subscription_slots"]
    if slots > 64:
        raise Skip(f"the device declares {slots} slots; filling them would take "
                   f"longer than this check is worth")
    installed = dict(s.state.get("installed", {}))
    base = 0x200
    refusals = []
    try:
        for i in range(slots + 1):
            can_id = base + i
            if can_id in installed:
                continue
            response = await c.subscribe_can(can_id)
            if response.ok:
                installed[can_id] = struct.unpack("<H", response.detail)[0]
            else:
                refusals.append((can_id, response))
            if len(installed) > slots:
                break
        if not refusals:
            raise Fail(
                f"installed {len(installed)} subscriptions against a declared "
                f"{slots} slot(s) with nothing refused. A device MUST refuse "
                f"with table_full rather than accept and silently discard frames",
                installed=len(installed), slots=slots)
        first = refusals[0][1]
        if first.status == refdec.STATUS_VALUE["rate_exceeded"]:
            # 9.4 -- the prediction this status once implied cannot be made,
            # and the specification removed the rule rather than patch it again.
            raise Fail(
                "a CAN subscription was refused rate_exceeded. §9.4 forbids "
                "refusing one on rate grounds: a device admits, and sheds if it "
                "must, reporting the loss in dropped",
                response=first.raw.hex())
        if first.status != refdec.STATUS_VALUE["table_full"]:
            raise Fail(f"the {len(installed) + 1}th subscription against "
                       f"{slots} slot(s) was answered {first.status_name}, not "
                       f"table_full", response=first.raw.hex())
    finally:
        # Leave the table as this phase found it, so the stream checks are not
        # reading a bus the harness saturated.
        await c.request(refdec.OPCODE["CAN_RESET"])
        s.state["installed"] = {}
        s.state.pop("probe_handle", None)


@check(id="control.applies_only_if_answerable", section="9.6", phase="control",
       severity="MUST", requires=("control", "can"), adversarial=True,
       title="A request whose response cannot be delivered is not applied")
async def control_applies_only_if_answerable(s):
    c = _control(s)
    install = await c.subscribe_can(PROBE_ID)
    if not install.ok:
        raise Skip("could not install a subscription to test against")
    s.state.setdefault("installed", {})[PROBE_ID] = struct.unpack(
        "<H", install.detail)[0]

    try:
        await c.disable()
    except (DeviceRefused, TransportError) as exc:
        raise Skip(f"this platform would not disable indications: {exc}")
    before = len(c.history)
    refused_at_att = False
    try:
        await s.transport.write(refdec.CHAR["control"],
                                bytes([refdec.OPCODE["CAN_RESET"], 0x42]),
                                response=True)
    except DeviceRefused:
        refused_at_att = True
    await asyncio.sleep(0.5)
    await c.enable()
    await asyncio.sleep(0.5)

    entries, _, last = await c.pages(refdec.OPCODE["CAN_LIST"], "can_list")
    survived = bool(entries)
    answered = any(r.tag == 0x42 for r in c.history[before:])

    if refused_at_att:
        raise Observe("the device refused the write outright while indications "
                      "were disabled, so the request was never dispatched")
    if survived:
        return                                  # not applied: the rule holds
    if answered:
        # The device held the request until its answer could be delivered and
        # then applied it. Deliverability was still decided before dispatch,
        # which is what §9.6 asks for.
        raise Observe("the request was held until indications returned, then "
                      "applied and answered")
    raise Fail(
        "CAN_RESET took effect while indications were disabled and was never "
        "answered. §9.6 makes deliverability a precondition of dispatch: a "
        "client that retries a request it believes was lost applies it twice")


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------

@check(id="control.rate_ceiling", section="9.4", phase="control", severity="MUST",
       requires=("control",), adversarial=True,
       title="A rate above the declared maximum is refused rate_exceeded")
async def control_rate_ceiling(s):
    c = _control(s)
    cases = []
    if s.has("gps"):
        cases.append(("GPS_SET_RATE", refdec.OPCODE["GPS_SET_RATE"],
                      s.info["gps_max_rate_hz"], s.info["gps_rate_hz"]))
    if s.has("imu"):
        cases.append(("IMU_SET_RATE", refdec.OPCODE["IMU_SET_RATE"],
                      s.info["imu_max_rate_hz"], s.info["imu_rate_hz"]))
    if not cases:
        raise Skip("this device declares neither GPS nor IMU")
    problems, skipped = [], []
    for name, opcode, ceiling, current in cases:
        if ceiling >= 0xFFFF:
            skipped.append(f"{name}: the declared maximum is the field's own "
                           f"maximum, so there is no value above it to try")
            continue
        response = await c.request(opcode, struct.pack("<H", ceiling + 1))
        if response.status == refdec.STATUS_VALUE["unsupported_opcode"]:
            skipped.append(f"{name}: not implemented")
            continue
        if response.status != refdec.STATUS_VALUE["rate_exceeded"]:
            problems.append(
                f"{name} at {ceiling + 1} Hz against a declared maximum of "
                f"{ceiling} was answered {response.status_name}, not rate_exceeded")
        # Put back what the device said it was doing, so the observed-rate
        # measurement later is measuring the device's own configuration.
        await c.request(opcode, struct.pack("<H", current))
    if problems:
        raise Fail("; ".join(problems))
    if skipped and len(skipped) == len(cases):
        raise Skip("; ".join(skipped))


# ---------------------------------------------------------------------------
# TIME_SYNC
# ---------------------------------------------------------------------------

TIME_SYNC_SAMPLES = 6


@check(id="control.time_sync", section="9.7", phase="control", severity="MUST",
       requires=("control",),
       title="TIME_SYNC carries two distinct readings of one clock")
async def control_time_sync(s):
    c = _control(s)
    first = await c.request(refdec.OPCODE["TIME_SYNC"])
    if first.status == refdec.STATUS_VALUE["unsupported_opcode"]:
        raise Skip("TIME_SYNC is not implemented")
    samples = []
    for _ in range(TIME_SYNC_SAMPLES):
        response = await c.request(refdec.OPCODE["TIME_SYNC"])
        if not response.ok:
            raise Fail(f"TIME_SYNC was answered {response.status_name}",
                       response=response.raw.hex())
        samples.append((response, _detail(response, "time_sync")))
    s.state["time_sync"] = samples

    if all(ts["processing_us"] == 0 for _, ts in samples):
        # 9.7 is explicit about this one: a device that reads its clock once and
        # reports it as both timestamps has silently implemented the
        # single-timestamp form while appearing to implement this one, and the
        # client's whole error bound goes with it.
        raise Fail(
            f"t_device_rx and t_device_tx were identical in all "
            f"{TIME_SYNC_SAMPLES} samples. t_device_rx MUST be taken when the "
            f"write arrives, not when the reply is composed -- the gap between "
            f"them is exactly the processing time this exchange exposes",
            samples=[ts["t_device_rx"] for _, ts in samples])


@check(id="control.time_sync_offset", section="9.7", phase="control",
       severity="OBSERVE", requires=("control",),
       title="Clock offset and round-trip delay")
async def control_time_sync_offset(s):
    samples = s.state.get("time_sync")
    if not samples:
        raise Skip("TIME_SYNC produced no samples")
    best = None
    for response, ts in samples:
        t1, t4 = response.t_write * 1e6, response.t_recv * 1e6
        delay = (t4 - t1) - (ts["t_device_tx"] - ts["t_device_rx"])
        offset = ((ts["t_device_rx"] - t1) + (ts["t_device_tx"] - t4)) / 2
        if best is None or delay < best[0]:
            best = (delay, offset, ts["processing_us"])
    delay, offset, processing = best
    # 9.7 -- keep the sample with the smallest delay: least time in flight
    # means least room for the two halves to differ.
    raise Observe(
        f"best of {len(samples)} samples: delay {delay / 1000:.2f} ms, device "
        f"clock offset {offset / 1000:.1f} ms, device processing "
        f"{processing} us",
        delay_us=round(delay), offset_us=round(offset), processing_us=processing)
