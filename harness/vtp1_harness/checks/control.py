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
#: Kept distinct so that finding it installed proves the refusal was a lie --
#: once there was a refusal for it to be a lie about, which is what
#: BUSY_PROBE_STATE is for.
BUSY_PROBE_ID = 0x7A1

#: Where `control.busy_when_outstanding` leaves what became of the request it
#: pipelined, for `control.busy_not_applied` to read.
#:
#: The two are halves of one exchange: the first writes the second request, the
#: second looks for its effect. Only the first can say whether that request was
#: ever pipelined at all -- against a device that answers before the host can
#: write again it was an ordinary conforming request, `ok` was the correct
#: answer, and installing it was correct too. Without this the second check
#: reads a rightly-installed subscription as a refusal that was a lie, and
#: fails a device for the one thing `control.busy_when_outstanding` says in the
#: same breath it could not test.
#:
#: What is recorded is what the timestamps said and not what the check meant
#: to do, for the reason `_overlap_excluded` gives: a note left on intent would
#: excuse a `busy` answered to a request that in the event overlapped nothing,
#: which §9 does not excuse. This note is only how the measurement reaches the
#: check that needs it -- the history records a status and a tag, not which
#: request was the pipelined one.
BUSY_PROBE_STATE = "busy_probe"


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
        first_response = None
    complete = second is not None and first_response is not None
    # The one thing two host timestamps can settle about §9's window, and they
    # settle it in one direction only.
    #
    # §9 owes a response until the device has SENT it. `t_recv` is the moment
    # the HOST's callback ran, which is later by however long the stack held
    # the delivery -- on macOS CoreBluetooth schedules that itself and tells
    # the application nothing about it. The second request's own journey runs
    # the same way: it reaches the device later than the moment recorded here.
    # Both unknowns lengthen the same gap and neither can be measured from
    # above the host stack, so:
    #
    #   written after the response ARRIVED  =>  written after it was SENT
    #
    # and an overlap can be EXCLUDED from a host. The converse does not hold at
    # any latency: a second request written before the response arrived may
    # still have reached a device that had already sent it, which is what a
    # conforming device answering inside its write handler does every time.
    # That is why the branch below that would need a proven overlap reports a
    # measurement instead of a verdict.
    overlap_excluded = complete and second.t_write > first_response.t_recv
    # Recorded before the cleanup below, which writes a request of its own and
    # can fail on a device that has stopped answering: what the pair did is
    # already known here, and `control.busy_not_applied` needs it whatever
    # happens next.
    if complete:
        s.state[BUSY_PROBE_STATE] = {"overlap_excluded": overlap_excluded,
                                     "status": second.status,
                                     "status_name": second.status_name}
    # Also before the verdicts, and not after them. The subscription is
    # installed the moment the device answers `ok`, whatever became of the
    # first request, so a cleanup that runs only on the paths reaching the end
    # of this check leaves a device that dropped the first response holding a
    # slot for the rest of the connection -- failing here, correctly, and then
    # failing `can.table_full` for a reason nothing in that report can explain.
    if s.has("can") and second is not None and second.ok:
        await _remove_busy_probe(s, c)
    if first_response is None:
        raise Fail("the first of two pipelined requests was never answered. A "
                   "device MUST respond to every request it applies")
    if second is None:
        raise Fail("the second of two pipelined requests was never answered. §9 "
                   "requires busy rather than silence: a device meeting a "
                   "client that pipelines has to have something true to say")
    if overlap_excluded:
        # The device answered the first before the second was written, so the
        # two were never outstanding together and there was nothing to detect.
        # A limit of testing from a host, not a finding.
        #
        # Tested BEFORE the status and not after. A `busy` reached this way is
        # not evidence the device got the rule right: nothing was outstanding
        # when that request was written, so it refused a conforming client and
        # `control.no_unprovoked_busy` reports it as one. Returning a pass here
        # because the status happened to read `busy` would credit a device for
        # the answer while it was answering the wrong question.
        #
        # It is a STRUCTURAL limit, and worth knowing before anyone tries to
        # make this check verdict reliably. ATT allows one outstanding request
        # per bearer, so a client cannot pipeline two Write Requests: the
        # second is illegal until the first's Write Response arrives, and a
        # device sends that as soon as its write handler returns. The window in
        # which `busy` is reachable is therefore the gap between that Write
        # Response and the moment the device SENDS the answer (SPEC.md 9) --
        # roughly one connection interval -- and a device that answers promptly
        # closes it before this harness can write into it. Reported by the
        # first outside implementer, who reached it only by holding responses
        # 300 ms behind a build-time fault.
        #
        # So this Observe is the honest outcome and not a gap to be plugged: a
        # device fast enough to be unverifiable here is a device behaving well.
        # Deepening the pipeline does not help -- a three-deep check has the
        # same window and would Observe on every device rather than only fast
        # ones.
        raise Observe(
            "the device answered the first request before the second was "
            "written, so nothing was ever pipelined and this could not be "
            "tested from here")
    if second.status == refdec.STATUS_VALUE["busy"]:
        # The device's own testimony that its slot was still occupied, which is
        # the only evidence there is that the window was open: nothing above
        # the host stack watched it open or close, and the device saying `busy`
        # is the device saying it was in it.
        return
    # And here the harness stops short of a verdict, which is the whole of what
    # this check gave up.
    #
    # A Fail on this branch has to assert that the device still owed the first
    # response when the second request reached it, and `overlap_excluded` above
    # is the argument that a host cannot assert it. What used to stand in for
    # it -- `second.t_write <= first_response.t_recv` -- is also true of a
    # conforming device that sent its answer inside its write handler and whose
    # delivery the host stack then held for one scheduler turn. That is not an
    # exotic shape; it is the ordinary one, and reading it as a violation
    # failed devices that had done exactly what §9 asks (issue #48).
    #
    # What is given up is real and worth naming: a device that applies a
    # pipelined request and answers `ok` is reported here rather than failed,
    # because from a host it leaves the same trace as a device that had already
    # answered. The neighbouring MUSTs are still verdicted -- silence above,
    # and a device that says `busy` and applies the request anyway by
    # `control.busy_not_applied` -- but this one needs the send timestamped. A
    # sniffer settles it; so would a `t_sent` field in `control_response`,
    # which is the sort of thing a later minor could add.
    raise Observe(
        f"the second of two pipelined requests was answered "
        f"{second.status_name}. §9 makes busy the one thing a device can say "
        f"while its single outstanding slot is occupied, and whether that "
        f"slot was still occupied cannot be told from here: the first "
        f"response had not reached the host, but a device that had already "
        f"SENT it owed nothing and was right to answer as it did",
        response=second.raw.hex())


async def _remove_busy_probe(s, c):
    """Take back the subscription the pipelined request installed.

    It is installed exactly when that request was answered `ok`, and against a
    device that answered the first request first, `ok` was the right answer to
    what was by then an ordinary conforming request. Nothing else removes it:
    `control.busy_not_applied` probes with an unsubscribe only where there was
    a refusal to contradict, and where there was none it does not write at all.
    A subscription left behind holds a slot for the rest of the connection, and
    `can.table_full` a few checks later is counting slots.

    Nothing is caught here. This is housekeeping, but it is housekeeping done
    by writing a request, and §9's MUSTs attach to the request and not to the
    reason it was written: a device that never answers it, or echoes the wrong
    opcode, has violated §9 whatever this check was about, and `Runner._one`
    already reports both as a failure with that rationale attached. Swallowing
    them here would leave the run reporting an Observe and a Skip, and
    `conforms` true, over a MUST the harness had watched go by.
    """
    removal = await c.unsubscribe_can(BUSY_PROBE_ID)
    if removal.ok:
        return
    s.note(f"the subscription this harness pipelined was installed, "
           f"correctly, and the unsubscribe taking it back was answered "
           f"{removal.status_name}; one subscription slot may be occupied "
           f"for the rest of this connection")


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
    """The second half of `control.busy_when_outstanding`'s exchange.

    It can only run where that check produced a refusal, so it reads what that
    check recorded before it writes anything. An installed subscription is only
    evidence of anything if the request that installed it was answered `busy`:
    where the answer was `ok` nothing was refused, and a device that had
    already sent its first response is doing exactly what §9 asks when the
    probe finds the subscription there. Reading the table without that question
    first fails a conforming device, and fails it most reliably on the
    promptest ones.

    The status gate is what carries this, and the overlap gate above it is kept
    for what it says rather than for what it excludes: whether the requests
    overlapped cannot be settled from a host in the direction that would
    matter here (see `control.busy_when_outstanding`), and a device that was
    never refused has no refusal for the table to contradict.
    """
    c = _control(s)
    pipelined = s.state.get(BUSY_PROBE_STATE)
    if pipelined is None:
        raise Skip("the pipelined exchange control.busy_when_outstanding "
                   "writes did not complete -- one of the two requests went "
                   "unanswered, which that check reports -- so there is no "
                   "refusal to hold to")
    if pipelined["overlap_excluded"]:
        raise Skip("the device answered the first request before the second "
                   "was written, so nothing was ever pipelined: that request "
                   "was an ordinary conforming one, nothing was owed when it "
                   "arrived, and applying it was right. There is no refusal "
                   "here for anything to have contradicted")
    if pipelined["status"] != refdec.STATUS_VALUE["busy"]:
        raise Skip(f"the pipelined request was answered "
                   f"{pipelined['status_name']}, not busy -- which "
                   f"control.busy_when_outstanding reports. Nothing was "
                   f"refused, so nothing can have been applied despite a "
                   f"refusal")
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


@check(id="control.no_busy_for_conforming_client", section="9",
       phase="control", severity="MUST", requires=("control",),
       title="A client that writes on arrival is never answered busy")
async def control_no_busy_for_conforming_client(s):
    """The other half of `control.busy_when_outstanding`, and the half §9 is
    written for.

    That check pipelines and requires `busy`. This one does what §9 tells a
    client to do -- one request outstanding, the next written as soon as the
    response arrives -- and requires that `busy` never comes back. A device
    that answers it is refusing a client that did nothing wrong.

    The boundary is §9's: a response is owed until the device has SENT it, not
    until its confirmation. A device that kept owing until the confirmation
    refuses exactly this client, and would look correct to every other check
    in this file.

    A pass is worth less than a failure here, and deliberately so. The window
    this aims at is between the response arriving at the host and the host's
    stack emitting the ATT confirmation, and on macOS CoreBluetooth confirms on
    its own schedule and never says when. If it confirms before this coroutine
    is woken, the next write lands after the confirmation and a device with the
    wrong boundary answers correctly anyway. So a pass says the device did not
    refuse a client writing this fast; it does not say the boundary is right.
    A failure says the boundary is wrong, and says it unambiguously. Same class
    of limit as the Observe branches of `control.busy_when_outstanding`, and
    like those it wants a sniffer to settle rather than a better host.
    """
    c = _control(s)
    # TIME_SYNC: owned by no capability, so every Control device answers it,
    # and §9.4 lists it as safe to repeat -- each attempt is a fresh reading
    # and nothing on the device changes. A device that does not implement it
    # is control.time_sync's finding, not this one's; here it only has to be
    # something the device answers, so fall back to the probe opcode.
    opcode = refdec.OPCODE["TIME_SYNC"]
    unsupported = refdec.STATUS_VALUE["unsupported_opcode"]
    rounds = 40
    for i in range(rounds):
        try:
            response = await c.request(opcode)
        except ControlTimeout:
            raise Fail(f"round {i + 1} of {rounds} was never answered. A "
                       f"device MUST respond to every request it "
                       f"applies") from None
        if i == 0 and response.status == unsupported:
            opcode = UNALLOCATED_OPCODE
            continue
        if response.status == refdec.STATUS_VALUE["busy"]:
            raise Fail(
                f"round {i + 1} of {rounds} was answered busy. Every request "
                f"here was written after the previous response had ARRIVED, "
                f"with one outstanding throughout -- which is what §9 tells a "
                f"client to do. A response is owed until the device has SENT "
                f"it, and a device still owing one the client has already "
                f"received refuses a client that waited exactly as long as it "
                f"was told to, and refuses the retry the same way",
                response=response.raw.hex(), round=i + 1)


def _overlap_excluded(response, history):
    """True if this request certainly overlapped nothing that was still owed.

    §9 permits `busy` in exactly one situation, and this is that situation
    ruled out, stated in the two timestamps the correlation layer already
    records: a request written after every earlier response had ARRIVED was
    written after every one of them had been SENT, whoever wrote it and
    whatever they meant to test.

    The same ruler `control.busy_when_outstanding` reads, and read in the
    direction it holds in. `t_recv` is the host's callback and §9's boundary
    is the device's send, which is earlier by an amount nothing above the host
    stack can measure, so arrival proves the send and non-arrival proves
    nothing. Exclusion is therefore sound and inclusion is not, and this is
    only ever asked in the excluding direction: `control.no_unprovoked_busy`
    reports the refusals this returns True for and says nothing about the
    rest.

    The error that remains runs the safe way. A delivery the host held onto
    pushes `t_recv` later, which returns False for a request that in fact
    overlapped nothing, which lets a `busy` that genuinely refused a conforming
    client go unreported. That is a finding missed rather than a device failed
    for something it did not do -- the direction to be wrong in, and the same
    trade `control.busy_when_outstanding` makes on its `ok` branch.

    Derived from the traffic rather than from a note left by the check that
    pipelined. A note has to be left at the right moment to be right --
    `control.busy_when_outstanding` writes its second request intending to
    pipeline, and on a device fast enough to answer the first in between, it
    does not manage to. A note left on intent would exempt that refusal; the
    timestamps say it overlapped nothing, which is what §9 actually asks.
    """
    return not any(other is not response
                   and other.t_write < response.t_write < other.t_recv
                   for other in history)


@check(id="control.no_unprovoked_busy", section="9", phase="transport",
       severity="MUST", requires=("control",),
       title="No busy was answered to a request that did not pipeline")
async def control_no_unprovoked_busy(s):
    """Every other check in this run, read back as a witness for §9.

    `control.busy_when_outstanding` is the only thing in this harness that
    writes a request before the previous one is answered. Every other request
    -- every subscribe, every rate, every probe, across every phase -- is a
    conforming single-outstanding client, so a `busy` anywhere else in the
    history is a device refusing a client that did nothing wrong. The traffic
    is already recorded, so this costs no writes of its own.

    Which requests certainly overlapped nothing is read out of the timestamps
    by `_overlap_excluded` rather than declared by the check that pipelined, so
    this holds that check to the same rule as every other: a request it *meant*
    to pipeline, but which a fast device answered before it was written, is a
    conforming write like any other and a `busy` answered to it is reported
    here.

    Placed in the `transport` phase rather than `control` so that it reads a
    history with the whole interrogation in it. Requests made in the reconnect
    phase run after this and are not covered.
    """
    c = _control(s)
    busy = refdec.STATUS_VALUE["busy"]
    unprovoked = [r for r in c.history
                  if r.status == busy and _overlap_excluded(r, c.history)]
    if not unprovoked:
        return
    named = ", ".join(f"{r.opcode_name} tag {r.tag}" for r in unprovoked[:6])
    if len(unprovoked) > 6:
        named += f", and {len(unprovoked) - 6} more"
    raise Fail(
        f"{len(unprovoked)} request(s) that did not pipeline were answered "
        f"busy: {named}. Each was written after the previous response had "
        f"arrived, so §9's one-outstanding rule was kept; a device owing a "
        f"response past the moment it sent it refuses a conforming client",
        responses=[r.raw.hex() for r in unprovoked[:6]])


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


#: Identifiers used only by the identity checks below. Distinct from PROBE_ID
#: so that a device that folds two subscriptions into one leaves the evidence
#: under a name no other check installs.
IDENTITY_ID = 0x7B0
IDENTITY_SIBLING = 0x7B1


@check(id="can.update_in_place_when_full", section="9.1", phase="control",
       severity="MUST", requires=("control", "can"),
       title="Re-installing an existing id and mask on a full table is answered ok")
async def can_update_in_place_when_full(s):
    """§9.1's update-in-place is not a courtesy the table's capacity revokes.

    A device that checks for a free slot before it checks for an existing entry
    refuses exactly the client §4 forces on it: one that reprograms
    unconditionally on every connection, whose table is full *because* it holds
    what is being re-written. can.subscribe_idempotent tests the same rule with
    room to spare, which is the case that does not fail.
    """
    c = _control(s)
    slots = s.info["can_subscription_slots"]
    if slots > 64:
        raise Skip(f"the device declares {slots} slots; filling them would take "
                   f"longer than this check is worth")
    base = 0x280
    try:
        await c.request(refdec.OPCODE["CAN_RESET"])
        for i in range(slots):
            response = await c.subscribe_can(base + i)
            if not response.ok:
                raise Skip(f"subscription {i + 1} of {slots} was answered "
                           f"{response.status_name}, so the table never filled "
                           f"and there is no full table to re-install into")
        again = await c.subscribe_can(base)
        if not again.ok:
            raise Fail(
                f"re-installing an (id, mask) the full table already holds was "
                f"answered {again.status_name}. §9.1 updates it in place and "
                f"creates no subscription, so no slot has to be free for it — "
                f"and this is the request a client that reprograms on every "
                f"connection sends",
                response=again.raw.hex())
    finally:
        await c.request(refdec.OPCODE["CAN_RESET"])
        s.state["installed"] = {}
        s.state.pop("probe_installed", None)


@check(id="can.transmission_bits_ignored", section="9.1", phase="control",
       severity="MUST", requires=("control", "can"),
       title="Bits 30 and 31 take no part in a subscription's identity")
async def can_transmission_bits_ignored(s):
    """§9.1 — CAN FD and RTR describe how a frame was transmitted, not which
    frame it is. A device that keeps them in what it stores installs a
    subscription its client can never name again: the removal it writes is a
    different name than the one the device recorded."""
    c = _control(s)
    decorated = IDENTITY_ID | (1 << 30) | (1 << 31)
    try:
        installed = await c.subscribe_can(decorated)
        if not installed.ok:
            raise Fail(
                f"a subscription whose id carries bits 30 and 31 was answered "
                f"{installed.status_name}. §9.1 ignores both bits in `id` and "
                f"in `mask` rather than refusing them",
                response=installed.raw.hex())
        removal = await c.unsubscribe_can(IDENTITY_ID)
        if not removal.ok:
            raise Fail(
                f"a subscription installed with bits 30 and 31 set could not "
                f"be removed by the same identifier without them "
                f"({removal.status_name}). Those bits are not part of the "
                f"identity, so a client that sets them has installed something "
                f"it can never remove",
                response=removal.raw.hex())
        # And the other direction: the bits set on the REMOVAL of a
        # subscription installed without them.
        again = await c.subscribe_can(IDENTITY_ID)
        if not again.ok:
            raise Skip(f"could not re-install the probe: {again.status_name}")
        decorated_removal = await c.unsubscribe_can(
            decorated, mask=refdec.MASK_EXACT | 0xC0000000)
        if not decorated_removal.ok:
            raise Fail(
                f"CAN_UNSUBSCRIBE carrying bits 30 and 31 was answered "
                f"{decorated_removal.status_name} for a subscription that is "
                f"installed. §9.1 ignores both bits on the way in and on the "
                f"way out",
                response=decorated_removal.raw.hex())
    finally:
        # Whatever this left installed -- including under a name only a device
        # that kept bits 30 and 31 has -- goes now. A subscription leaked past
        # here is forwarded in the streams phase and reported there as a frame
        # no subscription covers.
        await c.request(refdec.OPCODE["CAN_RESET"])


@check(id="can.identity_is_the_pair", section="9.1", phase="control",
       severity="MUST", requires=("control", "can", "masked_subscriptions"),
       title="Two subscriptions differing only in ignored id bits are two subscriptions")
async def can_identity_is_the_pair(s):
    """§9.1 — the identity is the `(id, mask)` pair the client wrote, not
    `id & mask`. The two installed here match exactly the same frames, and a
    device that folds them silently discards the second install: the client's
    own removal then names nothing, and a slot it believes it holds is gone."""
    c = _control(s)
    slots = s.info["can_subscription_slots"]
    if slots < 2:
        raise Skip(f"the device declares {slots} subscription slot(s); two "
                   f"distinct subscriptions do not fit, and refusing the "
                   f"second table_full is the right answer rather than the "
                   f"folding this looks for")
    mask = refdec.MASK_EXACT & ~0xF
    try:
        first = await c.subscribe_can(IDENTITY_ID, mask=mask)
        if not first.ok:
            raise Skip(f"the first subscription was answered "
                       f"{first.status_name}")
        second = await c.subscribe_can(IDENTITY_SIBLING, mask=mask)
        if not second.ok:
            raise Fail(
                f"a subscription whose id differs from an installed one only "
                f"in bits their shared mask ignores was answered "
                f"{second.status_name}. §9.1 compares the pair, so these are "
                f"two subscriptions",
                response=second.raw.hex())
        # Both accepted is not both installed: a device that folded them
        # answers ok twice and holds one entry. The arithmetic says which,
        # when the table is small enough to fill -- with two of `slots` spent
        # here, exactly `slots - 2` more may be accepted.
        if slots <= 64:
            for i in range(slots - 2):
                filler = await c.subscribe_can(0x2C0 + i)
                if not filler.ok:
                    # Fewer entries than Info declares is can.table_full's
                    # arithmetic, and failing it here would name the wrong
                    # rule: this check can only speak about the pair.
                    raise Skip(f"the table refused subscription {i + 3} of "
                               f"{slots} ({filler.status_name}), so the "
                               f"capacity for counting slots is not there "
                               f"(can.table_full owns that)")
            over = await c.subscribe_can(0x2C0 + slots)
            if over.ok:
                raise Fail(
                    f"a {slots + 1}th subscription was accepted, so one of the "
                    f"two that differ only in ignored id bits never took a "
                    f"slot: §9.1 compares the pair, and folding them by "
                    f"`id & mask` leaves the client a slot short of what Info "
                    f"promises", response=over.raw.hex())
        removals = []
        for can_id in (IDENTITY_ID, IDENTITY_SIBLING):
            removals.append((can_id,
                             await c.unsubscribe_can(can_id, mask=mask)))
        missing = [(can_id, r) for can_id, r in removals if not r.ok]
        if missing:
            can_id, response = missing[0]
            raise Fail(
                f"0x{can_id:X} under mask 0x{mask:08X} was installed and then "
                f"answered {response.status_name} on removal. Both installs "
                f"were accepted, so a device that folded them by `id & mask` "
                f"has one entry under a name the client never wrote",
                response=response.raw.hex())
    finally:
        # As above: nothing this check installed survives it, whatever went
        # wrong in the middle.
        await c.request(refdec.OPCODE["CAN_RESET"])


@check(id="can.unknown_mode_refused", section="6.8", phase="control",
       severity="MUST", requires=("control", "can"), adversarial=True,
       title="A subscription naming an unassigned mode is refused and takes no slot")
async def can_unknown_mode_refused(s):
    """§6.8 — modes 2 and 3 were assigned by pre-1.0 drafts and remain
    unassigned, and §11.4 makes an unknown enum unknown rather than a default.
    The slot matters as much as the status: a device that answers `bad_params`
    having already installed the entry has a table its client cannot account
    for."""
    c = _control(s)
    # Identifiers inside the standard range, so a device that validates what
    # it is asked to match refuses the MODE rather than the identifier.
    for i, mode in enumerate((2, 3, 0xFF)):
        can_id = 0x7C0 + i
        response = await c.subscribe_can(can_id, mode=mode)
        if response.status != refdec.STATUS_VALUE["bad_params"]:
            if response.ok:
                await c.unsubscribe_can(can_id)
            raise Fail(
                f"a subscription naming mode {mode} was answered "
                f"{response.status_name}, not bad_params. §6.8 leaves the "
                f"value unassigned and §11.4 forbids decoding it as a "
                f"default — a client asking for a mode it read in a later "
                f"specification MUST be told no",
                response=response.raw.hex())
        removal = await c.unsubscribe_can(can_id)
        if removal.status != refdec.STATUS_VALUE["unknown_subscription"]:
            await c.unsubscribe_can(can_id)
            raise Fail(
                f"a subscription refused bad_params for naming mode {mode} was "
                f"nevertheless installed: removing it was answered "
                f"{removal.status_name} rather than unknown_subscription. A "
                f"refused request MUST NOT take effect (§9.4)",
                response=removal.raw.hex())


@check(id="can.no_rate_admission", section="9.3", phase="control",
       severity="MUST", requires=("control", "can", "masked_subscriptions"),
       title="A subscription covering the whole bus is not refused on rate grounds")
async def can_no_rate_admission(s):
    """§9.3 — a device MUST NOT refuse a CAN subscription on rate grounds. A
    mask of zero at `every_frame` is the largest load a client can ask for and
    the one a device is most tempted to predict, and the prediction cannot be
    made: what the bus carries is not knowable at install time. It admits, and
    sheds what it cannot forward (§6.3)."""
    c = _control(s)
    try:
        await c.request(refdec.OPCODE["CAN_RESET"])
        response = await c.subscribe_can(0, mask=0)
        if response.status == refdec.STATUS_VALUE["rate_exceeded"]:
            raise Fail(
                "a catch-all subscription at every_frame was refused "
                "rate_exceeded. §9.3 forbids refusing a CAN subscription on "
                "rate grounds: the load it produces is not knowable at "
                "install, so a device admits and sheds, reporting the loss in "
                "`dropped` with the shedding flag set",
                response=response.raw.hex())
        if not response.ok:
            raise Fail(
                f"a catch-all subscription into an empty table was answered "
                f"{response.status_name}. `can_subscription_slots` is a count "
                f"of entries, and this is one entry",
                response=response.raw.hex())
    finally:
        await c.unsubscribe_can(0, mask=0)
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
