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
    # Each opcode is guarded by the length its own branch reads. One shared
    # `>= 7` guard let a short CAN_SUBSCRIBE_MASK reach a slice needing 11, and
    # the struct.error took the peripheral down while logging a request the
    # device itself had already handled correctly.
    if opcode == 0x02 and len(params) >= 7:
        cid, mode, arg = struct.unpack("<IBH", params[:7])
        mask = 0x3FFFFFFF
    elif opcode == 0x03 and len(params) >= 11:
        cid, mask, mode, arg = struct.unpack("<IIBH", params[:11])
    else:
        cid = None
    if cid is not None:
        fmt = "ext" if cid & (1 << 29) else "std"
        detail = (f" id=0x{cid & 0x1FFFFFFF:03X}/{fmt} mask=0x{mask:08X} "
                  f"mode={mode} arg={arg}")
    if opcode == 0x05 and len(params) == 2:
        detail = f" start={struct.unpack('<H', params)[0]}"
    return f"{name} tag={tag}{detail} params={params.hex() or '-'}"


# SPEC.md §9 — a device MUST accept at least four outstanding requests. One
# slot beyond that exists only to carry the `busy` refusal itself: a device
# with nothing left to answer with cannot tell a client it is out of room.
CONTROL_QUEUE_DEPTH = 4


class ControlQueue:
    """Decides whether a control request can be answered, before it is applied.

    SPEC.md §9.6 — a device MUST NOT apply a request it cannot answer. That
    makes admission a decision taken *ahead* of dispatch rather than a check on
    the way out, which is the whole point: applying first and answering second
    is the natural order to write the code in, and it is the order that leaves
    a client retrying a request that has already taken effect.

    Holds no Bluetooth state, so the rules above are testable without a radio.
    """

    def __init__(self, depth=CONTROL_QUEUE_DEPTH):
        self.depth = depth
        # (tag, response) pairs awaiting delivery. Outstanding tags are read
        # off this rather than tracked alongside it: two structures that must
        # agree are two structures that can disagree.
        self._out = collections.deque()
        self.dropped = 0

    def __len__(self):
        return len(self._out)

    def outstanding(self, tag):
        return any(t == tag for t, _ in self._out)

    def admit(self, tag):
        """`apply`, `duplicate-tag`, `busy` or `full`.

        Only `apply` permits the request to take effect. `duplicate-tag` and
        `busy` are answered but not applied; `full` cannot even be answered.
        """
        if self.outstanding(tag):
            return "duplicate-tag"
        if len(self._out) < self.depth:
            return "apply"
        if len(self._out) == self.depth:
            return "busy"
        # The client was told `busy` and wrote again anyway. There is no room
        # left to refuse it with, so the request is discarded unapplied --
        # which is the correct half of §9.6 when the other half is impossible.
        return "full"

    def hold(self, tag, response):
        self._out.append((tag, response))

    def peek(self):
        return self._out[0][1] if self._out else None

    def delivered(self):
        self._out.popleft()

    def discard_all(self):
        n = len(self._out)
        self.dropped += n
        self._out.clear()
        return n


# SPEC.md §10 — a device MAY require an encrypted link on any characteristic,
# all of them, or none, and a client MUST support whichever it meets. These are
# the three postures this peripheral can present, kept as a pure function so
# the selftest can check them without a radio.
ENCRYPTION_POSTURES = ("all", "control", "none")


def encrypted_characteristics(posture):
    """Names of the characteristics that require an encrypted link.

    Info is absent from every posture: §10.2 says to leave it readable so that
    a client which cannot pair can still identify what it found rather than
    reporting a device that is present, advertising a VTP service and
    apparently broken.
    """
    if posture == "all":
        return {"gps", "can", "imu", "control", "monitor_values"}
    if posture == "control":
        return {"control"}
    if posture == "none":
        return set()
    raise ValueError(f"unknown encryption posture {posture!r}")


# SPEC.md §3.4 SHOULD — the Device Information Service. It carries no protocol
# meaning, which is exactly why it is worth exposing: when someone asks which
# firmware is on a logger that is misbehaving, this is where every generic BLE
# tool already looks.
DIS_SERVICE = "0000180A-0000-1000-8000-00805F9B34FB"
DIS_CHARS = {
    "manufacturer": ("00002A29-0000-1000-8000-00805F9B34FB", "Lapsmith"),
    "model":        ("00002A24-0000-1000-8000-00805F9B34FB", "VTP Reference Peripheral"),
    "firmware":     ("00002A26-0000-1000-8000-00805F9B34FB", None),   # filled from the spec version
    "serial":       ("00002A25-0000-1000-8000-00805F9B34FB", "SOFTWARE-0001"),
}


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

    def __init__(self, device, name="VTP Logger", screen=None, encrypt="all"):
        self.device = device
        self.name = name
        # SPEC.md §10 leaves this to the device, and requires every client to
        # cope with whatever the device chose. This peripheral's job is to
        # exercise that client obligation, so it defaults to the demanding end:
        # "all" protects every characteristic except Info, which §10.2 says to
        # leave readable so a client that cannot pair can still identify what
        # it found. "control" is the common-but-incoherent arrangement §10.2
        # warns about, kept so a client can be tested against it. "none" is a
        # device that protects nothing, which §10 equally permits.
        assert encrypt in ENCRYPTION_POSTURES
        self.encrypt = encrypt
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
        # Monitor updates the device refused. Accepted ones are counted by the
        # device itself, because whether an update was applied is device truth;
        # only the refusal is observed out here.
        self._monitor_rejected = 0
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
        # The MTU the link actually negotiated, once a central tells us.
        self._observed_mtu = None
        # Control responses awaiting delivery. A notification may be discarded
        # and reported (SPEC.md §8.3); a control response may NOT. §9 requires
        # a device to respond to every request, and a client that never sees an
        # answer waits on its tag until it gives up and drops the link -- which
        # it did. Worse, the request had already been APPLIED, so the two ends
        # disagreed about the subscription table.
        self._control = ControlQueue()
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
                self._monitor_rejected += 1
                log.warning("rejected a monitor update: %s", problem)
            elif self.device.monitor_updates == 1:
                # Only the FIRST accepted update is logged, and it is logged in
                # full. A client may write at its own display rate, so a line
                # per update would bury the control conversation -- but "has
                # this client ever written anything at all" is the question an
                # empty Monitor panel raises, and nothing here answered it: an
                # accepted write was silent, so a client that sent nothing and
                # a client that sent something looked identical in the log.
                # One line settles it. The running count is in the status line.
                log.info("MONITOR  first update from this client: seq=%s  |  %s",
                         self.device.monitor_seq,
                         "  ".join(self.device.display_lines()))
            # The screen is refreshed from the poll loop, not from here: this
            # callback does not run on the loop that owns the window, and Tk is
            # not thread-safe.
            return
        if uuid != CHAR["control"].lower():
            return
        request = bytes(value)
        if len(request) < 2:
            # Too short to carry a tag, so there is nothing to correlate a
            # reply with. SPEC.md §9 requires a response to every *request*;
            # two bytes are the minimum that constitutes one.
            log.warning("control write of %d byte(s) is not a request",
                        len(value))
            return
        opcode, tag = request[0], request[1]

        # SPEC.md §9.6 — everything that could stop this response reaching the
        # client is decided here, BEFORE the device sees the request.
        subscribed = self._subscribed()
        if subscribed is not None and "control" not in subscribed:
            # No indications enabled: the answer has nowhere to go, so the
            # request MUST NOT take effect. A client that writes before
            # subscribing is violating §9.6 and would otherwise leave the two
            # ends disagreeing about the subscription table.
            self._note_control(request, "discarded: no indication subscriber")
            log.warning("CTRL  %s -> DISCARDED unapplied: the client has not "
                        "enabled indications on Control (SPEC.md 9.6)",
                        _describe_request(request))
            return

        verdict = self._control.admit(tag)
        if verdict == "full":
            self._note_control(request, "discarded: queue full")
            log.warning("CTRL  %s -> DISCARDED unapplied: %d response(s) "
                        "already awaiting delivery and the client kept writing",
                        _describe_request(request), len(self._control))
            self._control.dropped += 1
            return
        if verdict == "duplicate-tag":
            # SPEC.md §9 — the tag is the only means of correlation, so a
            # second request bearing an outstanding one is refused rather than
            # applied. The refusal necessarily echoes the same tag; that
            # ambiguity is the client's own doing and this is what tells it so.
            response = bytes([opcode, tag, 2])          # bad_params
        elif verdict == "busy":
            response = bytes([opcode, tag, 5])          # busy
        else:
            response = self.device.handle_control(request)
            if response is None:
                return

        status = STATUSES.get(response[2], f"0x{response[2]:02X}")
        log.info("CTRL  %s -> %s", _describe_request(request), status)
        self._note_control(request, status)
        # Queued rather than sent from here: this callback does not run on the
        # loop that owns the transport, and a refused response must be retried
        # rather than dropped.
        self._control.hold(tag, response)

    def _note_control(self, request, status):
        self.control_log.append(
            (time.strftime("%H:%M:%S"),
             _describe_request(request).split(" params=")[0], status))

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

    def _install_mtu_hook(self):
        """Learn the ATT MTU the link actually negotiated.

        bless keeps only a central's UUID string, so the CBCentral -- the one
        object that knows `maximumUpdateValueLength` -- is reachable only from
        the delegate callback it arrives in. Wrapped on the class for the same
        reason as the ready hook, and for the same one-peripheral-per-process
        caveat.

        Until this existed the device sized every batch from --mtu and never
        found out it was wrong. iOS commonly negotiates 185, not the 247 this
        peripheral assumed, and a notification built for 247 on a 185-byte
        link is one the stack will not carry.
        """
        from bless.backends.corebluetooth.peripheral_manager_delegate import (
            PeripheralManagerDelegate as Delegate)
        name = "peripheralManager_central_didSubscribeToCharacteristic_"
        if getattr(Delegate, "_vtp_mtu_hook", False):
            return
        original = getattr(Delegate, name)
        peripheral = self

        def patched(delegate_self, manager, central, characteristic):
            try:
                # maximumUpdateValueLength is the ATT payload, so the MTU is
                # three bytes more: opcode plus handle.
                peripheral._note_mtu(int(central.maximumUpdateValueLength()) + 3)
            except Exception:
                log.debug("central reported no maximum update length",
                          exc_info=True)
            return original(delegate_self, manager, central, characteristic)

        setattr(Delegate, name, patched)
        Delegate._vtp_mtu_hook = True

    def _note_mtu(self, att_mtu):
        if att_mtu == self._observed_mtu:
            return
        self._observed_mtu = att_mtu
        assumed = self.device.mtu
        self.device.set_negotiated_mtu(att_mtu)
        log.info("ATT MTU negotiated at %d (assumed %d); batches now sized "
                 "for %d payload bytes", att_mtu, assumed,
                 self.device.notify_bytes)
        if att_mtu < dev.MIN_ATT_MTU:
            log.warning("negotiated ATT MTU %d is below the %d SPEC.md 2 "
                        "requires; this link does not meet the specification",
                        att_mtu, dev.MIN_ATT_MTU)

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

        # SPEC.md §10.1 — a device that requires encryption enforces it with
        # the GATT permission, never an application check. The ATT layer then
        # answers with Insufficient Encryption, which every major central stack
        # turns into a pairing attempt on its own.
        # perms(0), not 0: GATTAttributePermissions is a Flag, so `readable | 0`
        # raises. Written once and applied by name, rather than once per group.
        crypt = perms.read_encryption_required | perms.write_encryption_required
        encrypted = encrypted_characteristics(self.encrypt)

        def guard(name):
            return crypt if name in encrypted else perms(0)

        log.info("encryption posture: %s — encrypted: %s (SPEC.md 10 leaves "
                 "this to the device; a client MUST support all of them)",
                 self.encrypt, ", ".join(sorted(encrypted)) or "nothing")

        # SPEC.md §10.2 — Info stays readable whatever the posture, so a client
        # that cannot pair can still identify what it has found and say so,
        # rather than reporting a device that is present, advertising a VTP
        # service and apparently broken.
        await add("info", read, self.device.info(), readable)
        for name in ("gps", "can", "imu"):
            await add(name, notify, None, readable | guard(name))
        await add("control", write | indicate, None,
                  readable | writeable | guard("control"))
        # The client writes values here; the device only ever reads them.
        await add("monitor_values", write, None,
                  readable | writeable | guard("monitor_values"))

        # SPEC.md §3.4 SHOULD.
        log.info("creating service %s (Device Information)", DIS_SERVICE)
        await self.server.add_new_service(DIS_SERVICE)
        for name, (uuid, text) in DIS_CHARS.items():
            if text is None:
                text = (f"VTP/{dev.PROTOCOL_MAJOR}."
                        f"{dev.PROTOCOL_MINOR} reference")
            log.info("adding DIS characteristic %s", name)
            await self.server.add_new_characteristic(
                DIS_SERVICE, uuid, read, text.encode(), readable)

        await self.server.start()
        try:
            self._install_ready_hook()
        except Exception:
            log.warning("could not hook the ready-to-send callback; the "
                        "peripheral will pace on a timer instead",
                        exc_info=True)
        try:
            self._install_mtu_hook()
        except Exception:
            log.warning("could not hook the subscribe callback; the device "
                        "will size batches from --mtu and GET_LINK_PARAMS "
                        "will report no ATT MTU", exc_info=True)
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
            while len(self._control) and self._ready:
                control = self.server.get_characteristic(CHAR["control"])
                control.value = self._control.peek()
                if self.server.update_value(SERVICE, CHAR["control"]):
                    self._control.delivered()
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

            # Every tick, not once per screen refresh. This had been inside
            # the `ticks % every` block, so the link edge was detected at the
            # DISPLAY rate -- 10 Hz, up to 100 ms late, and tied to a setting
            # that has nothing to do with it. In that window the device went on
            # numbering notifications from the previous connection and then
            # reset, so a client saw seq run 1500, 1501, 0: not a gap, which
            # §8.2 defines, but a jump backwards, which it does not.
            event = self._link.update(await self.server.is_connected())
            if event == "connected":
                # SPEC.md §8.2 and §9.2: a connection starts from a known
                # state. Without this the device carries the previous client's
                # subscriptions and sequence numbers into the next connection,
                # which is exactly what §9.2 forbids.
                self.device.on_connect()
                # The device zeroes its accepted-update count here, so this
                # one has to be zeroed alongside it. The two are printed side
                # by side in the status line, and a lifetime counter next to a
                # per-connection one reads as a comparison when it is not: the
                # previous client's malformed writes would be reported against
                # the client that has just arrived.
                self._monitor_rejected = 0
                log.info("CLIENT CONNECTED — sequence numbers restarted, "
                         "subscription table cleared")
            elif event == "disconnected":
                # SPEC.md §9.2 clears the table when the LINK DROPS, not when
                # the next one starts. Clearing only on connect left a
                # disconnected device reporting three installed ids with
                # nobody subscribed, which reads as a client fault.
                self.device.on_disconnect()
                self._observed_mtu = None
                # The client is gone; nothing is owed to it any more.
                undelivered = self._control.discard_all()
                if undelivered:
                    log.warning("%d control response(s) undelivered when the "
                                "link dropped", undelivered)
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
                if len(self._control) or self._control.dropped:
                    log.info("  control responses: %d awaiting delivery "
                             "(depth %d), %d never delivered",
                             len(self._control), self._control.depth,
                             self._control.dropped)
                if self._paints:
                    log.info("  display: paint %.1f ms, pump %.1f ms, %d paints"
                             "  |  ready-callbacks %d, safety-timeouts %d",
                             self._paint_ms, self._pump_ms, self._paints,
                             self._ready_callbacks, self._timeouts)
                monitor_state = self.device.monitor_state()
                if monitor_state or self._monitor_rejected:
                    supplied = sum(1 for *_, present in monitor_state if present)
                    log.info("  monitor: %d channel(s) requested | updates "
                             "accepted=%d rejected=%d | seq=%s | %d of %d "
                             "slot(s) supplied",
                             len(monitor_state), self.device.monitor_updates,
                             self._monitor_rejected,
                             "—" if self.device.monitor_seq is None
                             else self.device.monitor_seq,
                             supplied, len(monitor_state))
                    if (self._link.connected and monitor_state
                            and self.device.monitor_updates == 0):
                        # Silence and refusal look identical on the display --
                        # every slot absent -- and they are opposite faults.
                        # Saying "written nothing" to a client that has written
                        # 147 times and had every one refused sends the reader
                        # to look at MONITOR_LIST and at session state, which
                        # are the two things that are not wrong.
                        if self._monitor_rejected:
                            log.info("    every update this client has written "
                                     "was refused, so every slot on the display "
                                     "reads absent: the client IS writing and "
                                     "the device will not take it. The warnings "
                                     "above name the reason; check the payload "
                                     "against SPEC.md 13.4, not MONITOR_LIST.")
                        else:
                            log.info("    this client has written nothing to "
                                     "monitor_values, so every slot on the "
                                     "display reads absent: on Monitor the "
                                     "device asks and the CLIENT supplies "
                                     "(SPEC.md 13.1). Check it read "
                                     "MONITOR_LIST and has values to send.")
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

    peripheral = Peripheral(device, name=args.name,
                            encrypt=args.encrypt)
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
    ap.add_argument("--encrypt", choices=ENCRYPTION_POSTURES,
                    default="all",
                    help="which characteristics require an encrypted link. "
                         "all (default) protects everything except Info, which "
                         "SPEC.md 10.2 says to leave readable; control "
                         "protects only Control; none protects nothing. All "
                         "three conform -- SPEC.md 10 leaves the choice to the "
                         "device and requires every client to support each of "
                         "them")
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
