"""SPEC.md §2 and 9.1 -- the link, and the one opcode that lets a client see it.

12.1 is explicit that 2.1-2.3 are the requirements no vector can test: link-layer
payload, PHY and connection interval appear in no notification. They are also
unavailable to an application on every desktop operating system this harness
runs on, so what follows checks the device's own report of them and says plainly
where a report is all it is.
"""
import struct

from .. import refdec
from ..session import ControlTimeout
from . import Fail, Observe, Skip, check

PHY_NAMES = refdec.enum_values("phy")


def _link_params(s):
    params = s.state.get("link_params")
    if params is None:
        raise Skip("GET_LINK_PARAMS returned nothing to check")
    return params


@check(id="link.att_mtu", section="2", phase="transport", severity="MUST",
       title="The negotiated ATT MTU reaches the specified minimum")
async def link_att_mtu(s):
    if s.mtu is None:
        raise Skip("this platform does not report the negotiated ATT MTU")
    if s.mtu < refdec.MIN_ATT_MTU:
        raise Fail(
            f"the negotiated ATT MTU is {s.mtu}; §2 sets a minimum of "
            f"{refdec.MIN_ATT_MTU}. The central asks and the device answers, so "
            f"check the device's negotiated MTU before blaming this host",
            att_mtu=s.mtu)
    raise Observe(f"negotiated ATT MTU {s.mtu} "
                  f"({s.mtu - 3} bytes per notification)", att_mtu=s.mtu)


@check(id="link.get_link_params", section="9.1", phase="transport",
       severity="SHOULD", requires=("control",),
       title="GET_LINK_PARAMS is implemented")
async def link_get_link_params(s):
    if s.control is None:
        raise Skip("this device does not declare the control capability")
    try:
        response = await s.control.request(refdec.OPCODE["GET_LINK_PARAMS"])
    except ControlTimeout:
        # Implementing the opcode is a SHOULD; answering a request is not.
        raise Fail("GET_LINK_PARAMS went unanswered. §9 requires a device to "
                   "respond to every request it applies, whether or not the "
                   "opcode itself is optional", severity="MUST") from None
    if response.status == refdec.STATUS_VALUE["unsupported_opcode"]:
        # A client MUST NOT treat this as non-conforming, so it is a SHOULD --
        # but it is the only window this specification gives onto 2.1-2.3, and
        # without it those requirements are unobservable rather than merely
        # unproven.
        raise Fail(
            "GET_LINK_PARAMS is not implemented. It is the only way a client "
            "can check the transport requirements of 2.1-2.3, none of which it "
            "can see from its own Bluetooth stack")
    if not response.ok:
        raise Fail(f"GET_LINK_PARAMS was answered {response.status_name}",
                   response=response.raw.hex())
    try:
        s.state["link_params"] = response.detail_as("link_params")
    except refdec.Reject as exc:
        raise Fail(f"the link_params detail did not decode: {exc}",
                   detail=response.detail.hex()) from None
    s.state["link_params_raw"] = response.detail


@check(id="link.reported_fields_zeroed", section="9.1", phase="transport",
       severity="MUST", requires=("control",),
       title="Link parameters the device does not know are written as zero")
async def link_reported_fields_zeroed(s):
    _link_params(s)
    stale = refdec.absent_but_nonzero("link_params", s.state["link_params_raw"],
                                      "link_validity")
    if stale:
        raise Fail(
            f"{', '.join(f'{n}={v}' for n, v in stale)} "
            f"{'is' if len(stale) == 1 else 'are'} non-zero with the governing "
            f"validity bit clear. §9.1 holds these to the same rule as 5.1",
            detail=s.state["link_params_raw"].hex())


@check(id="link.validity_groups", section="9.1", phase="transport",
       severity="MUST", requires=("control",),
       title="A validity bit governing several fields is set only when all are known")
async def link_validity_groups(s):
    params = _link_params(s)
    validity = params["validity"]
    problems = []
    if validity & (1 << refdec.bit("link_validity", "phy")):
        # There is no PHY value zero, so a zeroed phy_tx cannot be mistaken for
        # LE 1M -- which is exactly why setting the bit over one is a claim the
        # device cannot support.
        for field in ("phy_tx", "phy_rx"):
            if params[field] == 0:
                problems.append(f"the phy validity bit is set and {field} is 0")
            elif not params[f"{field}_known"]:
                problems.append(f"{field} is {params[field]}, which this version "
                                f"does not define")
    if validity & (1 << refdec.bit("link_validity", "conn_params")):
        for field in ("conn_interval", "supervision_timeout"):
            if params[field] == 0:
                problems.append(f"the conn_params validity bit is set and "
                                f"{field} is 0")
    if problems:
        raise Fail(
            "; ".join(problems) + ". Half a group is the same state as none of "
            "it, and the honest encoding of that state is a clear bit",
            validity=f"0x{validity:04x}")


@check(id="link.reserved_validity", section="9.1", phase="transport",
       severity="MUST", requires=("control",),
       title="Reserved link_validity bits are zero")
async def link_reserved_validity(s):
    params = _link_params(s)
    reserved = refdec.reserved_mask("link_validity", 16)
    if params["validity"] & reserved:
        raise Fail(f"link_params.validity has reserved bits set "
                   f"(0x{params['validity']:04x})")


@check(id="link.reported_mtu_agrees", section="9.1", phase="transport",
       severity="MUST", requires=("control",),
       title="The device's reported ATT MTU matches the host's")
async def link_reported_mtu_agrees(s):
    params = _link_params(s)
    if "att_mtu" in params["absent"]:
        raise Skip("the device does not report its ATT MTU")
    if s.mtu is None:
        raise Skip("this platform does not report the negotiated ATT MTU, so "
                   "there is nothing to compare against")
    if params["att_mtu"] != s.mtu:
        # The one place a device's self-report can be checked against something
        # independent. A mismatch here says the device's view of its own link is
        # wrong, which puts every other field in this record in doubt.
        raise Fail(
            f"the device reports an ATT MTU of {params['att_mtu']}; this host "
            f"negotiated {s.mtu}. The two describe the same link",
            reported=params["att_mtu"], observed=s.mtu)


@check(id="link.ll_payload", section="2.1", phase="transport", severity="SHOULD",
       requires=("control",),
       title="The link-layer payload is not far below the ATT MTU")
async def link_ll_payload(s):
    params = _link_params(s)
    if "ll_max_tx_octets" in params["absent"]:
        raise Skip("the device does not report its link-layer payload")
    tx = params["ll_max_tx_octets"]
    mtu = params["att_mtu"] if "att_mtu" not in params["absent"] else s.mtu
    if mtu and tx < mtu:
        raise Fail(
            f"the device reports a link-layer payload of {tx} octets against an "
            f"ATT MTU of {mtu}. A notification that size is fragmented across "
            f"several packets, each with its own header, spacing and "
            f"acknowledgement -- roughly three times the radio airtime for the "
            f"same bytes, taken from every other peripheral sharing this "
            f"central. §9.1 asks a client to surface this as a device defect",
            ll_max_tx_octets=tx, att_mtu=mtu)
    raise Observe(f"link-layer payload {tx}/{params['ll_max_rx_octets']} octets "
                  f"tx/rx", ll_max_tx_octets=tx,
                  ll_max_rx_octets=params["ll_max_rx_octets"])


@check(id="link.phy", section="2.2", phase="transport", severity="SHOULD",
       requires=("control",),
       title="The device is running on the LE 2M PHY")
async def link_phy(s):
    params = _link_params(s)
    if "phy_tx" in params["absent"]:
        raise Skip("the device does not report its PHY")
    tx = PHY_NAMES.get(params["phy_tx"], f"unknown({params['phy_tx']})")
    rx = PHY_NAMES.get(params["phy_rx"], f"unknown({params['phy_rx']})")
    if params["phy_tx"] != refdec.enum_value("phy", "le_2m"):
        raise Fail(
            f"reported PHY is {tx} tx / {rx} rx. §2.2 asks a device to request "
            f"LE 2M, which halves the airtime of a given payload; LE 1M is "
            f"conforming and slower. The central grants the PHY, so this may be "
            f"this host rather than the device",
            phy_tx=tx, phy_rx=rx)
    raise Observe(f"PHY {tx} tx / {rx} rx", phy_tx=tx, phy_rx=rx)


@check(id="link.conn_params", section="2.3", phase="transport", severity="OBSERVE",
       requires=("control",),
       title="Reported connection parameters")
async def link_conn_params(s):
    params = _link_params(s)
    if "conn_interval" in params["absent"]:
        raise Skip("the device does not report its connection parameters")
    interval_ms = params["conn_interval"] * 1.25
    raise Observe(
        f"connection interval {interval_ms:.2f} ms, peripheral latency "
        f"{params['peripheral_latency']}, supervision timeout "
        f"{params['supervision_timeout'] * 10} ms. §2.3 asks a device to request "
        f"15 ms while streaming, but the central grants it -- a central serving "
        f"several peripherals commonly grants 30 ms or more, and a device MUST "
        f"function at whatever it is given",
        conn_interval_ms=round(interval_ms, 2),
        peripheral_latency=params["peripheral_latency"],
        supervision_timeout_ms=params["supervision_timeout"] * 10)


@check(id="transport.unverifiable", section="12.1", phase="transport",
       severity="OBSERVE",
       title="What this harness cannot verify from a desktop host")
async def transport_unverifiable(s):
    s.note(
        "SPEC.md §2.1-2.3 -- link-layer payload, PHY and connection interval -- "
        "cannot be verified from here. No desktop operating system exposes them "
        "to an application, so everything above about them is the device's own "
        "report, checked for internal consistency and against the one figure "
        "this host does know (the ATT MTU). A device that misreports cannot be "
        "caught by any means this specification provides. Verify them on a "
        "bench with a sniffer.")
    s.note(
        "SPEC.md §8.1's clock discipline and 6.1's timestamp bounds are not "
        "verified. This host's scheduler and Bluetooth stack sit between the "
        "device and every arrival time measured here, and they are worth tens "
        "of milliseconds against a clock specified in microseconds. What was "
        "checked is that the timestamps are internally consistent, ordered, and "
        "shared across the streams -- not that they are accurate.")
    raise Observe("see the notes below the results")
