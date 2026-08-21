#!/usr/bin/env python3
"""Present the synthetic device as a real BLE peripheral.

The transport shell, and deliberately thin: everything worth testing lives in
vtp_device.py, which has no Bluetooth dependency and is checked by selftest.py
on machines with no adapter. This file is the part that can only be verified by
connecting a client to it.

    pip install bless
    python3 reference/peripheral/serve.py

Platform notes, because they change what this can demonstrate:

  macOS   Works. But CoreBluetooth's peripheral role accepts only a local name
          and service UUIDs in an advertisement, so the three-byte Service Data
          of SPEC.md §3.3 CANNOT be advertised from a Mac. A client will find
          the device and must read Info to learn its capabilities — which is
          what §3.3 requires anyway, Service Data being advisory. It is the one
          part of the specification a Mac cannot exercise.

  Linux   BlueZ can advertise arbitrary Service Data, so §3.3 is reachable
          there. `btmgmt` can also set the connection parameters and PHY that
          §2.1–§2.3 ask for, which no desktop API exposes to an application.

Nothing here can validate SPEC.md §8's clock discipline or the timing bounds of
§6.1: a host operating system's scheduler is not an MCU's. This makes a client
developable. It does not make the protocol proven on hardware.
"""
import argparse
import asyncio
import collections
import json
import logging
import pathlib
import struct
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import vtp_device as dev  # noqa: E402
import display as disp  # noqa: E402

# Imported lazily, in _load_bless(), rather than at module scope. Half this
# file is transport-independent -- the advertisement budget, the connection
# edge detection -- and the selftest checks those on a machine that has no
# Bluetooth and therefore no reason to install a Bluetooth library. Exiting the
# process on a missing import made importing this module for two pure functions
# take the whole test run down with it.
BlessServer = GATTCharacteristicProperties = GATTAttributePermissions = None


def _load_bless():
    global BlessServer, GATTCharacteristicProperties, GATTAttributePermissions
    if BlessServer is not None:
        return
    try:
        from bless import (BlessServer as _Server,
                           GATTCharacteristicProperties as _Props,
                           GATTAttributePermissions as _Perms)
    except ImportError:
        raise RuntimeError(
            "bless is required to run the peripheral: pip install bless"
        ) from None
    BlessServer, GATTCharacteristicProperties = _Server, _Props
    GATTAttributePermissions = _Perms

UUIDS = json.loads((ROOT / "schema" / "uuids.json").read_text())
SERVICE = UUIDS["service"]["vtp1"]
CHAR = UUIDS["characteristics"]

log = logging.getLogger("vtp.peripheral")

# Launched through LaunchServices there is no terminal to write to, so the log
# goes somewhere findable instead of nowhere.
LOG_FILE = "/tmp/vtp-peripheral.log"


def _in_app_bundle():
    return "/Contents/MacOS/" in sys.executable


# Names for the log. A control write that arrives as eleven hex bytes tells a
# reader nothing; the same write named as CAN_SUBSCRIBE with its parameters is
# the difference between diagnosing a client and guessing at one.
OPCODES = {
    0x01: "CAN_RESET", 0x02: "CAN_SUBSCRIBE", 0x03: "CAN_SUBSCRIBE_MASK",
    0x04: "CAN_UNSUBSCRIBE", 0x05: "CAN_LIST", 0x10: "GPS_SET_RATE",
    0x20: "IMU_SET_RATE", 0x30: "TIME_SYNC", 0x31: "GET_LINK_PARAMS",
    0x40: "MONITOR_LIST",
}
STATUSES = {
    0: "ok", 1: "unsupported_opcode", 2: "bad_params", 3: "table_full",
    4: "rate_exceeded", 5: "busy", 6: "needs_encryption", 7: "unknown_handle",
}
CHAR_NAMES = {}


class ConnectionTracker:
    """Edge detection over a connection flag.

    Separated out and kept pure because the two things that hang off an edge
    are not cosmetic: a rising edge is where SPEC.md §8.2 restarts sequence
    numbers and §9.2 clears the subscription table, and a device that never
    sees an edge never does either. This peripheral did not, for its whole
    life, because the transport never told it.
    """

    def __init__(self):
        self.connected = False

    def update(self, is_connected):
        """Return 'connected', 'disconnected', or None when nothing changed."""
        if is_connected == self.connected:
            return None
        self.connected = is_connected
        return "connected" if is_connected else "disconnected"


def _describe_request(value):
    if len(value) < 2:
        return f"<{len(value)} bytes, not a request>"
    opcode, tag, params = value[0], value[1], value[2:]
    name = OPCODES.get(opcode, f"0x{opcode:02X}")
    detail = ""
    if opcode in (0x02, 0x03) and len(params) >= 7:
        cid, mode, arg = struct.unpack("<IBH", params[:7] if opcode == 0x02
                                       else params[:4] + params[8:11])
        detail = f" id=0x{cid & 0x1FFFFFFF:03X} mode={mode} arg={arg}"
    elif opcode == 0x05 and len(params) == 2:
        detail = f" start={struct.unpack('<H', params)[0]}"
    return f"{name} tag={tag}{detail} params={params.hex() or '-'}"


# A BLE advertisement is 31 bytes: 3 for flags, 18 for one 128-bit service UUID
# (2 header + 16), leaving 10 for everything else — and a local name costs 2
# bytes of header on top of its characters.
ADVERTISEMENT_BYTES = 31
_FLAGS_BYTES, _UUID128_BYTES, _AD_HEADER = 3, 18, 2
MAX_NAME_CHARS = ADVERTISEMENT_BYTES - _FLAGS_BYTES - _UUID128_BYTES - _AD_HEADER


def check_advertisement_fits(name):
    """Return a complaint if `name` will not fit beside the service UUID.

    An over-long name does not truncate: the packet overflows and the host
    stack drops a whole element. If what it drops is the service UUID, a client
    scanning for that UUID never sees the device at all, and nothing in the log
    says so — the peripheral reports itself as advertising perfectly happily.
    """
    if len(name) <= MAX_NAME_CHARS:
        return None
    return (f"local name {name!r} is {len(name)} characters; only "
            f"{MAX_NAME_CHARS} fit beside the 128-bit service UUID in a "
            f"{ADVERTISEMENT_BYTES}-byte advertisement. The packet overflows "
            f"and the service UUID may be dropped, which makes this device "
            f"invisible to any client scanning for it.")


class Peripheral:
    STREAM_ORDER = ("gps", "can", "imu")

    def __init__(self, device, name="VTP Logger", screen=None):
        self.device = device
        self.name = name
        self.server = None
        self.screen = screen
        self._link = ConnectionTracker()
        # Everything the debug panel shows. Kept here rather than in the device
        # because it is transport truth, not device truth: how many
        # notifications the stack accepted is not something the device knows.
        self.sent = {"gps": 0, "can": 0, "imu": 0}
        self.refused = {"gps": 0, "can": 0, "imu": 0}
        self.unwanted = {"gps": 0, "can": 0, "imu": 0}
        self.rate = {"gps": 0.0, "can": 0.0, "imu": 0.0}
        self.control_log = collections.deque(maxlen=8)
        self.started = time.monotonic()
        self._turn = 0
        # Backpressure. The host stack refuses when its transmit queue is full
        # and calls back when it has drained; firing regardless just converts
        # the overflow into loss. At most one notification per stream is held,
        # so a slow link delays data rather than queueing it without bound.
        self._ready = True
        self._blocked_since = None
        self._pending = {}
        self._paint_ms = self._pump_ms = 0.0
        self._paints = 0
        # If the stack never calls back, every refusal costs the 250 ms safety
        # timeout instead, which would throttle far harder than the refusal
        # itself. Counted rather than assumed.
        self._ready_callbacks = 0
        self._timeouts = 0
        # Control responses awaiting delivery. A notification may be discarded
        # and reported (SPEC.md §8.3); a control response may NOT. §9 requires
        # a device to respond to every request, and a client that never sees an
        # answer waits on its tag until it gives up and drops the link -- which
        # it did. Worse, the request had already been APPLIED, so the two ends
        # disagreed about the subscription table.
        self._control_out = collections.deque()
        self._control_dropped = 0
        self._notify = {"gps": CHAR["gps"], "can": CHAR["can"],
                        "imu": CHAR["imu"]}

    # -- GATT callbacks ---------------------------------------------------

    def read_request(self, characteristic, **kwargs):
        """Info is regenerated per read: SPEC.md §4 forbids a client caching it
        across connections precisely because it can change."""
        name = CHAR_NAMES.get(characteristic.uuid.lower(), characteristic.uuid)
        log.info("READ  %s", name)
        if characteristic.uuid.lower() == CHAR["info"].lower():
            return self.device.info()
        return characteristic.value or b""

    def write_request(self, characteristic, value, **kwargs):
        uuid = characteristic.uuid.lower()
        if uuid == CHAR["monitor_values"].lower():
            # SPEC.md §13.4 — the one direction that runs client-to-device.
            problem = self.device.handle_monitor_write(bytes(value))
            if problem:
                log.warning("rejected a monitor update: %s", problem)
            else:
                # The screen is refreshed from the poll loop, not from here:
                # this callback does not run on the loop that owns the window,
                # and Tk is not thread-safe.
                pass
            return
        if uuid != CHAR["control"].lower():
            return
        request = bytes(value)
        response = self.device.handle_control(request)
        if response is not None:
            described = _describe_request(request)
            status = STATUSES.get(response[2], f"0x{response[2]:02X}")
            log.info("CTRL  %s -> %s", described, status)
            self.control_log.append(
                (time.strftime("%H:%M:%S"), described.split(" params=")[0],
                 status))
        if response is None:
            # Too short to carry a tag, so there is nothing to correlate a
            # reply with. SPEC.md §9 requires a response to every *request*;
            # two bytes are the minimum that constitutes one.
            log.warning("control write of %d byte(s) is not a request",
                        len(value))
            return
        # Queued rather than sent from here: this callback does not run on the
        # loop that owns the transport, and a refused response must be retried
        # rather than dropped.
        self._control_out.append(response)

    def _subscribed(self):
        """Characteristic names a central has enabled notifications on.

        A GATT subscription and a VTP CAN_SUBSCRIBE are different things and it
        is easy to have one without the other: the control opcode tells the
        device which arbitration ids to forward, while this is the client's
        stack agreeing to carry the notifications at all. A device with three
        CAN ids installed and no subscriber on the CAN characteristic produces
        batches that go nowhere, and the only visible symptom is that
        update_value keeps returning false.

        Reaches into bless's delegate because nothing public exposes it.
        """
        try:
            subs = self.server.peripheral_manager_delegate._central_subscriptions
        except AttributeError:
            return None
        names = set()
        for chars in subs.values():
            for uuid in chars:
                names.add(CHAR_NAMES.get(uuid.lower(), uuid))
        return names

    # -- lifecycle --------------------------------------------------------

    def _install_ready_hook(self):
        """Learn when the transmit queue has drained.

        CoreBluetooth calls peripheralManagerIsReadyToUpdateSubscribers: after
        refusing, and that callback is the documented way to pace a peripheral.
        bless only logs it, so the delegate method is wrapped here. Patched on
        the class because PyObjC dispatches through the class rather than the
        instance; harmless with one peripheral per process, which is the only
        shape this file supports.
        """
        from bless.backends.corebluetooth.peripheral_manager_delegate import (
            PeripheralManagerDelegate as Delegate)
        name = "peripheralManagerIsReadyToUpdateSubscribers_"
        if getattr(Delegate, "_vtp_ready_hook", False):
            return
        original = getattr(Delegate, name)
        peripheral = self

        def patched(delegate_self, manager):
            peripheral._ready = True
            peripheral._blocked_since = None
            peripheral._ready_callbacks += 1
            return original(delegate_self, manager)

        setattr(Delegate, name, patched)
        Delegate._vtp_ready_hook = True

    def _deliver(self, characteristic, payload, sent, refused):
        """One attempt. Returns True when the stack took it."""
        uuid = self._notify[characteristic]
        self.server.get_characteristic(uuid).value = payload
        if self.server.update_value(SERVICE, uuid):
            sent[characteristic] += 1
            return True
        self._ready = False
        self._blocked_since = time.monotonic()
        refused[characteristic] += 1
        return False

    async def start(self):
        _load_bless()
        CHAR_NAMES.update({v.lower(): k for k, v in CHAR.items()})
        # macOS TERMINATES any process that creates a CBPeripheralManager
        # without an NSBluetoothAlwaysUsageDescription in its Info.plist --
        # killed outright, no exception to catch, nothing on stderr. It is not
        # a permission that can be granted after the fact: the process dies
        # before it can ask, so it never appears in System Settings, and that
        # pane has no way to add one by hand. Run it from the bundle that
        # make_macos_app.sh builds, via `open`, and note that exec'ing the
        # bundle's binary directly fails identically -- a directly-launched
        # Mach-O uses its embedded __info_plist section, not the file.
        if sys.platform == "darwin" and not _in_app_bundle():
            log.error("not running from an app bundle: macOS will kill this "
                      "process when it creates the peripheral manager")
            log.error("build one with reference/peripheral/make_macos_app.sh, "
                      "then: open -a VTPPeripheral.app --args serve.py")
        self.server = BlessServer(name=self.name)
        self.server.read_request_func = self.read_request
        self.server.write_request_func = self.write_request

        log.info("creating service %s", SERVICE)
        await self.server.add_new_service(SERVICE)

        props, perms = GATTCharacteristicProperties, GATTAttributePermissions
        read, notify = props.read, props.notify
        write, indicate = props.write, props.indicate
        readable, writeable = perms.readable, perms.writeable

        # CoreBluetooth: "Characteristics with cached values must be
        # read-only". Only Info may carry an initial value; anything
        # notifiable or writable must be created with None, or addService_
        # raises NSInternalInconsistencyException.
        # Logged one at a time: a GATT call that never returns is otherwise
        # indistinguishable from any other, and one of them did.
        async def add(name, props, value, perms):
            log.info("adding characteristic %s", name)
            await self.server.add_new_characteristic(
                SERVICE, CHAR[name], props, value, perms)

        await add("info", read, self.device.info(), readable)
        for name in ("gps", "can", "imu"):
            await add(name, notify, None, readable)
        await add("control", write | indicate, None, readable | writeable)
        # The client writes values here; the device only ever reads them.
        await add("monitor_values", write, None, readable | writeable)

        await self.server.start()
        try:
            self._install_ready_hook()
        except Exception:
            log.warning("could not hook the ready-to-send callback; the "
                        "peripheral will pace on a timer instead",
                        exc_info=True)
        log.info("advertising %s as %r", SERVICE, self.name)
        log.info("a client matching on the service UUID needs that UUID in the "
                 "advertisement; name is %d of %d permitted characters",
                 len(self.name), MAX_NAME_CHARS)
        log.info("Service Data (SPEC.md 3.3) is not advertised: the host "
                 "peripheral API does not expose it on every platform")

    async def run(self, poll_hz=200, screen_hz=10):
        interval = 1.0 / poll_hz
        ticks = 0
        every = max(1, poll_hz // screen_hz)
        # Counted per characteristic. A single total hides the one question a
        # reader of this log actually has, which is which stream is silent.
        sent, refused, unwanted = self.sent, self.refused, self.unwanted
        next_report = 0.0
        next_rate = time.monotonic() + 1.0
        last_counts = dict(sent)
        while True:
            subscribed = self._subscribed()

            # Control responses first, and retried until they land. They are
            # the one thing on this link that is owed rather than offered.
            while self._control_out and self._ready:
                response = self._control_out[0]
                control = self.server.get_characteristic(CHAR["control"])
                control.value = response
                if self.server.update_value(SERVICE, CHAR["control"]):
                    self._control_out.popleft()
                else:
                    self._ready = False
                    self._blocked_since = time.monotonic()
                    break

            # New work. At most one notification per stream is held: a second
            # supersedes the first, and the superseded one is loss and is
            # counted as such. Holding more would deliver a backlog, which
            # SPEC.md §8.3 is explicit is the wrong answer.
            for characteristic, payload in self.device.poll():
                if subscribed is not None and characteristic not in subscribed:
                    unwanted[characteristic] += 1
                    continue
                stale = self._pending.get(characteristic)
                if stale is not None:
                    self.device.record_refused(characteristic, stale)
                    refused[characteristic] += 1
                self._pending[characteristic] = payload

            # A refusal we never got a callback for must not wedge the device.
            if (not self._ready and self._blocked_since
                    and time.monotonic() - self._blocked_since > 0.25):
                self._ready = True
                self._blocked_since = None
                self._timeouts += 1

            # Rotate which stream is offered first. The queue is finite, and a
            # fixed order means the LAST stream absorbs every refusal: with
            # GPS, IMU and CAN all subscribed, CAN was refused almost in full
            # while the other two flowed, purely because it was sent last.
            if self._ready and self._pending:
                self._turn = (self._turn + 1) % len(self.STREAM_ORDER)
                order = (self.STREAM_ORDER[self._turn:]
                         + self.STREAM_ORDER[:self._turn])
                for characteristic in order:
                    payload = self._pending.get(characteristic)
                    if payload is None:
                        continue
                    if self._deliver(characteristic, payload, sent, refused):
                        del self._pending[characteristic]
                    else:
                        self.device.record_refused(characteristic, payload)
                        del self._pending[characteristic]
                        break

            # A rate is what tells a stalled stream from a slow one, and a
            # total never does.
            now_wall = time.monotonic()
            if now_wall >= next_rate:
                span = 1.0
                for name in self.rate:
                    self.rate[name] = (sent[name] - last_counts[name]) / span
                last_counts = dict(sent)
                next_rate = now_wall + span

            if ticks % every == 0:
                event = self._link.update(await self.server.is_connected())
                if event == "connected":
                    # SPEC.md §8.2 and §9.2: a connection starts from a known
                    # state. Without this the device carries the previous
                    # client's subscriptions and sequence numbers into the next
                    # connection, which is exactly what §9.2 forbids.
                    self.device.on_connect()
                    log.info("CLIENT CONNECTED — sequence numbers restarted, "
                             "subscription table cleared")
                elif event == "disconnected":
                    # SPEC.md §9.2 clears the table when the LINK DROPS, not
                    # when the next one starts. Clearing only on connect left
                    # a disconnected device reporting three installed ids with
                    # nobody subscribed, which reads as a client fault.
                    self.device.on_disconnect()
                    # The client is gone; nothing is owed to it any more.
                    if self._control_out:
                        self._control_dropped += len(self._control_out)
                        log.warning("%d control response(s) undelivered when "
                                    "the link dropped", len(self._control_out))
                        self._control_out.clear()
                    log.info("CLIENT DISCONNECTED — subscription table cleared")

            now = self.device.now_us() / 1e6
            if now >= next_report:
                next_report = now + 10.0
                subs = len(self.device._subscriptions)
                subscribed = self._subscribed()
                log.info("sent gps=%d can=%d imu=%d | refused gps=%d can=%d "
                         "imu=%d | no-subscriber gps=%d can=%d imu=%d | "
                         "CAN ids=%d | notify-subscribed: %s",
                         sent["gps"], sent["can"], sent["imu"],
                         refused["gps"], refused["can"], refused["imu"],
                         unwanted["gps"], unwanted["can"], unwanted["imu"],
                         subs,
                         ", ".join(sorted(subscribed)) if subscribed else "none")
                if self._control_out or self._control_dropped:
                    log.info("  control responses: %d awaiting delivery, %d "
                             "lost to a dropped link",
                             len(self._control_out), self._control_dropped)
                if self._paints:
                    log.info("  display: paint %.1f ms, pump %.1f ms, %d paints"
                             "  |  ready-callbacks %d, safety-timeouts %d",
                             self._paint_ms, self._pump_ms, self._paints,
                             self._ready_callbacks, self._timeouts)
                if subs and subscribed is not None and "can" not in subscribed:
                    log.warning(
                        "  %d CAN id(s) are installed but no central has "
                        "subscribed to the CAN characteristic: the device is "
                        "producing batches that go nowhere. A client must "
                        "enable notifications on %s as well as sending "
                        "CAN_SUBSCRIBE.", subs, CHAR["can"])
                if subs == 0:
                    log.info("  no CAN subscription installed, so no CAN "
                             "frames are due: a client must CAN_SUBSCRIBE "
                             "before this device sends any (SPEC.md 9.2)")

            ticks += 1
            if self.screen and ticks % every == 0:
                # A fault in the panel must not take the device down with it.
                # Serving the client is the job; drawing is a convenience, and
                # an IndexError in a grid once killed a running peripheral
                # mid-session.
                try:
                    t0 = time.perf_counter()
                    self.screen.update(self.device.monitor_state(),
                                       self.telemetry(subscribed))
                    t1 = time.perf_counter()
                    alive = self.screen.pump()
                    t2 = time.perf_counter()
                    # Timed rather than reasoned about: the panel turned out to
                    # cost this peripheral a third of its BLE throughput, and
                    # two guesses at why were both wrong.
                    self._paint_ms = (t1 - t0) * 1000
                    self._pump_ms = (t2 - t1) * 1000
                    self._paints += 1
                except Exception:
                    log.exception("the display failed; continuing headless")
                    self.screen.close()
                    self.screen = None
                    alive = True
                if not alive:
                    log.info("display closed; stopping")
                    return
            await asyncio.sleep(interval)

    def telemetry(self, subscribed):
        """Everything the debug panel draws, gathered in one place."""
        return {
            "connected": self._link.connected,
            "uptime": time.monotonic() - self.started,
            "subscribed": subscribed,
            "sent": dict(self.sent),
            "refused": dict(self.refused),
            "unwanted": dict(self.unwanted),
            "rate": dict(self.rate),
            "pending_dropped": self.device.pending_dropped(),
            "can_table": self.device.can_table(),
            "control": list(self.control_log),
            "mtu": self.device.mtu,
            "configured": self.device.rates(),
            "monitor_seq": self.device.monitor_seq,
            "monitor_updates": self.device.monitor_updates,
        }

    async def stop(self):
        if self.server:
            await self.server.stop()


async def main_async(args):
    handlers = [logging.StreamHandler()]
    if not sys.stdout.isatty():
        handlers.append(logging.FileHandler(LOG_FILE, mode="w"))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers)
    if not sys.stdout.isatty():
        log.info("logging to %s", LOG_FILE)

    complaint = check_advertisement_fits(args.name)
    if complaint:
        log.error("%s", complaint)
        log.error("refusing to advertise a packet that may omit the service "
                  "UUID; pass a shorter --name")
        return

    device = dev.VtpDevice(mtu=args.mtu, gps_hz=args.gps_hz,
                           imu_hz=args.imu_hz)

    peripheral = Peripheral(device, name=args.name)
    screen = None
    # Launched through LaunchServices there is no stderr, so an unhandled
    # exception would vanish and look exactly like a silent exit. Everything
    # goes to the log file instead.
    try:
        await peripheral.start()
        # The window is created only after the server is advertising. Tk takes
        # over the main run loop when it initialises, and CoreBluetooth needs
        # that run loop to deliver its power-on callback -- creating the window
        # first leaves BlessServer.start() waiting for an event that can no
        # longer arrive, with the window up and nothing behind it.
        if not args.no_display:
            try:
                screen = disp.MonitorDisplay(title=f"{args.name} — display")
                peripheral.screen = screen
                log.info("display open; close the window to stop the peripheral")
            except RuntimeError as exc:
                log.warning("no display: %s", exc)
        await peripheral.run()
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("the peripheral stopped with an error")
        raise
    finally:
        try:
            await peripheral.stop()
        except Exception:
            log.exception("error while stopping")
        if screen:
            screen.close()
        log.info("stopped")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", default="VTP",
                    help=f"advertised local name, at most {MAX_NAME_CHARS} "
                         f"characters beside the service UUID")
    ap.add_argument("--mtu", type=int, default=247,
                    help="assumed ATT MTU for batch sizing")
    ap.add_argument("--gps-hz", type=int, default=10)
    ap.add_argument("--imu-hz", type=int, default=100)
    ap.add_argument("--no-display", action="store_true",
                    help="run headless; do not open the device screen")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.getLogger("vtp.peripheral").exception("fatal")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
