"""SPEC.md §9.9 -- what a device knows about its own supply.

One opcode, three independently valid fields, and a rule no byte vector can
reach: a device that declares the capability and then reports nothing valid has
said what a device without the capability says by not declaring it.
"""
from .. import refdec
from ..session import ControlTimeout
from . import Fail, Observe, Skip, check

SOURCE_NAMES = refdec.enum_values("power_source")


def _control(s):
    if s.control is None:
        raise Skip("this device does not declare the control capability")
    return s.control


def _power(s):
    power = s.state.get("power")
    if power is None:
        raise Skip("GET_POWER returned nothing to check")
    return power


@check(id="power.get_power", section="9.9", phase="control", severity="MUST",
       requires=("power",),
       title="GET_POWER is answered with a power_state record")
async def power_get_power(s):
    c = _control(s)
    try:
        response = await c.request(refdec.OPCODE["GET_POWER"])
    except ControlTimeout:
        raise Fail("GET_POWER went unanswered. §9 requires a device to respond "
                   "to every request it applies") from None
    if not response.ok:
        # Unlike GET_LINK_PARAMS this is not optional. The device declared the
        # capability that owns the opcode, so refusing it -- unsupported_opcode
        # most of all -- contradicts its own Info.
        raise Fail(
            f"GET_POWER was answered {response.status_name} on a device that "
            f"declares capability bit "
            f"{refdec.CAPABILITIES['power']} (`power`). §9 makes the bit the "
            f"opcode's owner: declaring it and refusing the opcode leaves a "
            f"client no way to tell which of the two statements is true",
            response=response.raw.hex())
    try:
        s.state["power"] = response.detail_as("power_state")
    except refdec.Reject as exc:
        # `percent-out-of-range` lands here, and it is the interesting one: a
        # percentage above 100 is rejected whole rather than clamped (§9.9),
        # so the whole record is refused and this is where that surfaces.
        raise Fail(f"the power_state detail did not decode: {exc}",
                   detail=response.detail.hex()) from None
    s.state["power_raw"] = response.detail


@check(id="power.something_valid", section="9.9", phase="control",
       severity="MUST", requires=("power",),
       title="A device declaring `power` reports at least one valid field")
async def power_something_valid(s):
    power = _power(s)
    if power["validity"]:
        raise Observe(
            "reports " + ", ".join(
                n for n in ("source", "percent")
                if n not in power["absent"]),
            validity=f"0x{power['validity']:02x}")
    raise Fail(
        "every power_validity bit is clear. §9.9 -- with nothing valid this "
        "device has said what a device without the capability says by not "
        "declaring it, and a client has spent a round trip to learn nothing",
        detail=s.state["power_raw"].hex())


@check(id="power.absent_fields_zero", section="9.9", phase="control",
       severity="MUST", requires=("power",),
       title="Power fields the device does not measure are written as zero")
async def power_absent_fields_zero(s):
    _power(s)
    stale = refdec.absent_but_nonzero("power_state", s.state["power_raw"],
                                      "power_validity")
    if stale:
        raise Fail(
            f"{', '.join(f'{n}={v}' for n, v in stale)} "
            f"{'is' if len(stale) == 1 else 'are'} non-zero with the governing "
            f"validity bit clear. §9.9 holds these to the same rule as §5.1, "
            f"and a stale reading behind a cleared bit is exactly what §1.1 "
            f"exists to keep off the wire",
            detail=s.state["power_raw"].hex())


@check(id="power.reserved", section="9.9", phase="control", severity="MUST",
       requires=("power",),
       title="Reserved power_validity bits and the reserved byte are zero")
async def power_reserved(s):
    power = _power(s)
    reserved = refdec.reserved_mask("power_validity", 8)
    problems = []
    if power["validity"] & reserved:
        problems.append(f"power_state.validity has reserved bits set "
                        f"(0x{power['validity']:02x})")
    if power["reserved"]:
        problems.append(f"power_state.reserved is {power['reserved']}; "
                        f"Appendix A holds byte 5 for power metadata")
    if problems:
        raise Fail("; ".join(problems) + ". A 1.0 device that writes them has "
                   "published a claim this version has not defined, on the only "
                   "bytes a later minor may still assign",
                   detail=s.state["power_raw"].hex())


@check(id="power.source_defined", section="9.9", phase="control",
       severity="OBSERVE", requires=("power",),
       title="The reported source is a member this version defines")
async def power_source_defined(s):
    power = _power(s)
    if "source" in power["absent"]:
        raise Skip("this device does not report a power source")
    if not power["source_known"]:
        # Not a failure: §11.4 lets a minor version add members, and reporting
        # it unknown is precisely what a conforming client does with one.
        raise Observe(
            f"power_source {power['source']} is not a member of VTP/1.0; a "
            f"client reports it unknown and MUST NOT substitute a default",
            source=power["source"])
    raise Observe(f"running on {SOURCE_NAMES[power['source']]}",
                  source=SOURCE_NAMES[power["source"]])
