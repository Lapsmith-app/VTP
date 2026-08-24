"""SPEC.md §4 -- the Info characteristic, and what the rest of the run derives from it."""
from .. import refdec
from . import Fail, Observe, Skip, check


@check(id="info.decodes", section="4", phase="info", severity="MUST",
       title="Info is 24 bytes and decodes")
async def info_decodes(s):
    if s.info_raw is None:
        raise Fail("Info could not be read")
    if s.info is None:
        raise Fail(f"Info did not decode: {s.info_reject}. It is "
                   f"{len(s.info_raw)} byte(s); §4 defines {refdec.size('info')}",
                   payload=s.info_raw.hex())


@check(id="info.major", section="4", phase="info", severity="MUST",
       title="protocol_major matches the service UUID's major version")
async def info_major(s):
    if s.info is None:
        raise Skip("Info did not decode")
    if s.info["protocol_major"] != refdec.PROTOCOL_MAJOR:
        # §4 makes this a disconnect for a client, so it is the one Info field
        # whose failure invalidates everything after it.
        raise Fail(
            f"protocol_major is {s.info['protocol_major']}, but the discovered "
            f"service UUID is major {refdec.PROTOCOL_MAJOR}. A client MUST treat "
            f"this device as non-conforming and disconnect")


@check(id="info.reserved_capabilities", section="4", phase="info", severity="MUST",
       title="Reserved capability bits are zero")
async def info_reserved_capabilities(s):
    if s.info is None:
        raise Skip("Info did not decode")
    reserved = refdec.reserved_mask("capabilities", 32)
    set_bits = s.info["capabilities"] & reserved
    if set_bits:
        raise Fail(
            f"capabilities has reserved bit(s) set: "
            f"{sorted(i for i in range(32) if set_bits & (1 << i))}. Appendix A "
            f"holds bits 8-31 for roles added in a later minor, and a client "
            f"reading one today cannot tell a future role from a bug",
            capabilities=f"0x{s.info['capabilities']:08x}")


@check(id="info.capability_implications", section="4.1", phase="info",
       severity="MUST",
       title="Every capability bit brings the bits it requires")
async def info_capability_implications(s):
    if s.info_raw is None or len(s.info_raw) < refdec.size("info"):
        raise Skip("Info could not be read")
    # §4.1 binds the DEVICE: it MUST NOT publish an Info that breaks an
    # implication. A client decodes it -- the record is well-formed -- and
    # this is the surfacing §4.1 asks that client to do: it MUST NOT use a
    # role whose required bit is missing, and MUST NOT guess which half was
    # meant.
    declared = _declared_capabilities(s.info_raw)
    broken = []
    for capability, required in refdec.IMPLIES.items():
        if capability not in declared:
            continue
        missing = [r for r in required if r not in declared]
        if missing:
            broken.append(f"{capability} requires {', '.join(missing)}")
    if broken:
        raise Fail("; ".join(broken) + ". §4.1 makes the implications normative: "
                   "monitor without control has asked for values through an "
                   "opcode it cannot answer, and can_fd without can describes a "
                   "bus the device does not have", declared=sorted(declared))


@check(id="info.capacities", section="4.1", phase="info", severity="MUST",
       title="Capacity fields agree with the capabilities that govern them")
async def info_capacities(s):
    if s.info is None:
        raise Skip("Info did not decode")
    problems = []
    for capability, fields in refdec.CAPACITY_FIELDS.items():
        # "A capacity field of zero means none, not unspecified", so both
        # directions carry meaning and both are checkable. The pairing of field
        # to bit is the schema's, not this file's.
        if s.has(capability):
            if not s.info[fields[-1]]:
                problems.append(
                    f"{capability} is declared and {fields[-1]} is 0, which "
                    f"means none -- the device has said it can do nothing")
            continue
        nonzero = [f for f in fields if s.info[f]]
        if nonzero:
            problems.append(
                f"{capability} is not declared but "
                f"{', '.join(nonzero)} {'is' if len(nonzero) == 1 else 'are'} "
                f"non-zero, publishing a role the device does not have")
    if problems:
        raise Fail("; ".join(problems), info=_summary(s.info))


@check(id="info.rate_ceiling", section="4", phase="info", severity="SHOULD",
       title="Current rates do not exceed their own ceilings")
async def info_rate_ceiling(s):
    if s.info is None:
        raise Skip("Info did not decode")
    i = s.info
    over = [name for name, current, ceiling in
            (("GPS", i["gps_rate_hz"], i["gps_max_rate_hz"]),
             ("IMU", i["imu_rate_hz"], i["imu_max_rate_hz"]))
            if current > ceiling]
    if over:
        raise Fail(f"{' and '.join(over)} reports a current rate above its own "
                   f"maximum", info=_summary(i))


# `info.reserved_fields` lived here until SPEC.md §15 assigned Info's last
# reserved bytes (20 and 22-23) to the OBD capacities: the record now has no
# reserved field left to hold to §2, and a check that can never fail is a
# check that does not work. If a later revision reserves an Info byte again,
# the check comes back with it -- schema-derived, as it was.


@check(id="info.can_payload", section="4.1", phase="info", severity="OBSERVE",
       requires=("can",),
       title="The largest CAN payload this device can carry")
async def info_can_payload(s):
    if s.info is None:
        raise Skip("Info did not decode")
    payload = refdec.can_max_payload(s.capabilities)
    raise Observe(
        f"{payload} bytes -- {'CAN FD' if s.has('can_fd') else 'classic CAN'}. "
        f"§4.1 derives this from the capability bits; it is not a field, "
        f"so there is nothing here that can disagree with itself",
        can_max_payload=payload)


@check(id="info.identity", section="4", phase="info", severity="OBSERVE",
       title="What the device says it is")
async def info_identity(s):
    if s.info is None:
        raise Skip("Info did not decode")
    i = s.info
    clock = []
    if i["clock_flags"] & 0x01:
        clock.append("GNSS-disciplined")
    if i["clock_flags"] & 0x02:
        clock.append("survives reconnect")
    raise Observe(
        f"VTP/{i['protocol_major']}.{i['protocol_minor']}, roles "
        f"{', '.join(sorted(s.capabilities)) or 'none'}"
        + (f"; clock {', '.join(clock)}" if clock else ""),
        **_summary(i))


@check(id="info.stable", section="4", phase="info", severity="OBSERVE",
       title="Info is unchanged when read a second time in one connection")
async def info_stable(s):
    again = await s.transport.read(refdec.CHAR["info"])
    if bytes(again) != bytes(s.info_raw):
        # §4 requires a client to re-read per connection, not per read, so this
        # is not stated as a rule. A device that answers differently within one
        # connection is nonetheless describing two devices.
        raise Fail("Info read twice in one connection returned different bytes",
                   first=s.info_raw.hex(), second=bytes(again).hex())


@check(id="adv.service_data_agrees", section="3.3", phase="info", severity="SHOULD",
       title="Service Data agrees with Info")
async def adv_service_data_agrees(s):
    data = s.state.get("adv_service_data")
    if data is None:
        raise Skip("no Service Data was advertised")
    if s.info is None:
        raise Skip("Info did not decode")
    problems = []
    if data[0] != s.info["protocol_minor"]:
        problems.append(f"advertised minor {data[0]}, Info says "
                        f"{s.info['protocol_minor']}")
    if data[1] != (s.info["capabilities"] & 0xFF):
        problems.append(f"advertised capabilities 0x{data[1]:02x}, Info says "
                        f"0x{s.info['capabilities'] & 0xFF:02x}")
    if problems:
        # Advisory, and a client MUST read Info regardless -- but a scan list
        # showing the wrong roles is a user-visible defect.
        raise Fail("; ".join(problems), service_data=data.hex())


def _declared_capabilities(raw):
    import struct
    (word,) = struct.unpack_from("<I", raw, refdec.offset("info", "capabilities"))
    return {name for name, bit in refdec.CAPABILITIES.items() if word & (1 << bit)}


def _summary(info):
    return {k: v for k, v in info.items() if not k.startswith("_")}
