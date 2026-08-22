"""SPEC.md §4 -- the Info characteristic, and what the rest of the run derives from it."""
from .. import refdec
from . import Fail, Observe, Skip, check


@check(id="info.decodes", section="4", phase="info", severity="MUST",
       title="Info is 24 bytes and decodes")
async def info_decodes(s):
    if s.info_raw is None:
        raise Fail("Info could not be read")
    if s.info is None:
        raise Fail(f"Info did not decode: {len(s.info_raw)} bytes, "
                   f"{refdec.size('info')} required",
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


@check(id="info.capacities", section="4", phase="info", severity="MUST",
       title="Capacity fields agree with the declared capabilities")
async def info_capacities(s):
    if s.info is None:
        raise Skip("Info did not decode")
    i = s.info
    problems = []
    # "A capacity field of zero means none, not unspecified" -- so the two
    # directions are both meaningful, and both are checkable.
    if not s.has("gps") and (i["gps_rate_hz"] or i["gps_max_rate_hz"]):
        problems.append("no GPS capability, but a GPS rate is non-zero")
    if not s.has("imu") and (i["imu_rate_hz"] or i["imu_max_rate_hz"]):
        problems.append("no IMU capability, but an IMU rate is non-zero")
    if not s.has("can") and (i["can_subscription_slots"] or i["can_max_frames_per_s"]):
        problems.append("no CAN capability, but a CAN capacity is non-zero")
    if s.has("gps") and not i["gps_max_rate_hz"]:
        problems.append("GPS is declared but gps_max_rate_hz is 0, which means "
                        "none -- the device has said it can produce no fixes")
    if s.has("imu") and not i["imu_max_rate_hz"]:
        problems.append("IMU is declared but imu_max_rate_hz is 0")
    if s.has("can") and not i["can_subscription_slots"]:
        problems.append("CAN is declared but can_subscription_slots is 0, so no "
                        "client can ever ask for a frame")
    if problems:
        raise Fail("; ".join(problems), info=_summary(i))


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


@check(id="info.can_payload", section="4", phase="info", severity="MUST",
       requires=("can",),
       title="can_max_payload matches the declared CAN flavour")
async def info_can_payload(s):
    if s.info is None:
        raise Skip("Info did not decode")
    expected = 64 if s.has("can_fd") else 8
    got = s.info["can_max_payload"]
    if got != expected:
        raise Fail(
            f"can_max_payload is {got}; a device declaring "
            f"{'CAN FD' if s.has('can_fd') else 'classic CAN'} carries {expected}",
            can_fd=s.has("can_fd"))


@check(id="info.notify_bytes", section="4", phase="info", severity="MUST",
       title="max_notify_bytes fits the link and clears the specified floor")
async def info_notify_bytes(s):
    if s.info is None:
        raise Skip("Info did not decode")
    if not (s.capabilities & {"gps", "can", "imu"}):
        raise Skip("this device declares no notifying role")
    declared = s.info["max_notify_bytes"]
    if declared < refdec.MIN_NOTIFY_BYTES:
        raise Fail(
            f"max_notify_bytes is {declared}; §2 requires a device to function "
            f"at an ATT MTU of {refdec.MIN_ATT_MTU}, which is "
            f"{refdec.MIN_NOTIFY_BYTES} bytes of payload")
    if s.mtu is not None and declared > s.mtu - 3:
        raise Fail(
            f"max_notify_bytes is {declared} but the negotiated ATT MTU is "
            f"{s.mtu}, leaving {s.mtu - 3}. A notification that size cannot be "
            f"delivered on this link", att_mtu=s.mtu)
    if s.mtu is not None and declared < s.mtu - 3:
        # Not a failure -- a device may have a smaller buffer than the link --
        # but worth reporting, because it is throughput the link is offering
        # and the device is not taking.
        raise Observe(
            f"max_notify_bytes is {declared} of the {s.mtu - 3} this link "
            f"allows", declared=declared, available=s.mtu - 3)


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


def _summary(info):
    return {k: v for k, v in info.items() if not k.startswith("_")}
