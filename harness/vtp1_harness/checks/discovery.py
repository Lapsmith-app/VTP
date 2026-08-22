"""SPEC.md §3 -- discovery: the advertisement, the GATT layout, Device Information."""
from .. import refdec
from ..transport import DeviceRefused, TransportError
from . import Fail, Observe, Skip, check


@check(id="adv.service_uuid", section="3.3", phase="discovery", severity="MUST",
       title="The advertisement carries the VTP/1 service UUID")
async def adv_service_uuid(s):
    if s.advert is None:
        raise Skip("connected to an address rather than a scan result")
    if not s.advert.is_vtp:
        raise Fail("the advertisement does not name the VTP/1 service UUID",
                   advertised=s.advert.service_uuids,
                   expected=refdec.SERVICE_UUID)


@check(id="adv.service_data", section="3.3", phase="discovery", severity="SHOULD",
       title="The advertisement carries three bytes of Service Data")
async def adv_service_data(s):
    if s.advert is None:
        raise Skip("connected to an address rather than a scan result")
    data = s.advert.vtp_service_data
    if data is None:
        raise Fail(
            "no Service Data for the VTP/1 service UUID. It is advisory, so a "
            "client works without it, but it is what lets one show capabilities "
            "before connecting")
    if len(data) != 3:
        raise Fail(f"Service Data is {len(data)} bytes; §3.3 defines three",
                   service_data=data.hex())
    s.state["adv_service_data"] = data


@check(id="adv.name", section="3.3", phase="discovery", severity="OBSERVE",
       title="Advertised local name")
async def adv_name(s):
    if s.advert is None:
        raise Skip("connected to an address rather than a scan result")
    name = s.advert.name
    # A 128-bit service UUID takes 18 of the 31 advertising bytes and the flags
    # take 3. A long name does not truncate: the stack drops a whole element,
    # and if what it drops is the service UUID the device becomes invisible to
    # every client scanning for it.
    budget = 31 - 3 - 18 - 2
    if name and len(name.encode()) > budget:
        raise Fail(
            f"the local name is {len(name.encode())} bytes and only {budget} fit "
            f"beside a 128-bit service UUID in one advertisement. A stack drops "
            f"whole elements, so the service UUID may be the one dropped",
            name=name)
    raise Observe(f"advertised as {name!r}" if name else "no local name advertised",
                  name=name, rssi=s.advert.rssi)


@check(id="gatt.service", section="3.1", phase="gatt", severity="MUST",
       title="The VTP/1 service is present")
async def gatt_service(s):
    if refdec.SERVICE_UUID not in s.services:
        raise Fail("the VTP/1 service UUID was not found in the GATT table",
                   found=sorted(s.services), expected=refdec.SERVICE_UUID)


@check(id="gatt.info", section="4", phase="gatt", severity="MUST",
       title="The Info characteristic is present and readable")
async def gatt_info(s):
    ch = s.char("info")
    if ch is None:
        raise Fail("no Info characteristic. It is the only unconditional one "
                   "in the specification", expected=refdec.CHAR["info"])
    if "read" not in ch.properties:
        raise Fail("Info is not readable", properties=sorted(ch.properties))


@check(id="gatt.characteristics", section="3.1", phase="gatt", severity="MUST",
       title="Every declared capability has its characteristic, with the right properties")
async def gatt_characteristics(s):
    if s.info is None:
        raise Skip("Info did not decode")
    wanted = {
        "gps": ("gps", {"notify"}),
        "can": ("can", {"notify"}),
        "imu": ("imu", {"notify"}),
        "control": ("control", {"write", "indicate"}),
        "monitor": ("monitor_values", {"write"}),
    }
    problems = []
    for capability, (char_name, required) in wanted.items():
        if not s.has(capability):
            continue
        ch = s.char(char_name)
        if ch is None:
            problems.append(f"capability {capability!r} is declared but "
                            f"characteristic {char_name} is absent")
            continue
        missing = required - ch.properties
        if missing:
            problems.append(f"{char_name} lacks {sorted(missing)} "
                            f"(has {sorted(ch.properties)})")
    if problems:
        raise Fail("; ".join(problems), declared=sorted(s.capabilities))


@check(id="gatt.control_indicates", section="9", phase="gatt", severity="MUST",
       requires=("control",),
       title="Control answers by indication, not notification")
async def gatt_control_indicates(s):
    ch = s.char("control")
    if ch is None:
        raise Skip("no Control characteristic")
    if "indicate" not in ch.properties:
        # The distinction is not cosmetic: an indication is acknowledged at the
        # ATT layer, so a device knows its answer was delivered. §9.6 makes a
        # request the device cannot answer one it MUST NOT apply, and it can
        # only know that with the acknowledgement.
        raise Fail("Control does not support indications; §9 makes the response "
                   "an INDICATE", properties=sorted(ch.properties))


@check(id="gatt.monitor_needs_control", section="13", phase="gatt", severity="MUST",
       requires=("monitor",),
       title="A Monitor device also declares Control")
async def gatt_monitor_needs_control(s):
    # The declaration is read with MONITOR_LIST, which is a Control opcode, so
    # a device asking for values with no way to say which values has asked for
    # something no client can supply.
    if not s.has("control"):
        raise Fail("capability bit 3 (monitor) is set but bit 4 (control) is "
                   "not, and MONITOR_LIST is a Control opcode -- no client can "
                   "read the declaration")


@check(id="gatt.family", section="3.2", phase="gatt", severity="OBSERVE",
       title="Other VTP-family services on this device")
async def gatt_family(s):
    # 56 54 50 MM -- ASCII "VTP" and a major version.
    others = sorted(u for u in s.services
                    if u.startswith("565450") and u != refdec.SERVICE_UUID)
    if not others:
        raise Observe("no other VTP major version is advertised")
    raise Observe(f"also exposes {len(others)} other VTP-family service(s)",
                  services=others)


@check(id="dis.present", section="3.4", phase="gatt", severity="SHOULD",
       title="Device Information Service with manufacturer, model and firmware")
async def dis_present(s):
    if refdec.DIS_SERVICE not in s.services:
        raise Fail(
            "no Device Information Service (0x180A). Nothing in VTP/1 reads it, "
            "which is the point: it is where every generic Bluetooth tool looks "
            "to answer which firmware is on the misbehaving logger")
    await s.read_dis()
    missing = [name for name in refdec.DIS_CHARS if not s.dis.get(name)]
    if missing:
        raise Fail(f"Device Information Service is present but {missing} "
                   f"{'is' if len(missing) == 1 else 'are'} empty or absent",
                   read=s.dis)
    s.note("SPEC.md §2 makes the Device Information Service a MUST while 3.4 "
           "makes it a SHOULD and explains why it is one. This harness follows "
           "3.4, which is the section that argues its case; the two clauses "
           "disagree and one of them needs amending.")
    raise Observe(", ".join(f"{k}={v!r}" for k, v in s.dis.items()), **s.dis)


@check(id="security.posture", section="10", phase="gatt", severity="OBSERVE",
       title="Which characteristics required an encrypted link")
async def security_posture(s):
    # Encryption is the device's decision (10), so there is nothing to pass or
    # fail -- but which characteristics enforce it is worth putting in a report,
    # because a device that requires it on some and not others is usually
    # reporting an oversight rather than a policy.
    refused = []
    for name, uuid in refdec.CHAR.items():
        ch = s.chars.get(uuid)
        if ch is None or "read" not in ch.properties:
            continue
        try:
            await s.transport.read(uuid)
        except DeviceRefused:
            refused.append(name)
        except TransportError as exc:
            raise Skip(f"the link failed while probing: {exc}")
    if refused:
        raise Observe(f"an encrypted link was required for: {', '.join(refused)}",
                      refused=refused)
    raise Observe("every readable characteristic was readable on this link "
                  "without additional authentication")
