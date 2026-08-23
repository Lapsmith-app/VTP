"""SPEC.md §9.1 and 8.2 -- the state a device MUST NOT carry across a link drop.

Both rules exist so that a client always finds a known state at connect and
never inherits one it did not install. Neither can be tested without dropping
the link, which is why they are last.
"""
from .. import refdec
from . import Fail, Observe, Skip, check


@check(id="reconnect.subscriptions_cleared", section="9.1", phase="reconnect",
       severity="MUST", requires=("control", "can"),
       title="The CAN subscription table is empty on a new connection")
async def reconnect_subscriptions_cleared(s):
    if not s.state.get("reconnected"):
        raise Skip("the harness did not reconnect")
    if s.control is None:
        raise Skip("no control plane on the second connection")
    installed = s.state.get("installed_before_reconnect") or {}
    if not installed:
        raise Skip("nothing was installed before the link dropped, so an empty "
                   "table proves nothing")
    # §9.1 makes (id, mask) the subscription's identity, so removal is also
    # the probe: unsubscribing something the OLD connection installed answers
    # unknown_subscription on a device that cleared its table, and ok on one
    # that carried it across the drop.
    surviving = []
    for can_id, mask in sorted(installed.items()):
        response = await s.control.unsubscribe_can(can_id, mask=mask)
        if response.ok:
            surviving.append(can_id)
        elif response.status != refdec.STATUS_VALUE["unknown_subscription"]:
            raise Fail(
                f"probing the table after reconnecting, CAN_UNSUBSCRIBE was "
                f"answered {response.status_name}", response=response.raw.hex())
    if surviving:
        raise Fail(
            f"{len(surviving)} subscription(s) survived the link dropping; "
            f"{len(installed)} were installed on the previous connection. A "
            f"device MUST clear its table when the link drops, so that a client "
            f"always finds a known state",
            surviving=[hex(i) for i in surviving])


@check(id="reconnect.seq_restarts", section="8.2", phase="reconnect",
       severity="MUST",
       title="Sequence numbers restart at 0 on the new connection")
async def reconnect_seq_restarts(s):
    if not s.state.get("reconnected"):
        raise Skip("the harness did not reconnect")
    import struct
    seq_record = {"gps": "gps_fix", "can": "can_header", "imu": "imu_header"}
    checked, problems = [], []
    for name in s.STREAMS:
        log = s.streams[name]
        if not s.has(name) or not len(log):
            continue
        seq = struct.unpack_from(
            "<H", log.items[0].payload, refdec.offset(seq_record[name], "seq"))[0]
        checked.append(name)
        if seq != 0:
            problems.append(f"{name} restarted at seq {seq}")
    if not checked:
        raise Skip("no stream produced a notification on the second connection")
    if problems:
        raise Fail(
            "; ".join(problems) + ". A client never has to tell a reconnection "
            "from a wrap, which is why this protocol needs no session or boot "
            "identifier",
            streams=checked)


@check(id="reconnect.info_reread", section="4", phase="reconnect",
       severity="OBSERVE",
       title="Info on the second connection")
async def reconnect_info_reread(s):
    if not s.state.get("reconnected"):
        raise Skip("the harness did not reconnect")
    before = s.state.get("info_first_connection")
    if before is None or s.info_raw is None:
        raise Skip("Info was not read on both connections")
    if bytes(before) != bytes(s.info_raw):
        # Not a fault. §4 forbids caching Info across connections precisely
        # because a DIY device is reflashed by its owner and its capability set
        # can change while its address does not.
        raise Observe(
            "Info differs between the two connections, which is why a client "
            "MUST re-read it on every one",
            first=bytes(before).hex(), second=bytes(s.info_raw).hex())
    raise Observe("Info is identical across both connections; a client MUST "
                  "still re-read it on each one")
