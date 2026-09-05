#!/usr/bin/env python3
"""Drive the real transport loop against a fake GATT link.

selftest.py proves the DEVICE conforms. This proves the TRANSPORT does, which
is a different thing and, on the evidence, the harder one: every bug in this
file's subject matter reached a real phone before anyone noticed, because
nothing here is reachable by a conformance vector and nothing here is in the
device model.

It runs `Peripheral.run()` itself rather than a copy of it. A reimplementation
would be a second state machine, and the ordering bugs it exists to catch all
lived in the ordering of the first.

Usage:
  python3 reference/peripheral/transport_selftest.py
"""
import asyncio
import pathlib
import struct
import sys
import time

sys.dont_write_bytecode = True
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "python"))

import vtp1                      # noqa: E402
import vtp_device as dev         # noqa: E402
import serve                     # noqa: E402
import gattsim                   # noqa: E402

problems = []


def check(ok, why):
    if not ok:
        problems.append(why)
        print(f"FAIL: {why}")


def seqs(payloads, characteristic):
    reader = {"gps": lambda p: vtp1.decode_gps_fix(p)["seq"],
              "can": lambda p: vtp1.decode_can_batch(p)["header"]["seq"],
              "imu": lambda p: vtp1.decode_imu_batch(p)["header"]["seq"]}
    return [reader[characteristic](p) for p in payloads]


def build(gps_hz=10, imu_hz=0, bless_semantics=False):
    clock = [0]
    device = dev.VtpDevice(now_us=lambda: clock[0], gps_hz=gps_hz, imu_hz=imu_hz)
    peripheral = serve.Peripheral(device, name="VTP")
    server = gattsim.FakeServer(clock, bless_semantics=bless_semantics)
    gattsim.FakeServer.bind(serve.CHAR)
    peripheral.server = server
    # The pump's own ready hook is CoreBluetooth-specific; this is the same
    # signal by the only route the fake has.
    server.on_ready = lambda: setattr(peripheral, "_ready", True)
    serve.CHAR_NAMES.update({v.lower(): k for k, v in serve.CHAR.items()})
    return peripheral, server, clock


class SimulatedClock:
    """The clock the pump schedules against, under the test's control.

    The pump is the one thing in this file with a real-time contract: it must
    reach `poll_hz`. Timing it with a wall clock would report how loaded the
    machine is, which is the same objection gattsim answers for device time.
    So the clock `Peripheral.run` reads is simulated instead -- a tick's work
    advances it by a stated amount, `asyncio.sleep` advances it by exactly
    what it was asked to sleep, and nothing else moves it. Whatever schedule
    the pump then keeps is a property of its own arithmetic.
    """

    def __init__(self):
        self.t = 0.0


class _TimeShim:
    """`serve.time`, with the two readings the pump takes made simulated."""

    def __init__(self, clock):
        self._clock = clock

    def __getattr__(self, name):
        return getattr(time, name)

    def monotonic(self):
        return self._clock.t

    def perf_counter(self):
        return self._clock.t


class _AsyncioShim:
    """`serve.asyncio`, with `sleep` charging the simulated clock.

    It still yields to the real event loop, so the loop's ordering -- which is
    what every other check in this file is about -- is untouched.
    """

    def __init__(self, clock):
        self._clock = clock

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, delay):
        self._clock.t += max(0.0, delay)
        await asyncio.sleep(0)


def pump(work_s, ticks, poll_hz=200):
    """Drive the real pump for `ticks` ticks against a simulated clock.

    `work_s(n)` is charged to that clock as tick n's work, which is the thing
    a loop that sleeps a fixed interval AFTER its work adds to its own period.
    Returns `(marks, tick_hz)`: `marks[n]` is the clock at the start of tick
    n, and `tick_hz` is the rate the pump itself reported.
    """
    clock = SimulatedClock()
    real_time, real_asyncio = serve.time, serve.asyncio
    serve.time, serve.asyncio = _TimeShim(clock), _AsyncioShim(clock)
    try:
        peripheral, server, _ = build(gps_hz=10)
        server.connect(subscribe=("gps",))
        # is_connected is awaited exactly once per tick, before the tick does
        # anything, so it is where a tick's work can be charged.
        inner, marks, n = server.is_connected, [], [0]

        async def worked():
            marks.append(clock.t)
            clock.t += work_s(n[0])
            n[0] += 1
            return await inner()

        server.is_connected = worked
        asyncio.run(peripheral.run(poll_hz=poll_hz, screen_hz=10,
                                   max_ticks=ticks))
        return marks, peripheral._tick_hz
    finally:
        serve.time, serve.asyncio = real_time, real_asyncio


def run(peripheral, ticks):
    asyncio.run(peripheral.run(poll_hz=100_000, screen_hz=10, max_ticks=ticks))


def main():
    # ---- The first notification delivered carries seq 0 -----------------
    # SPEC.md §8.2. It carried 2: the pump polls every tick whether or not a
    # central has subscribed, and an unwanted notification used to consume a
    # number and never return it.
    peripheral, server, _ = build()
    server.connect()                       # connected, but no CCCD yet
    run(peripheral, 400)
    check(not server.sent("gps"),
          "nothing may be delivered on a characteristic no central has "
          "subscribed to")
    server.subscribe("gps")
    run(peripheral, 400)
    delivered = seqs(server.sent("gps"), "gps")
    check(delivered and delivered[0] == 0,
          f"the first notification DELIVERED must carry seq 0, not "
          f"{delivered[:1]}; notifications dropped before subscription must "
          f"not consume a number")
    check(delivered == sorted(set(delivered)) and len(delivered) == len(set(delivered)),
          f"sequence numbers must be contiguous and unique: {delivered[:8]}")

    # ---- Backpressure must not duplicate a number -----------------------
    # A refused notification was returning its number after a newer one had
    # already taken it, so the wire carried 1, 1, 2, 3.
    peripheral, server, _ = build(gps_hz=25, imu_hz=100)
    server.connect(subscribe=("gps", "imu"))
    server.stall()
    run(peripheral, 600)                   # produce a backlog nothing can send
    server.drain()
    run(peripheral, 600)
    for name in ("gps", "imu"):
        got = seqs(server.sent(name), name)
        check(len(got) == len(set(got)),
              f"{name}: backpressure duplicated a sequence number: {got[:8]}")
        check(not got or got[0] == 0,
              f"{name}: first delivered seq after backpressure is {got[:1]}")
        check(got == list(range(len(got))),
              f"{name}: delivered sequence has a gap or a jump: {got[:8]}")

    # ---- A refusal alone loses nothing ----------------------------------
    # SPEC.md §8.3 -- `dropped` counts items the device ACCEPTED AND THEN
    # DISCARDED, and a full transmit queue is neither. The pump used to hand
    # the refused payload to record_refused and delete it, so one "not now"
    # from the stack cost a whole batch and was reported to the client as the
    # device being overrun. `_deliver` had already been written for the other
    # answer -- it stamps seq and commits only on acceptance, precisely so the
    # same number can go out on the next attempt (§8.2) -- but no next attempt
    # was ever made.
    #
    # A stall short enough that nothing newer is produced must therefore cost
    # nothing at all: same payload, same number, delivered late.
    peripheral, server, _ = build(gps_hz=10)
    server.connect(subscribe=("gps",))
    run(peripheral, 60)                      # three fixes at 5 ms per tick
    before = len(server.sent("gps"))
    check(before > 0, "the run before the stall should have delivered")
    server.stall()
    run(peripheral, 25)                      # one fix produced, and refused
    check(server.refusals > 0, "the stall should have refused a notification")
    check(len(server.sent("gps")) == before,
          "nothing may reach the wire while the queue is full")
    server.drain()
    run(peripheral, 10)
    after = seqs(server.sent("gps"), "gps")
    check(len(after) > before,
          f"the refused notification must be retried and land, not be "
          f"discarded: {before} before the stall, {len(after)} after")
    check(after == list(range(len(after))),
          f"a retried notification must carry the number it was stamped with, "
          f"leaving no gap: {after[:8]}")
    dropped = [vtp1.decode_gps_fix(p)["dropped"] for p in server.sent("gps")]
    check(sum(dropped) == 0,
          f"a refusal is the radio being busy, not the device discarding "
          f"anything, so §8.3's counter must stay at zero: {dropped}")

    # ---- A batch is not built while the last one is undelivered ---------
    # SPEC.md §6.2's partial-batch timer is a convenience: it exists so a quiet
    # bus still delivers. Running it while the transport is still holding the
    # previous batch only builds one to supersede the other, and a superseded
    # batch IS loss (§8.3). The bounded flushes are not discretionary and still
    # run -- §6.1 caps a batch at what `dt` can span, and capacity at what fits
    # in one notification.
    clock_box = [0]
    device = dev.VtpDevice(now_us=lambda: clock_box[0], gps_hz=0, imu_hz=0)
    device.on_connect()
    installed = device.handle_control(
        bytes([0x02, 0x01]) + struct.pack("<IBH", 0x0C0, 0, 0))
    check(installed[2] == 0, "the probe subscription should have installed")
    clock_box[0] += 200_000                  # two flush periods of frames
    held = [p for c, p in device.poll() if c == "can"]
    check(len(held) == 1, f"one batch should be due, got {len(held)}")
    clock_box[0] += 200_000
    while_blocked = [p for c, p in device.poll(undelivered=("can",))
                     if c == "can"]
    check(not while_blocked,
          f"the discretionary flush must be held back while the transport has "
          f"not taken the last batch, got {len(while_blocked)}")
    clock_box[0] += 10_000
    freed = [p for c, p in device.poll() if c == "can"]
    check(len(freed) == 1,
          f"and must resume the moment it can, got {len(freed)}")
    check(device.pending_dropped()["can"] == 0,
          f"holding frames back is latency, not loss (§8.3): "
          f"{device.pending_dropped()}")
    carried = vtp1.decode_can_batch(freed[0])["header"]["count"]
    check(carried > vtp1.decode_can_batch(held[0])["header"]["count"] // 2,
          f"the deferred batch should carry what accumulated meanwhile, "
          f"got {carried} frame(s)")

    # ---- A reconnection inherits nothing --------------------------------
    peripheral, server, _ = build()
    server.connect(subscribe=("gps",))
    run(peripheral, 600)
    first = seqs(server.sent("gps"), "gps")
    check(len(first) > 1, "the first connection should have delivered several")
    server.disconnect()
    run(peripheral, 20)
    server.clear_wire()
    server.connect(subscribe=("gps",))
    run(peripheral, 600)
    second = seqs(server.sent("gps"), "gps")
    check(second and second[0] == 0,
          f"seq must restart at 0 for a new central, got {second[:1]}")
    check(len(second) == len(set(second)),
          f"the second connection duplicated a number: {second[:8]}")

    # ---- Nothing survives the link that produced it ---------------------
    # The connection edge used to be handled AFTER the tick had already sent,
    # so a payload built for the previous central could reach the next one.
    peripheral, server, clock = build(gps_hz=25)
    server.connect(subscribe=("gps",))
    server.stall()
    run(peripheral, 400)                   # a payload is now held, undeliverable
    check(peripheral._pending,
          "the test needs a held payload for the rest of it to mean anything")
    dropped_at = clock[0]
    server.disconnect()
    run(peripheral, 20)                    # the pump observes the edge
    server.drain()
    server.clear_wire()
    server.connect(subscribe=("gps",))
    run(peripheral, 300)

    # Checked by CONTENT, not by sequence number. seq is stamped at delivery
    # now, so a stale payload sent to a new central would carry a perfectly
    # fresh 0 -- the number says nothing about when the payload was built. Its
    # timestamp does: anything measured before the link dropped belongs to a
    # central that has gone.
    stale = [f for f in (vtp1.decode_gps_fix(p) for p in server.sent("gps"))
             if f["t_device"] <= dropped_at]
    check(not stale,
          f"{len(stale)} notification(s) built before the link dropped were "
          f"delivered to the central that replaced it")
    check(server.sent("gps"),
          "the new central should be receiving its own notifications")
    restarted = seqs(server.sent("gps"), "gps")
    check(restarted and restarted[0] == 0,
          f"the replacement central must be numbered from 0, got "
          f"{restarted[:1]}")

    # NOTE: `_reset_transport_state()` clearing `_pending` is belt and braces.
    # With this pump a payload held for a dead link is also removed by the send
    # that fails, or superseded by the next one, so removing the clear does not
    # fail this suite. It stays because relying on a side effect of a failed
    # send to enforce a lifecycle rule is how the rule stops holding the moment
    # the send path changes -- but this comment is here rather than a check,
    # because a check that cannot fail is exactly what this repository keeps
    # finding in its own tooling.

    # ---- The first request of a connection is not thrown away -----------
    # The client connects, enables indications on Control, and writes its
    # first request. All three can happen before the pump's next poll, because
    # a GATT write is delivered by callback and `is_connected()` is polled.
    #
    # The request was admitted, applied and queued -- and then the pump saw the
    # connection it had already been serving, ran the connect edge, and cleared
    # the response queue and the device state out from under it. The client's
    # CAN_SUBSCRIBE had taken effect and was never answered, so it retried a
    # request that was already installed and eventually dropped the link.
    #
    # This is the ordinary path, not an exotic one: no stall, no reconnection,
    # nothing refused. The previous version of this test stepped the pump five
    # ticks before writing, and called the loss correct.
    peripheral, server, _ = build(gps_hz=0)
    server.connect(subscribe=("control",))
    peripheral.write_request(
        gattsim.FakeCharacteristic(serve.CHAR["control"]),
        bytes([dev.CAN_SUBSCRIBE, 3]) + b"\xa0\x01\x00\x00\x00\x00\x00")
    run(peripheral, 50)
    answered = server.sent("control")
    check(len(answered) == 1 and answered[0][1] == 3,
          f"a request written before the pump first polled MUST still be "
          f"answered; the wire holds {answered}")
    check(len(peripheral.device.can_table()) == 1,
          f"...and MUST still be in effect: the subscription table holds "
          f"{peripheral.device.can_table()}")

    # The other half of the same rule: a request that arrives before the pump
    # has polled must not ALSO be applied twice, once per edge-taker.
    check(peripheral._link.connected,
          "the write should have taken the connection edge itself")

    # ---- A control response is owed, not offered ------------------------
    peripheral, server, _ = build(gps_hz=0)
    server.connect(subscribe=("control",))
    run(peripheral, 5)
    server.stall()
    peripheral.write_request(
        gattsim.FakeCharacteristic(serve.CHAR["control"]),
        bytes([dev.CAN_RESET, 9]))
    run(peripheral, 50)
    check(not server.sent("control"),
          "the fake refused, so nothing should have reached the wire")
    server.drain()
    run(peripheral, 50)
    got = server.sent("control")
    check(len(got) == 1 and got[0][1] == 9,
          f"a refused control response MUST be retried until it lands, not "
          f"dropped; wire holds {got}")

    # ---- Timed recovery does not send a stream past a held response ------
    # SPEC.md §9.4 -- a held response goes before every notification the
    # device queues after it. The pump's retry timer ran AFTER the control
    # loop and set `_ready` for the stream block of the same pass, so a GPS
    # notification went out while the response the loop had just been refused
    # on was still held. Found by review of PR #62 and reproduced here: the
    # fake's queue empties SILENTLY -- no ready callback, which is the case
    # the timer exists for -- and the first pass after it must deliver the
    # response before anything else, whatever the stream has pending.
    peripheral, server, _ = build(gps_hz=1000)
    server.connect(subscribe=("control", "gps"))
    run(peripheral, 5)
    server.stall()
    peripheral.write_request(
        gattsim.FakeCharacteristic(serve.CHAR["control"]),
        bytes([dev.CAN_RESET, 11]))
    run(peripheral, 5)
    check(len(peripheral._control) == 1 and not peripheral._ready,
          "the stall should have left the response held and the pump blocked")
    server.accepting = True                    # emptied, and nobody said so
    peripheral._blocked_since = time.monotonic() - 1.0   # past RETRY_BLOCKED_S
    gps_before = len(server.sent("gps"))
    run(peripheral, 1)
    check(len(peripheral._control) == 0,
          "the first pass after a silent recovery MUST deliver the held "
          "response; it is still held")
    check(not (len(server.sent("gps")) > gps_before and len(peripheral._control)),
          "a stream notification went out while a control response was held "
          "(SPEC.md 9.4)")

    # ---- Owing ends at the send, over the real pump (SPEC.md 9) ---------
    # The isolated ControlQueue test in selftest.py calls delivered() by hand,
    # so it pins the admission rule but not the wiring that drives it. This
    # drives the REAL pump: the send is `update_value` returning True, and
    # nothing here supplies that event on the queue's behalf.
    peripheral, server, _ = build(gps_hz=0)
    server.connect(subscribe=("control",))
    run(peripheral, 5)
    # Two writes before the pump has run: the first response is composed and
    # UNSENT, so it is owed and the second is refused.
    peripheral.write_request(
        gattsim.FakeCharacteristic(serve.CHAR["control"]),
        bytes([dev.CAN_RESET, 1]))
    peripheral.write_request(
        gattsim.FakeCharacteristic(serve.CHAR["control"]),
        bytes([dev.CAN_RESET, 2]))
    run(peripheral, 50)
    got = server.sent("control")
    check([(r[1], r[2]) for r in got] == [(1, 0), (2, 5)],
          f"a request written while the first response was still unsent MUST "
          f"be refused busy and queued behind it; wire holds "
          f"{[(r[1], r[2]) for r in got]}")

    # Both have now been SENT, so the device owes nothing and the next request
    # is APPLIED. This is the case a confirmation-anchored device gets wrong:
    # it would still be owing both, and would refuse a client that wrote
    # exactly when SPEC.md 9 tells it to.
    peripheral.write_request(
        gattsim.FakeCharacteristic(serve.CHAR["control"]),
        bytes([dev.CAN_SUBSCRIBE, 3]) + b"\xa0\x01\x00\x00\x00\x00\x00")
    run(peripheral, 50)
    got = server.sent("control")
    check(len(got) == 3 and (got[2][1], got[2][2]) == (3, 0),
          f"once every response has been sent the device owes nothing and the "
          f"next request MUST be applied, not refused; wire holds "
          f"{[(r[1], r[2]) for r in got]}")
    check(len(peripheral.device.can_table()) == 1,
          f"...and it MUST have taken effect: the subscription table holds "
          f"{peripheral.device.can_table()}")

    # ---- OBD_INFO is answered only when the probe completes -------------
    # SPEC.md 15.2 -- the response reports a COMPLETED probe. The device
    # answers RESPONSE_PENDING and the pump collects the reply from
    # due_control_response(); this drives the REAL pump over the fake link
    # and pins that no indication reaches the wire while the probe's
    # request windows are still running, that the reply lands once they
    # have, and that nothing is still scheduled ahead of the clock then.
    peripheral, server, clock = build(gps_hz=0)
    server.connect(subscribe=("control", "can"))
    run(peripheral, 5)
    peripheral.write_request(
        gattsim.FakeCharacteristic(serve.CHAR["control"]),
        bytes([dev.OBD_INFO, 0x21]))
    run(peripheral, 2)                     # 10 ms: the probe has just begun
    check(not server.sent("control"),
          "the OBD_INFO indication reached the wire before the probe could "
          "have completed (SPEC.md 15.2)")
    delivered_at = None
    for _ in range(200):
        run(peripheral, 1)
        if server.sent("control"):
            delivered_at = clock[0]
            break
    check(delivered_at is not None, "the OBD_INFO indication never arrived")
    if delivered_at is not None:
        answered = server.sent("control")
        check(len(answered) == 1 and answered[0][:3] ==
              bytes([dev.OBD_INFO, 0x21, dev.ST_OK]),
              f"the probe's indication must answer ok under its own tag; "
              f"the wire holds {[a.hex() for a in answered]}")
        check(vtp1.decode_obd_info(answered[0][3:])["probe"]["count"] == 2,
              "the delivered detail must report the completed probe: both "
              "synthetic ECUs")
        check(delivered_at >=
              peripheral.device._obd_last_tx_us + 50_000,
              "the indication was delivered before the 50 ms collection "
              "window after the probe's final request (SPEC.md 15.2)")
        check(all(t <= delivered_at
                  for t, _, _ in peripheral.device._obd_rx),
              "a probe event was still scheduled ahead of the clock when "
              "the indication was delivered (SPEC.md 15.2)")

    # ---- What bless 0.3.0 actually reports ------------------------------
    # `is_connected()` on that backend is `len(_central_subscriptions) > 0`,
    # not a link flag: a CoreBluetooth peripheral is never told about a connect
    # or a disconnect at all. So a client that unsubscribes from every
    # characteristic while staying connected is indistinguishable from one that
    # went away, and resubscribing is indistinguishable from a new connection.
    #
    # This pins what the peripheral DOES about that, because the behaviour is
    # a deliberate choice between two unequal mistakes and not an accident:
    # it resets, exactly as it would for a real reconnection. See
    # serve.ConnectionTracker for why that is the safe direction.
    peripheral, server, _ = build(gps_hz=25, bless_semantics=True)
    server.connect(subscribe=("gps", "control"))
    run(peripheral, 200)
    peripheral.write_request(
        gattsim.FakeCharacteristic(serve.CHAR["control"]),
        bytes([dev.CAN_SUBSCRIBE, 4]) + b"\xa0\x01\x00\x00\x00\x00\x00")
    run(peripheral, 50)
    check(len(peripheral.device.can_table()) == 1,
          "the fixture needs an installed subscription to say anything")
    before = seqs(server.sent("gps"), "gps")
    check(len(before) > 1, "the fixture needs several notifications delivered")

    # Every CCCD cleared, link still up. On this backend that reads as a
    # disconnect, and the peripheral treats it as one.
    server.unsubscribe("gps")
    server.unsubscribe("control")
    run(peripheral, 20)
    check(peripheral.device.can_table() == [],
          "on a backend whose is_connected() means `something is subscribed`, "
          "clearing every CCCD reads as a disconnect and MUST clear the "
          "subscription table -- not resetting here would hand a genuine "
          "reconnection the previous link's table (SPEC.md 9.2)")

    server.clear_wire()
    server.subscribe("gps")
    run(peripheral, 300)
    after = seqs(server.sent("gps"), "gps")
    check(after and after[0] == 0,
          f"resubscribing reads as a new connection on this backend, so seq "
          f"MUST restart at 0; got {after[:1]}. The alternative -- carrying "
          f"seq across what might have been a real reconnection -- is the one "
          f"a client cannot detect (SPEC.md 8.2)")

    # And the identity is carried, so the log can say which it probably was.
    check(peripheral._link.central == server.central,
          f"the tracker should be carrying the central's identity, got "
          f"{peripheral._link.central!r}")

    # ---- The pump reaches its target rate -------------------------------
    # SPEC.md has nothing to say about this; every stream does. Each one is
    # sized against poll_hz, so a pump that runs under it delivers every
    # stream under its configured rate and reports none of them as short.
    #
    # The loop used to await a fixed `interval` at the END of the tick, after
    # variable work, making the period `work + interval` -- it could approach
    # the target from below but never reach it. Measured against a client, a
    # nominal 200 Hz ran at ~172 and every stream came out ~14% under. The
    # fix is an absolute deadline advanced by exactly `interval`, and it is
    # invisible: nothing in the delivered bytes distinguishes the two, which
    # is why it is pinned here rather than left to be noticed again.
    marks, tick_hz = pump(lambda _n: 0.003, 401)
    rate = (len(marks) - 1) / (marks[-1] - marks[0])
    check(abs(rate - 200) < 2,
          f"3 ms of work per tick at a 5 ms interval must still tick at "
          f"200 Hz; got {rate:.1f}. A fixed sleep AFTER the work gives 125 "
          f"here, because the work is added to the period instead of "
          f"absorbed by it")
    check(abs(tick_hz - 200) < 4,
          f"the pump must report the rate it achieved; it reported "
          f"{tick_hz:.1f} while ticking at {rate:.1f}")

    # ---- Time lost while behind is not repaid as a burst ----------------
    # The other half of the deadline: when a tick overruns, the missed ticks
    # are abandoned and the deadline is taken from now. Carrying the debt
    # instead would run the backlog back-to-back the moment the work eased --
    # a burst of notifications the link never asked for, on a loop that has
    # nothing else to give them room. 100 ticks of 12 ms overrun leaves a
    # 700 ms debt; what follows must still be 200 Hz, not 345.
    marks, _ = pump(lambda n: 0.012 if n < 100 else 0.0005, 401)
    recovered = (len(marks) - 101) / (marks[-1] - marks[100])
    check(abs(recovered - 200) < 2,
          f"after falling 700 ms behind, the pump must resume at 200 Hz and "
          f"not repay the debt in a burst; it ran the next 300 ticks at "
          f"{recovered:.1f} Hz")

    # ---- Falling behind is reported, not hidden -------------------------
    # 12 ms of work cannot be ticked at 200 Hz by any scheduling, and the
    # honest report of that is a rate below the target rather than a number
    # copied from poll_hz. This is what makes can_bus_rates(), which models
    # the ideal poll grid, checkable against what the pump actually did.
    marks, tick_hz = pump(lambda _n: 0.012, 401)
    rate = (len(marks) - 1) / (marks[-1] - marks[0])
    check(abs(rate - 1 / 0.012) < 1 and abs(tick_hz - 1 / 0.012) < 1,
          f"work that overruns the interval must show as a tick rate below "
          f"the target: ran {rate:.1f}, reported {tick_hz:.1f}, expected "
          f"{1 / 0.012:.1f}")

    if problems:
        print(f"\n{len(problems)} transport problem(s).", file=sys.stderr)
        return 1
    total = sum(len(v) for v in server.wire.values())
    print(f"Transport conforms: sequence integrity across subscription, "
          f"backpressure and reconnection; nothing outlives its link; control "
          f"responses are retried; the bless 0.3.0 subscription-as-link "
          f"signal resets in the safe direction; and the pump holds its "
          f"target rate under load without repaying lost time as a burst.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
