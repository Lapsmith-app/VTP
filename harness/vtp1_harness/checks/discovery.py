"""SPEC.md §3 -- discovery: the advertisement, the GATT layout, Device Information."""
from .. import refdec
from ..transport import DeviceRefused, TransportError
from . import Fail, Observe, Skip, check

#: A check that fires on the ABSENCE of a capability cannot run when Info did
#: not decode. `session.capabilities` is empty in that case -- not because the
#: device declares nothing, but because nobody could read what it declares --
#: and a check reading that as "no Control" reports a device answering writes
#: to an inert characteristic when the truth is that its Info is malformed.
#: One real defect then arrives as two, the second of them false, and the
#: developer starts on the wrong one.
_NO_INFO = ("Info did not decode, so what this device declares is unknown and "
            "an absent capability cannot be told from an unreadable one")


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
    # The budget comes from the peripheral, which already names every term of
    # it. Restating the arithmetic here would let the two disagree silently --
    # the peripheral refusing a name this accepts, or the reverse -- and
    # nothing in CI compares constants the way check_docs.py compares section
    # references.
    budget = _advertisement_budget()
    if budget is None:
        raise Skip("the peripheral's advertisement constants are not importable "
                   "from here, and this check will not restate them")
    if name and len(name.encode()) > budget:
        raise Fail(
            f"the local name is {len(name.encode())} bytes and only {budget} fit "
            f"beside a 128-bit service UUID in one advertisement. A stack drops "
            f"whole elements, so the service UUID may be the one dropped",
            name=name)
    raise Observe(f"advertised as {name!r}" if name else "no local name advertised",
                  name=name, rssi=s.advert.rssi)


def _advertisement_budget():
    """Bytes left for a local name beside a 128-bit service UUID.

    A long name does not truncate: the stack drops a whole advertising element,
    and if what it drops is the service UUID the device becomes invisible to
    every client scanning for it.
    """
    import sys
    path = str(refdec.ROOT / "reference" / "peripheral")
    if path not in sys.path:
        sys.path.insert(0, path)
    try:
        import serve
    except Exception:                               # noqa: BLE001
        return None
    return serve.MAX_NAME_CHARS


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


@check(id="gatt.attribute_table", section="4.1", phase="gatt", severity="MUST",
       title="Every characteristic in the profile is present, with its properties")
async def gatt_attribute_table(s):
    problems = []
    for name, spec in refdec.PROFILE_CHARS.items():
        ch = s.chars.get(refdec.CHAR[name])
        if ch is None:
            # The attribute table is FIXED. A characteristic whose capability
            # bit is clear is inert, not absent, and the reason is not
            # elegance: central stacks cache the attribute table across
            # connections and several cache it across reboots, so a table that
            # changes when a role is switched off in firmware hands the client
            # a stale handle to the wrong attribute.
            problems.append(
                f"{name} is absent"
                + ("" if spec["capability"] is None else
                   f" (capability {spec['capability']!r} is "
                   f"{'set' if s.has(spec['capability']) else 'clear'}, and "
                   f"§4.1 requires the characteristic either way)"))
            continue
        missing = set(spec["properties"]) - ch.properties
        if missing:
            problems.append(f"{name} lacks {sorted(missing)} "
                            f"(has {sorted(ch.properties)})")
    if problems:
        raise Fail("; ".join(problems), declared=sorted(s.capabilities))


@check(id="gatt.no_extra_characteristics", section="4.1", phase="gatt",
       severity="MUST",
       title="The VTP/1 service carries nothing beyond the profile")
async def gatt_no_extra_characteristics(s):
    known = {refdec.CHAR[name] for name in refdec.PROFILE_CHARS}
    extra = sorted(uuid for uuid, ch in s.chars.items()
                   if ch.service_uuid == refdec.SERVICE_UUID and uuid not in known)
    if extra:
        raise Fail(
            f"the VTP/1 service exposes {len(extra)} characteristic(s) §4.1 "
            f"does not define: {', '.join(extra)}. A device MUST NOT add one; "
            f"a vendor extension belongs in a service of its own",
            extra=extra)


@check(id="gatt.inert_cccd", section="4.1", phase="gatt", severity="MUST",
       title="A CCCD write is accepted on a stream whose capability is clear")
async def gatt_inert_cccd(s):
    if s.info is None:
        raise Skip(_NO_INFO)
    inert = [name for name, spec in refdec.PROFILE_CHARS.items()
             if spec["cccd"] != "none" and spec["capability"] is not None
             and not s.has(spec["capability"])
             and refdec.CHAR[name] in s.chars]
    if not inert:
        raise Skip("this device declares every capability that has a CCCD, so "
                   "it has no inert stream to test")
    refused = []
    for name in inert:
        try:
            await s.transport.subscribe(refdec.CHAR[name], lambda *_: None)
            await s.transport.unsubscribe(refdec.CHAR[name])
        except DeviceRefused as exc:
            refused.append(f"{name} ({exc})")
        except TransportError as exc:
            raise Skip(f"the link failed while probing {name}: {exc}")
    if refused:
        # It costs a two-byte descriptor and a stored value nothing reads, and
        # a device MUST NOT refuse on the grounds that the capability is
        # absent: the client then never notifies on it, which is what inert
        # already means.
        raise Fail(
            f"a CCCD write was rejected on {', '.join(refused)}. §4.1 requires "
            f"a device to accept one on an inert stream and then simply never "
            f"notify", inert=inert)
    raise Observe(f"accepted on {', '.join(inert)}, which notify nothing",
                  inert=inert)


@check(id="gatt.inert_control_rejects_writes", section="4.1", phase="gatt",
       severity="MUST", adversarial=True,
       title="A device without Control rejects every write to it")
async def gatt_inert_control_rejects_writes(s):
    if s.info is None:
        raise Skip(_NO_INFO)
    if s.has("control"):
        raise Skip("this device declares the control capability")
    if refdec.CHAR["control"] not in s.chars:
        raise Skip("no Control characteristic to write to")
    try:
        await s.transport.write(refdec.CHAR["control"], b"\x30\x01", response=True)
    except DeviceRefused:
        return
    except TransportError as exc:
        raise Skip(f"the link failed while probing: {exc}")
    # It does not parse opcodes, does not implement indications, and never
    # answers unsupported_opcode -- answering needs the response path it does
    # not have. Accepting the write silently is the one thing it must not do.
    raise Fail("a write to Control was accepted by a device that does not "
               "declare the control capability. An inert Control rejects every "
               "write with an ATT error and parses no opcode")


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
    #
    # What can be probed from here is narrow, and this reports what it did
    # rather than generalising from it. Under §4.1 only Info carries `read`, so
    # a direct read probes exactly one attribute. Subscribing to the declared
    # streams has already happened and would have failed if their CCCDs needed
    # an encrypted link, so that is evidence rather than a probe. The
    # write-only characteristics cannot be probed at all without writing to
    # them, and a reassuring blanket sentence covering all three would be a
    # green tick for something nothing asserted.
    read_probed, refused = [], []
    for name, uuid in refdec.CHAR.items():
        ch = s.chars.get(uuid)
        if ch is None or "read" not in ch.properties:
            continue
        read_probed.append(name)
        try:
            await s.transport.read(uuid)
        except DeviceRefused:
            refused.append(name)
        except TransportError as exc:
            raise Skip(f"the link failed while probing: {exc}")
    subscribed = sorted(name for name in s.STREAMS
                        if s.has(name) and s.streams[name].subscribed_at)
    unprobed = sorted(name for name, spec in refdec.PROFILE_CHARS.items()
                      if "write" in spec["properties"])
    if unprobed:
        s.note(
            f"SPEC.md §10 lets a device require an encrypted link on any "
            f"characteristic. This run read {', '.join(read_probed) or 'nothing'} "
            f"and subscribed to {', '.join(subscribed) or 'no stream'}; it did "
            f"not probe {', '.join(unprobed)}, because the only way to find out "
            f"is to write to them. Their encryption posture is unverified.")
    if refused:
        raise Observe(f"an encrypted link was required to read: "
                      f"{', '.join(refused)}", refused=refused)
    raise Observe(
        f"read {', '.join(read_probed) or 'nothing'} and subscribed to "
        f"{', '.join(subscribed) or 'no stream'} without additional "
        f"authentication; {', '.join(unprobed) or 'nothing'} not probed",
        read_probed=read_probed, subscribed=subscribed, unprobed=unprobed)
