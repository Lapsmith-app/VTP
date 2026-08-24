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

#: A second identifier, used only by the request a device must refuse `busy`.
#: Kept distinct so that finding it installed proves the refusal was a lie.
BUSY_PROBE_ID = 0x7A1


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
    problems = []
    # Every opcode this device actually owns, given a parameter block of the
    # wrong length. Derived from the schema so an opcode added in a later minor
    # is covered the moment it is declared there.
    for name, capability in refdec.OPCODE_CAPABILITY.items():
        if capability is not None and not s.has(capability):
            continue
        wanted = refdec.OPCODE_PARAM_SIZE[name]
        params = b"\x00" * (wanted + 1)
        # For a variadic opcode the fixed part is a minimum, not the whole
        # request: the surplus byte here is a pid the count byte (zero, in
        # this all-zero block) does not declare, which is §15.4's own
        # length-mismatch refusal.
        label = (f"{name} with {wanted + 1} parameter byte(s) where "
                 + (f"the fixed part is {wanted}"
                    if name in refdec.OPCODE_VARIADIC
                    else f"{wanted} {'is' if wanted == 1 else 'are'} defined"))
        try:
            response = await c.request(refdec.OPCODE[name], params)
        except ControlTimeout:
            problems.append(f"{label}: no response")
            continue
        if response.status == refdec.STATUS_VALUE["unsupported_opcode"]:
            # §9 -- availability is decided before parameters, so this answer
            # means the device does not implement the opcode at all. That is
            # the opcode-capability check's business, not this one's.
            continue
        if response.ok:
            problems.append(f"{label}: answered ok")
        elif response.detail:
            problems.append(f"{label}: refused, but carried a detail")
    if problems:
        raise Fail("; ".join(problems))


@check(id="control.opcode_capability", section="9", phase="control",
       severity="MUST", requires=("control",), adversarial=True,
       title="An opcode the device does not own is refused before its parameters")
async def control_opcode_capability(s):
    c = _control(s)
    absent = [(name, capability)
              for name, capability in refdec.OPCODE_CAPABILITY.items()
              if capability is not None and not s.has(capability)]
    if not absent:
        raise Skip("this device owns every opcode in this version")
    problems = []
    for name, capability in absent:
        # Deliberately the wrong length. §9 fixes the order: availability
        # first, parameters second, so this MUST still be unsupported_opcode.
        # The two refusals mean different things to a client -- "not on this
        # device, ever" against "try again with better arguments" -- and one
        # that gets them the wrong way round either retries forever or gives up
        # on a device that would have worked.
        params = b"\x00" * (refdec.OPCODE_PARAM_SIZE[name] + 1)
        try:
            response = await c.request(refdec.OPCODE[name], params)
        except ControlTimeout:
            problems.append(f"{name}: no response")
            continue
        if response.status != refdec.STATUS_VALUE["unsupported_opcode"]:
            problems.append(
                f"{name} needs {capability!r}, which this device has not "
                f"declared, and a malformed one was answered "
                f"{response.status_name}")
    if problems:
        raise Fail("; ".join(problems))


@check(id="control.busy_when_outstanding", section="9", phase="control",
       severity="MUST", requires=("control",), adversarial=True,
       title="A request arriving while one is owed is answered busy, and not applied")
async def control_busy_when_outstanding(s):
    c = _control(s)
    # This check deliberately breaks the client rule it is testing the other
    # half of: §9 says a client MUST have at most one request outstanding, and
    # the only way to see what a device does when one pipelines anyway is to
    # pipeline. Nothing else in this harness writes a second request before the
    # first is answered.
    first_tag, first_future = await c.send(UNALLOCATED_OPCODE)
    second = await _pipelined_second(s, c)
    try:
        first_response = await c.await_response(
            UNALLOCATED_OPCODE, first_tag, first_future, timeout=5.0)
    except ControlTimeout:
        raise Fail("the first of two pipelined requests was never answered. A "
                   "device MUST respond to every request it applies") from None
    if second is None:
        raise Fail("the second of two pipelined requests was never answered. §9 "
                   "requires busy rather than silence: a device meeting a "
                   "client that pipelines has to have something true to say")
    if second.status == refdec.STATUS_VALUE["busy"]:
        return
    if second.t_write > first_response.t_recv:
        # The device answered the first before the second was written, so the
        # two were never outstanding together and there was nothing to detect.
        # A limit of testing from a host, not a finding.
        raise Observe(
            "the device answered the first request before the second was "
            "written, so nothing was ever pipelined and this could not be "
            "tested from here")
    raise Fail(
        f"a request written while the device still owed a response was answered "
        f"{second.status_name}, not busy. §9 makes busy the one thing a device "
        f"can say when its single outstanding slot is occupied -- the "
        f"alternatives §9.6 forbids are silence, and applying a request it "
        f"cannot answer", response=second.raw.hex())


async def _pipelined_second(s, c):
    """Write a second request immediately, and return whatever answers it.

    Chosen to be observable: if the device applies it despite owing a response,
    the subscription table says so and `control.busy_not_applied` finds it.
    """
    if s.has("can"):
        opcode = refdec.OPCODE["CAN_SUBSCRIBE"]
        params = struct.pack("<IBH", BUSY_PROBE_ID, 0, 0)
    else:
        opcode, params = UNALLOCATED_OPCODE, b""
    tag, future = await c.send(opcode, params)
    try:
        return await c.await_response(opcode, tag, future, timeout=5.0)
    except ControlTimeout:
        return None


@check(id="control.busy_not_applied", section="9", phase="control",
       severity="MUST", requires=("control", "can"), adversarial=True,
       title="A request refused busy did not take effect")
async def control_busy_not_applied(s):
    c = _control(s)
    # §9.1 makes (id, mask) the subscription's identity, so removal is also the
    # probe: an install that was refused busy leaves nothing to remove.
    probe = await c.unsubscribe_can(BUSY_PROBE_ID)
    if probe.ok:
        # The failure the whole lifecycle is built to prevent: the client is
        # told the request was refused, the device did it anyway, and the two
        # disagree about the device's state for as long as the link lasts.
        raise Fail(
            "a subscription written while the device owed a response was "
            "installed. A device MUST NOT apply a request it answers busy",
            response=probe.raw.hex())
    if probe.status != refdec.STATUS_VALUE["unknown_subscription"]:
        raise Skip(f"the probe unsubscribe was answered {probe.status_name}, "
                   f"so whether the busy request took effect cannot be told")


# ---------------------------------------------------------------------------
# CAN subscriptions
# ---------------------------------------------------------------------------

@check(id="can.subscribe_ok", section="9.1", phase="control", severity="MUST",
       requires=("control", "can"),
       title="Installing a subscription is answered ok with no detail")
async def can_subscribe_ok(s):
    c = _control(s)
    response = await c.subscribe_can(PROBE_ID)
    if not response.ok:
        raise Fail(f"CAN_SUBSCRIBE was answered {response.status_name}",
                   response=response.raw.hex())
    if response.detail:
        raise Fail(f"CAN_SUBSCRIBE answered ok with {len(response.detail)} "
                   f"detail byte(s); §9 gives it none",
                   detail=response.detail.hex())
    s.state["probe_installed"] = True
    s.state.setdefault("installed", {})[PROBE_ID] = refdec.MASK_EXACT


@check(id="can.subscribe_idempotent", section="9.1", phase="control",
       severity="MUST", requires=("control", "can"),
       title="Re-installing the same id and mask updates in place")
async def can_subscribe_idempotent(s):
    c = _control(s)
    if not s.state.get("probe_installed"):
        raise Skip("the first subscription did not install")
    response = await c.subscribe_can(PROBE_ID, mode=0, arg=0)
    if not response.ok:
        # table_full here is the specific failure §9.1 rules out: the same
        # (id, mask) MUST update in place, so a client that reprograms
        # unconditionally on every connection -- which §4 already forces on
        # it -- can never exhaust the table.
        raise Fail(f"re-installing an identical subscription was answered "
                   f"{response.status_name}; the same id and mask MUST update "
                   f"in place rather than consume a slot",
                   response=response.raw.hex())
    # `ok` alone cannot tell an update-in-place from a device that quietly
    # created a second entry -- both answer ok. The table can: remove the
    # subscription once, and a device that updated in place has nothing left
    # under that name, so a second removal MUST find nothing. (Whether the
    # duplicate consumed a physical SLOT is can.table_full's arithmetic.)
    first = await c.unsubscribe_can(PROBE_ID)
    if not first.ok:
        raise Fail(f"removing the twice-installed subscription was answered "
                   f"{first.status_name}", response=first.raw.hex())
    second = await c.unsubscribe_can(PROBE_ID)
    if second.ok:
        s.state.pop("probe_installed", None)
        s.state.get("installed", {}).pop(PROBE_ID, None)
        raise Fail(
            "the subscription came out of the table twice, so installing the "
            "same id and mask twice created two entries. §9.1 makes "
            "(id, mask) the subscription's identity: the second install MUST "
            "update the first in place",
            response=second.raw.hex())
    # Put the table back the way can.subscribe_ok left it, for the checks
    # that rely on the probe being installed.
    redo = await c.subscribe_can(PROBE_ID)
    if not redo.ok:
        s.state.pop("probe_installed", None)
        s.state.get("installed", {}).pop(PROBE_ID, None)
        raise Skip(f"could not re-install the probe subscription afterwards: "
                   f"{redo.status_name}")


@check(id="can.unknown_subscription", section="9.1", phase="control",
       severity="MUST", requires=("control", "can"), adversarial=True,
       title="Unsubscribing an id and mask that name nothing is refused")
async def can_unknown_subscription(s):
    c = _control(s)
    # The probe id under a mask nothing here ever installs: the same id under
    # a different mask is a different subscription (§9.1).
    response = await c.unsubscribe_can(PROBE_ID, mask=refdec.MASK_EXACT & ~0x10)
    if response.status != refdec.STATUS_VALUE["unknown_subscription"]:
        raise Fail(
            f"unsubscribing an id and mask that were never installed was "
            f"answered {response.status_name} rather than unknown_subscription. "
            f"A client that cannot verify what it removed has not removed it",
            response=response.raw.hex())


@check(id="can.table_full", section="9.1", phase="control", severity="MUST",
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
                installed[can_id] = refdec.MASK_EXACT
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
            # 9.3 -- the prediction this status once implied cannot be made,
            # and the specification removed the rule rather than patch it again.
            raise Fail(
                "a CAN subscription was refused rate_exceeded. §9.3 forbids "
                "refusing one on rate grounds: a device admits, and sheds if it "
                "must, reporting the loss in dropped",
                response=first.raw.hex())
        if first.status != refdec.STATUS_VALUE["table_full"]:
            raise Fail(f"the {len(installed) + 1}th subscription against "
                       f"{slots} slot(s) was answered {first.status_name}, not "
                       f"table_full", response=first.raw.hex())
        # EXACTLY at capacity, not merely eventually. Every distinct (id,
        # mask) this connection installed is accounted for here, so a refusal
        # with fewer than `slots` of them accepted means a slot went to
        # something the client never asked to keep -- a duplicate install
        # that consumed a second entry, or a ceiling one short of Info's
        # declaration. Either way Info and the table disagree, and the table
        # full arriving "eventually" is what let both hide.
        if len(installed) != slots:
            raise Fail(
                f"table_full arrived with {len(installed)} distinct "
                f"subscription(s) accepted against a declared {slots}. Info "
                f"promises {slots} slots and §9.1 gives every one a distinct "
                f"(id, mask); {slots - len(installed)} slot(s) are held by "
                f"something this client never installed",
                response=first.raw.hex())
    finally:
        # Leave the table as this phase found it, so the stream checks are not
        # reading a bus the harness saturated.
        await c.request(refdec.OPCODE["CAN_RESET"])
        s.state["installed"] = {}
        s.state.pop("probe_installed", None)


@check(id="control.applies_only_if_answerable", section="9.4", phase="control",
       severity="MUST", requires=("control", "can"), adversarial=True,
       title="A request whose response cannot be delivered is not applied")
async def control_applies_only_if_answerable(s):
    c = _control(s)
    install = await c.subscribe_can(PROBE_ID)
    if not install.ok:
        raise Skip("could not install a subscription to test against")
    s.state.setdefault("installed", {})[PROBE_ID] = refdec.MASK_EXACT

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

    # §9.1 makes removal by (id, mask) the probe for whether the reset took
    # effect: ok means the probe subscription was still installed.
    probe = await c.unsubscribe_can(PROBE_ID)
    survived = probe.ok
    if survived:
        s.state.get("installed", {}).pop(PROBE_ID, None)
    else:
        s.state["installed"] = {}
    answered = any(r.tag == 0x42 for r in c.history[before:])

    if refused_at_att:
        raise Observe("the device refused the write outright while indications "
                      "were disabled, so the request was never dispatched")
    if survived:
        return                                  # not applied: the rule holds
    if answered:
        # The device held the request until its answer could be delivered and
        # then applied it. Deliverability was still decided before dispatch,
        # which is what §9.4 asks for.
        raise Observe("the request was held until indications returned, then "
                      "applied and answered")
    raise Fail(
        "CAN_RESET took effect while indications were disabled and was never "
        "answered. §9.4 makes deliverability a precondition of dispatch: a "
        "client that retries a request it believes was lost applies it twice")


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------

@check(id="control.rate_ceiling", section="9.6", phase="control", severity="MUST",
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


@check(id="control.rate_readback", section="9.6", phase="control", severity="MUST",
       requires=("control",),
       title="A rate that was accepted is the rate Info then reports")
async def control_rate_readback(s):
    c = _control(s)
    cases = [(name, refdec.OPCODE[f"{role.upper()}_SET_RATE"],
              f"{role}_rate_hz", f"{role}_max_rate_hz")
             for role, name in (("gps", "GPS_SET_RATE"), ("imu", "IMU_SET_RATE"))
             if s.has(role)]
    if not cases:
        raise Skip("this device declares neither GPS nor IMU")
    problems, tried = [], []
    for name, opcode, current_field, ceiling_field in cases:
        before, ceiling = s.info[current_field], s.info[ceiling_field]
        if ceiling == before or not ceiling:
            continue
        response = await c.request(opcode, struct.pack("<H", ceiling))
        if response.status in (refdec.STATUS_VALUE["unsupported_opcode"],
                               refdec.STATUS_VALUE["bad_params"]):
            # §9.6 -- a device MAY support only a discrete set of rates, and
            # refusing one it does not support is exactly right.
            continue
        if not response.ok:
            problems.append(f"{name} at {ceiling} Hz, its own declared "
                            f"maximum, was answered {response.status_name}")
            continue
        tried.append(name)
        again = refdec.decode("info", await s.transport.read(refdec.CHAR["info"]))
        if again[current_field] != ceiling:
            # The response carries no detail, so Info is the only statement of
            # what was applied. Answering ok for a rate the device did not adopt
            # is §1.1's plausible wrong value: the client believes it is getting
            # 25 Hz, the timestamps say otherwise, and nothing connects the two.
            problems.append(
                f"{name} answered ok for {ceiling} Hz and Info then reports "
                f"{again[current_field]} Hz. A device MUST NOT silently apply "
                f"the nearest rate it can manage")
        await c.request(opcode, struct.pack("<H", before))
    if problems:
        raise Fail("; ".join(problems))
    if not tried:
        raise Skip("this device is already at its declared maximum rate, or "
                   "supports no other rate to move to and back")


# ---------------------------------------------------------------------------
# TIME_SYNC
# ---------------------------------------------------------------------------

TIME_SYNC_SAMPLES = 6


@check(id="control.time_sync", section="9.5", phase="control", severity="MUST",
       requires=("control",),
       title="TIME_SYNC carries two distinct readings of one clock")
async def control_time_sync(s):
    c = _control(s)
    first = await c.request(refdec.OPCODE["TIME_SYNC"])
    if first.status == refdec.STATUS_VALUE["unsupported_opcode"]:
        # §9 -- TIME_SYNC has no owning capability. It is about the clock,
        # which every device has, and reaching it at all means Control is
        # live, so there is no device for which this answer is correct. This
        # used to skip, which is the one outcome that reports a MUST nobody
        # implemented as a MUST nobody needed: a device with no TIME_SYNC
        # passed a clean run, and the client left holding it has no way to
        # bound its own clock error against §8.1's device clock.
        raise Fail(
            "TIME_SYNC was answered unsupported_opcode. It has no owning "
            "capability (§9): a device whose Control characteristic answers at "
            "all MUST implement it, and without it a client cannot bound the "
            "error in its own view of the device clock",
            response=first.raw.hex())
    samples = []
    for _ in range(TIME_SYNC_SAMPLES):
        response = await c.request(refdec.OPCODE["TIME_SYNC"])
        if not response.ok:
            raise Fail(f"TIME_SYNC was answered {response.status_name}",
                       response=response.raw.hex())
        samples.append((response, _detail(response, "time_sync")))
    s.state["time_sync"] = samples

    if all(ts["processing_us"] == 0 for _, ts in samples):
        # 9.5 is explicit about this one: a device that reads its clock once and
        # reports it as both timestamps has silently implemented the
        # single-timestamp form while appearing to implement this one, and the
        # client's whole error bound goes with it.
        raise Fail(
            f"t_device_rx and t_device_tx were identical in all "
            f"{TIME_SYNC_SAMPLES} samples. t_device_rx MUST be taken when the "
            f"write arrives, not when the reply is composed -- the gap between "
            f"them is exactly the processing time this exchange exposes",
            samples=[ts["t_device_rx"] for _, ts in samples])


@check(id="control.time_sync_offset", section="9.5", phase="control",
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
    # 9.5 -- keep the sample with the smallest delay: least time in flight
    # means least room for the two halves to differ.
    raise Observe(
        f"best of {len(samples)} samples: delay {delay / 1000:.2f} ms, device "
        f"clock offset {offset / 1000:.1f} ms, device processing "
        f"{processing} us",
        delay_us=round(delay), offset_us=round(offset), processing_us=processing)
