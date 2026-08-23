"""SPEC.md §2 -- the link, and what a desktop host can see of it.

§12.1 is explicit that 2.1-2.3 are the requirements no vector can test:
link-layer payload, PHY and connection interval appear in no notification.
They are also unavailable to an application on every desktop operating system
this harness runs on, so the one figure checked here is the one the host does
know -- the negotiated ATT MTU -- and the rest is named plainly as
bench-only.
"""
from .. import refdec
from . import Fail, Observe, check


@check(id="link.att_mtu", section="2", phase="transport", severity="MUST",
       title="The negotiated ATT MTU reaches the specified minimum")
async def link_att_mtu(s):
    if s.mtu is None:
        raise Observe("this platform does not report the negotiated ATT MTU")
    if s.mtu < refdec.MIN_ATT_MTU:
        raise Fail(
            f"the negotiated ATT MTU is {s.mtu}; §2 sets a minimum of "
            f"{refdec.MIN_ATT_MTU}. The central asks and the device answers, so "
            f"check the device's negotiated MTU before blaming this host",
            att_mtu=s.mtu)
    raise Observe(f"negotiated ATT MTU {s.mtu} "
                  f"({s.mtu - 3} bytes per notification)", att_mtu=s.mtu)


@check(id="transport.unverifiable", section="12.1", phase="transport",
       severity="OBSERVE",
       title="What this harness cannot verify from a desktop host")
async def transport_unverifiable(s):
    s.note(
        "SPEC.md §2.1-2.3 -- link-layer payload, PHY and connection interval -- "
        "cannot be verified from here. No desktop operating system exposes them "
        "to an application, and they appear in no payload, so nothing in this "
        "run says anything about them. Verify them on a bench with a sniffer.")
    s.note(
        "SPEC.md §8.1's clock discipline and 6.1's timestamp bounds are not "
        "verified. This host's scheduler and Bluetooth stack sit between the "
        "device and every arrival time measured here, and they are worth tens "
        "of milliseconds against a clock specified in microseconds. What was "
        "checked is that the timestamps are internally consistent, ordered, and "
        "shared across the streams -- not that they are accurate.")
    raise Observe("see the notes below the results")
