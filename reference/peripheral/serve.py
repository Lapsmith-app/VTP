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
sys.path.insert(0, str(ROOT / "reference" / "python"))

import vtp_device as dev  # noqa: E402
import display as disp  # noqa: E402
import vtp1_encode as enc  # noqa: E402

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

# How often the pump polls the device for notifications that are due. It also
# bounds every rate the device can be asked for: a stream produces at most one
# notification per poll, so nothing configured above this can be met.
POLL_HZ = 200


def _in_app_bundle():
    return "/Contents/MacOS/" in sys.executable


# Names for the log. A control write that arrives as eleven hex bytes tells a
# reader nothing; the same write named as CAN_SUBSCRIBE with its parameters is
# the difference between diagnosing a client and guessing at one.
#
# Read from the schema rather than restated. Hand-written, this named eight of
# the twelve opcodes: GET_POWER and the three GNSS aiding opcodes were added to
# the specification and never added here, so every aiding exchange and every
# power read reached the log as a bare `0x50` -- unnamed in the one artefact a
# client is diagnosed from, and unnamed in the way that invites the reader to
# conclude the device did not recognise it. A table that has to be edited in
# step with the schema is a table that will not be.
OPCODES = {op["value"]: op["name"] for op in enc.SCHEMA["control"]["opcodes"]}
STATUSES = {m["value"]: m["name"]
            for m in enc.SCHEMA["enums"]["status"]["members"]}
CHAR_NAMES = {}

#: How long to wait after a refused notification before trying again anyway.
#:
#: CoreBluetooth answers "ready to update subscribers" on a callback, and that
#: remains the fast path -- but it is a callback this process does not own, and
#: waiting on it alone once wedged the device, so there has always been a
#: fallback. The fallback was 0.25 s, which is two and a half CAN flush periods
#: (vtp_device flushes a partial batch every 100 ms): a single missed callback
#: therefore guaranteed that two or three batches were built, superseded and
#: counted as loss before anything could be sent again. It was the difference
#: between "the radio was busy for a moment" and "the device is shedding".
#:
#: `update_value` reports the queue state itself on every attempt, so the cost
#: of trying early is one call that returns False. At 200 Hz this retries
#: within two ticks, and a stall shorter than one flush period now costs
#: nothing at all.
RETRY_BLOCKED_S = 0.01


class ConnectionTracker:
    """Edge detection over "a central is being served".

    Separated out and kept pure because the two things that hang off an edge
    are not cosmetic: a rising edge is where SPEC.md 8.2 restarts sequence
    numbers and 9.2 clears the subscription table, and a device that never
    sees an edge never does either. This peripheral did not, for its whole
    life, because the transport never told it.

    WHAT THIS EDGE ACTUALLY IS, which is not what it looks like
    ----------------------------------------------------------
    It is NOT a physical connect or disconnect. A CoreBluetooth peripheral is
    never told about either one: the delegate has exactly two central-facing
    callbacks, `didSubscribeToCharacteristic` and
    `didUnsubscribeFromCharacteristic`, and no connect/disconnect pair at all.
    bless 0.3.0's `is_connected()` therefore reports
    `len(_central_subscriptions) > 0` -- "at least one central is subscribed to
    at least one characteristic". That is the only link-ish signal the platform
    offers a peripheral, and this file used to call it a connection without
    saying so.

    The difference is visible: a client that unsubscribes from every
    characteristic while staying connected looks exactly like a disconnect, and
    resubscribing looks exactly like a new connection.

    WHY THE EDGE IS STILL TAKEN, AMBIGUITY AND ALL
    ---------------------------------------------
    Because the two possible mistakes are not symmetric.

      * Resetting on a resubscribe that was not a reconnection costs the
        client its CAN subscription table and restarts `seq`. A client must
        already tolerate both -- they are what every real reconnection does --
        and it can see the restart in the very next notification.

      * NOT resetting on a reconnection that was not a resubscribe hands the
        next connection the previous one's sequence numbers and subscription
        table. SPEC.md 8.2 exists so a client never has to tell a reconnection
        from a wrap, and 9.2 exists so it never inherits state it did not
        install. A client cannot detect either failure.

    The second is unrecoverable and silent, so where the signal is ambiguous
    this errs towards resetting. `central` is tracked alongside so the log can
    say which of the two it probably was -- a same-identity rising edge is
    most likely a resubscribe, a different identity is certainly a new central
    -- but both reset, because "probably" is not something to bet a client's
    subscription table on.

    A backend that does expose a real link edge (the fake transport in
    gattsim.py can be told to, and a BlueZ peripheral has one) feeds it in here
    unchanged and gets exactly the semantics the names suggest.
    """

    def __init__(self):
        self.connected = False
        self.central = None

    def update(self, is_connected, central=None):
        """Return 'connected', 'disconnected', or None when nothing changed.

        `central` is the identity of the central being served, when the backend
        exposes one. It never suppresses an edge; it is carried so the caller
        can describe what it saw.
        """
        if is_connected == self.connected:
            # A different central without the flag ever dropping: on a backend
            # that reports subscriptions, one central replacing another between
            # two polls. Definitely a new connection.
            if is_connected and central is not None and central != self.central:
                self.central = central
                return "connected"
            return None
        self.connected = is_connected
        if is_connected:
            self.central = central
            return "connected"
        return "disconnected"


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
    if opcode == 0x04 and len(params) == 8:
        cid, mask = struct.unpack("<II", params)
        detail = f" id=0x{cid & 0x1FFFFFFF:03X} mask=0x{mask:08X}"
    return f"{name} tag={tag}{detail} params={params.hex() or '-'}"


# SPEC.md §9 — a client has at most ONE request outstanding: it writes, waits
# for the indication, and only then writes again.
#
# One slot beyond that still exists, and it is not slack. It carries the `busy`
# refusal itself: a client that pipelines anyway must be told so, and a device
# whose only slot is occupied by the response it already owes has nothing left
# to say that with. §9.4 forbids both alternatives — silence, and applying a
# request it cannot answer.
#
# So this queue holds TWO, which is what §9 requires and also all it requires.
# The link carries one indication at a time, so the second slot is where a
# response composed while an earlier one is unconfirmed waits its turn -- and
# §9 is explicit that a CONFORMING client's next request lands in exactly that
# window, having arrived after the previous response reached it but before the
# confirmation did. A `busy` refusal is a response and waits the same way.
# Past two there is no room, and `admit` says so by answering "full".
CONTROL_OUTSTANDING = 1
CONTROL_QUEUE_DEPTH = CONTROL_OUTSTANDING


class ControlQueue:
    """Decides whether a control request can be answered, before it is applied.

    SPEC.md §9.4 — a device MUST NOT apply a request it cannot answer. That
    makes admission a decision taken *ahead* of dispatch rather than a check on
    the way out, which is the whole point: applying first and answering second
    is the natural order to write the code in, and it is the order that leaves
    a client retrying a request that has already taken effect.

    Holds no Bluetooth state, so the rules above are testable without a radio.
    """

    def __init__(self, depth=CONTROL_QUEUE_DEPTH):
        self.depth = depth
        # Responses awaiting delivery. The tag rides along for the log only:
        # admission does not consult it, because with one request outstanding
        # it cannot tell anything the depth has not already told us.
        self._out = collections.deque()
        self.dropped = 0

    def __len__(self):
        return len(self._out)

    def admit(self, tag):
        """`apply`, `busy` or `full`.

        Only `apply` permits the request to take effect. `busy` is answered but
        not applied; `full` cannot even be answered.

        There used to be a `duplicate-tag` verdict here, refusing a request
        bearing a tag already outstanding. One-outstanding removed the state it
        was detecting rather than the check: a second request written before
        the first is answered is refused whatever tag it carries, and a request
        written after cannot collide with anything. So tag ambiguity is not
        prevented by this class any more, it is structurally impossible -- and
        a device needs no tag table at all.
        """
        if len(self._out) > self.depth:            # depth + 1 held: the
            return "full"                          # response and one refusal
        if len(self._out) == self.depth:
            return "busy"
        return "apply"

    def hold(self, tag, response):
        self._out.append((tag, response))

    def peek(self):
        return self._out[0][1] if self._out else None

    def delivered(self):
        """One response has been sent, so it is no longer owed.

        SPEC.md §9 — owing ends at the SEND: the response is handed to the
        transport and the device has nothing further to do for it. The caller
        is `update_value` returning True, which under CoreBluetooth means the
        stack accepted the value for transmission, and that is exactly the
        moment §9 names. Not the confirmation: a device whose obligation ran
        that far would refuse a client that wrote as soon as the response
        reached it, which §9 tells clients to do.

        CoreBluetooth never tells a peripheral app that a central confirmed an
        indication, so this reference could not implement the confirmation
        reading even if §9 asked for it. It does not, and that is not a
        coincidence -- §9 anchors on the one event every device can observe
        about its own sending.
        """
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


#: What bless 0.3.0 -- the pinned backend, and the newest that exists -- can
#: actually enforce, read from its source rather than its documentation.
#:
#:   bluezdbus  `transform_flags_with_permissions()` converts exactly two
#:              flags: READ -> ENCRYPT_READ and WRITE -> ENCRYPT_WRITE. A
#:              characteristic whose write property is write-without-response,
#:              or whose only property is notify or indicate, keeps its plain
#:              D-Bus flag however its permissions are set.
#:   winrt      `permissions_to_protection_level()` shifts the permission word
#:              right by 3 for reads and 4 for writes, while the bits it is
#:              looking for are 0x4 and 0x8. Both shifts land on zero, so every
#:              characteristic is PLAIN whatever the posture says.
#:   corebluetooth
#:              Permissions are handed to CBMutableCharacteristic unchanged and
#:              cover reads and both write forms. Notification delivery is not
#:              governed by CB permissions at all.
#:
#: SPEC.md §10 is about what a device REQUIRES, and a peripheral that reports a
#: posture it does not deliver is the plausible wrong value §1.1 exists to
#: prevent, aimed at whoever is deciding whether this link is safe.
_BACKEND_ENFORCES = {
    "corebluetooth": {"read", "write", "write-without-response"},
    "bluezdbus": {"read", "write"},
    "winrt": set(),
}


def backend_for(platform):
    """The bless backend `platform` selects. `None` if bless has none."""
    if platform.startswith("darwin"):
        return "corebluetooth"
    if platform.startswith("linux"):
        return "bluezdbus"
    if platform.startswith("win"):
        return "winrt"
    return None


def unenforced_characteristics(posture, backend):
    """Names `posture` says to protect that `backend` will not protect.

    Pure, so the selftest checks it without a radio -- which is the only way it
    ever gets checked, because the gap is invisible from this side of the link:
    the peripheral asks for encryption and is told nothing went wrong.
    """
    enforceable = _BACKEND_ENFORCES.get(backend)
    if enforceable is None:
        return set()
    properties = {c["name"]: set(c["properties"])
                  for c in dev.enc.SCHEMA["profile"]["characteristics"]}
    return {name for name in encrypted_characteristics(posture)
            if not (properties[name] & enforceable)}


def encrypted_characteristics(posture):
    """Names of the characteristics that require an encrypted link.

    Info is absent from every posture: §10.2 says to leave it readable so that
    a client which cannot pair can still identify what it found rather than
    reporting a device that is present, advertising a VTP service and
    apparently broken.
    """
    if posture == "all":
        return {"gps", "can", "imu", "control", "monitor_values", "aiding"}
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
        # The identity served by the PREVIOUS link, kept past the reset so the
        # log can say whether a rising edge was a new central or the same one
        # resubscribing.
        self._last_central = None
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
        self._aiding_discarded = 0
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
        # Measured, not assumed: the pump's target rate is what every stream is
        # sized against, so the rate it actually achieves is worth reporting.
        self._tick_hz = 0.0
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
        # A request the device has applied but not yet answered (OBD_INFO:
        # SPEC.md 15.2 sends the response only when the probe completes).
        # (tag, request) while one is open; the pump asks the device each
        # tick and queues the answer when it is due. It occupies the
        # one-outstanding slot exactly as a queued response does.
        self._pending_control = None
        self._notify = {"gps": CHAR["gps"], "can": CHAR["can"],
                        "imu": CHAR["imu"]}

    # -- GATT callbacks ---------------------------------------------------

    def read_request(self, characteristic, **kwargs):
        """Info is regenerated per read: SPEC.md §4 forbids a client caching it
        across connections precisely because it can change."""
        self._observe_link_up()
        name = CHAR_NAMES.get(characteristic.uuid.lower(), characteristic.uuid)
        log.info("READ  %s", name)
        if characteristic.uuid.lower() == CHAR["info"].lower():
            return self.device.info()
        return characteristic.value or b""

    def write_request(self, characteristic, value, **kwargs):
        # BEFORE anything is applied. This write is itself proof the link is
        # up, and taking the connection edge here is what stops the pump
        # noticing the connection a moment later and clearing away a request it
        # has already applied.
        self._observe_link_up()
        uuid = characteristic.uuid.lower()
        if uuid == CHAR["aiding"].lower():
            # SPEC.md §14.3 — a chunk, written without a response. There is no
            # error to return by construction: this is a Write Command, so ATT
            # carries nothing back, and every refusal a client acts on arrives
            # at GNSS_AID_COMMIT instead. A reason is logged and discarded.
            problem = self.device.handle_aiding_write(bytes(value))
            if problem:
                self._aiding_discarded += 1
                log.warning("discarded an aiding chunk: %s", problem)
            return

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
        # SPEC.md §9.7 — the arrival instant, read here because this callback
        # is the closest this process gets to the write landing. Everything
        # below happens after it.
        t_rx = self.device.now_us()
        request = bytes(value)
        if len(request) < 2:
            # Too short to carry a tag, so there is nothing to correlate a
            # reply with. SPEC.md §9 requires a response to every *request*;
            # two bytes are the minimum that constitutes one.
            log.warning("control write of %d byte(s) is not a request",
                        len(value))
            return
        opcode, tag = request[0], request[1]

        # SPEC.md §9.4 — everything that could stop this response reaching the
        # client is decided here, BEFORE the device sees the request.
        subscribed = self._subscribed()
        if subscribed is not None and "control" not in subscribed:
            # No indications enabled: the answer has nowhere to go, so the
            # request MUST NOT take effect. A client that writes before
            # subscribing is violating §9.4 and would otherwise leave the two
            # ends disagreeing about the subscription table.
            self._note_control(request, "discarded: no indication subscriber")
            log.warning("CTRL  %s -> DISCARDED unapplied: the client has not "
                        "enabled indications on Control (SPEC.md 9.4)",
                        _describe_request(request))
            return

        verdict = self._control.admit(tag)
        if verdict == "apply" and self._pending_control is not None:
            # A deferred response is a request outstanding (SPEC.md 9): the
            # queue cannot see it, so the busy answer is decided here.
            verdict = "busy"
        if verdict == "full":
            self._note_control(request, "discarded: queue full")
            log.warning("CTRL  %s -> DISCARDED unapplied: %d response(s) "
                        "already awaiting delivery and the client kept writing",
                        _describe_request(request), len(self._control))
            self._control.dropped += 1
            return
        if verdict == "busy":
            # SPEC.md §9 — a client has one request outstanding. This one wrote
            # again before its answer arrived, so it is told to wait rather
            # than having a request applied that cannot be answered (§9.4).
            response = bytes([opcode, tag, 5])          # busy
        else:
            response = self.device.handle_control(request, t_rx=t_rx)
            if response is None:
                return
            if response is dev.RESPONSE_PENDING:
                # SPEC.md 15.2 -- the request took effect (9.4's order), and
                # the response is owed once the probe completes. The pump
                # collects it from due_control_response(); until then this
                # request holds the one-outstanding slot.
                log.info("CTRL  %s -> pending: the probe is running "
                         "(SPEC.md 15.2)", _describe_request(request))
                self._note_control(request, "pending")
                self._pending_control = (tag, request)
                return

        status = STATUSES.get(response[2], f"0x{response[2]:02X}")
        log.info("CTRL  %s -> %s", _describe_request(request), status)
        self._note_control(request, status)
        # Queued rather than sent from here: this callback does not run on the
        # loop that owns the transport, and a refused response must be retried
        # rather than dropped. SPEC.md §9.4 -- `update_value` returning False
        # is CoreBluetooth's transmit queue being full, the same event as a
        # stack returning -ENOMEM from its indicate call, and it is a reason
        # to hold the response, never to answer the write with an ATT error.
        self._control.hold(tag, response)

    def _note_control(self, request, status):
        self.control_log.append(
            (time.strftime("%H:%M:%S"),
             _describe_request(request).split(" params=")[0], status))

    def _central_identity(self):
        """Which central bless believes it is serving, or None.

        The keys of `_central_subscriptions` are central UUID strings. Note
        that CoreBluetooth keeps a CBCentral's identifier STABLE across
        connections to the same peer, so an unchanged identity does not mean
        the link never dropped -- it is a hint for the log, never a reason to
        skip a reset. See ConnectionTracker.
        """
        try:
            subs = self.server.peripheral_manager_delegate._central_subscriptions
        except AttributeError:
            return None
        return sorted(subs)[0] if subs else None

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

    # -- link edges -------------------------------------------------------
    #
    # SPEC.md 8.2 and 9.2 hang off these two, and the pump used to be the only
    # thing that could see them: it polled `is_connected()` once a tick and ran
    # the edge from what it found.
    #
    # A GATT callback is not polled. It arrives when the central's write
    # arrives, which on every real stack can be BEFORE the next poll -- so a
    # perfectly ordinary first request (the client connects, enables
    # indications, writes CAN_SUBSCRIBE) was admitted, applied and queued, and
    # then the pump noticed the connection it had already been serving and
    # cleared the queue and the device state out from under it. The client's
    # first request had taken effect and was never answered.
    #
    # The edge is therefore taken from whichever comes first: a GATT callback,
    # which is proof the link exists, or the poll. `ConnectionTracker` makes
    # that idempotent -- whichever loses the race sees no edge at all.

    def _observe_link_up(self):
        """A GATT callback is itself evidence that a central is being served."""
        if self._link.update(True, self._central_identity()) == "connected":
            self._on_connected()

    def _on_connected(self):
        # A connection starts from a known state. Without this the device
        # carries the previous client's subscriptions and sequence numbers into
        # the next connection, which 9.2 forbids.
        same = (self._last_central is not None
                and self._last_central == self._link.central)
        self._last_central = self._link.central
        self.device.on_connect()
        # The device zeroes its accepted-update count in on_connect(), so this
        # one has to be zeroed alongside it. The two are printed side by side
        # in the status line, and a lifetime counter next to a per-connection
        # one reads as a comparison when it is not: the previous client's
        # malformed writes would be reported against the client that has just
        # arrived. Not in _reset_transport_state() -- that runs on both edges,
        # and clearing on disconnect would drop the count before the final
        # status line could report it.
        self._monitor_rejected = 0
        self._aiding_discarded = 0
        self._reset_transport_state()
        log.info("CLIENT CONNECTED — sequence numbers restarted, "
                 "subscription table cleared")
        if same:
            # Worth saying out loud, because it is the one case where this
            # peripheral resets state on a link that may never have dropped.
            log.info("  (the same central identity as before: on this backend "
                     "a reconnection and a resubscribe are indistinguishable, "
                     "and SPEC.md 9.2 is the safe way to be wrong — see "
                     "ConnectionTracker)")

    def _on_disconnected(self):
        # 9.2 clears the table when the LINK DROPS, not when the next one
        # starts. Clearing only on connect left a disconnected device reporting
        # three installed ids with nobody subscribed, which reads as a client
        # fault.
        self.device.on_disconnect()
        undelivered = self._reset_transport_state()
        if undelivered:
            log.warning("%d control response(s) undelivered when the link "
                        "dropped", undelivered)
        log.info("CLIENT DISCONNECTED — subscription table cleared")
        log.info("  (strictly: no central is subscribed to anything. This "
                 "backend cannot tell that from a dropped link, so a client "
                 "that unsubscribes from everything and stays connected "
                 "reaches here too)")

    def _reset_transport_state(self):
        """Everything this transport holds on behalf of ONE link.

        Both edges call it. A connect that cleared only the device's state left
        payloads queued for the previous central sitting in `_pending`, and
        `_observed_mtu` describing a link that had gone; a disconnect that
        cleared only the control queue left the same payloads to be delivered
        to whoever connected next. Returning the count lets the disconnect path
        report what was owed and never arrived.
        """
        undelivered = self._control.discard_all()
        if self._pending_control is not None:
            # The probe's answer belongs to the link that asked for it; the
            # device side died in on_connect/on_disconnect (_obd_clear).
            self._pending_control = None
            undelivered += 1
        self._pending.clear()
        self._observed_mtu = None
        self._ready = True
        self._blocked_since = None
        return undelivered

    def _deliver(self, characteristic, payload, sent, refused):
        """One attempt. Returns True when the stack took it.

        SPEC.md §8.2 — the sequence number is written here and committed only
        if the stack accepts the notification, because seq counts notifications
        *sent*. A refusal therefore consumes nothing and the same number goes
        out on the next attempt, which is what makes the count a count of
        deliveries rather than of encodings.
        """
        uuid = self._notify[characteristic]
        stamped = self.device.stamp_seq(characteristic, payload)
        self.server.get_characteristic(uuid).value = stamped
        if self.server.update_value(SERVICE, uuid):
            self.device.commit_seq(characteristic)
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
        write_no_response = props.write_without_response
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

        # Said out loud, because nothing else will say it. The permission is
        # accepted, the server starts, and the characteristic is writable
        # without encryption -- there is no error anywhere in that sequence,
        # and from this side of the link a protected attribute and an
        # unprotected one look identical.
        backend = backend_for(sys.platform)
        unenforced = unenforced_characteristics(self.encrypt, backend)
        if unenforced:
            log.warning("NOT ENCRYPTED on this backend (%s): %s. bless 0.3.0 "
                        "translates the encryption permission only for "
                        "characteristics carrying %s, and 0.3.0 is the newest "
                        "there is. Treat --encrypt %s as a statement of intent "
                        "here, not as a control; the posture holds on "
                        "CoreBluetooth for everything but the notify streams.",
                        backend, ", ".join(sorted(unenforced)),
                        ", ".join(sorted(_BACKEND_ENFORCES[backend])) or
                        "nothing at all", self.encrypt)

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
        # SPEC.md §14 — bulk client-to-device, written without a response.
        await add("aiding", write_no_response, None,
                  readable | writeable | guard("aiding"))

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
                        "will size batches from --mtu", exc_info=True)
        log.info("advertising %s as %r", SERVICE, self.name)
        log.info("a client matching on the service UUID needs that UUID in the "
                 "advertisement; name is %d of %d permitted characters",
                 len(self.name), MAX_NAME_CHARS)
        log.info("Service Data (SPEC.md 3.3) is not advertised: the host "
                 "peripheral API does not expose it on every platform")

    async def run(self, poll_hz=POLL_HZ, screen_hz=10, max_ticks=None):
        """The pump. `max_ticks` bounds it so the loop can be driven by a test.

        Nothing else about the loop changes under test: transport_selftest.py
        runs THIS function against a fake GATT link rather than a
        reimplementation of it, because a second copy of a state machine is a
        second state machine and the bugs it was written to catch all lived in
        the ordering of this one.
        """
        interval = 1.0 / poll_hz
        ticks = 0
        # The tick is scheduled against an absolute deadline, not by sleeping
        # `interval` at the end of the loop. Sleeping a fixed interval AFTER
        # variable work makes the period `work + interval + whatever the event
        # loop adds`, which can only ever exceed the target: measured against a
        # client, a nominal 200 Hz ran at 172, and every stream came out ~14%
        # under its configured rate -- GPS at 9.7 Hz, the CAN bus at 262
        # frames/s against 306. Advancing a deadline by exactly `interval` and
        # sleeping until it absorbs both the work and the overshoot, because a
        # sleep that runs long shortens the next wait instead of displacing it.
        next_tick = time.monotonic()
        tick_epoch, tick_epoch_count = next_tick, 0
        self._tick_hz = 0.0
        every = max(1, poll_hz // screen_hz)
        # Counted per characteristic. A single total hides the one question a
        # reader of this log actually has, which is which stream is silent.
        sent, refused, unwanted = self.sent, self.refused, self.unwanted
        next_report = 0.0
        next_rate = time.monotonic() + 1.0
        last_counts = dict(sent)
        while True:
            # SPEC.md §8.2, §9.2 — the connection edge is handled BEFORE
            # anything is sent this tick. It used to run after the control
            # drain and the telemetry pump, so a notification built for the
            # previous central could be handed to a new one in the same tick
            # that reset the device: data from a link that no longer exists,
            # delivered under sequence numbers about to restart.
            # A GATT callback may already have taken the rising edge; the
            # tracker makes that idempotent, so this handles whichever the pump
            # is first to see, and nothing twice.
            event = self._link.update(await self.server.is_connected(),
                                      self._central_identity())
            if event == "connected":
                self._on_connected()
            elif event == "disconnected":
                self._on_disconnected()

            subscribed = self._subscribed()

            # A deferred response first: OBD_INFO is answered only when the
            # probe completes (SPEC.md 15.2), and the device says when that
            # is. Queued like any other response, so delivery below retries
            # it until it lands.
            if self._pending_control is not None:
                due = self.device.due_control_response()
                if due is not None:
                    tag, request = self._pending_control
                    self._pending_control = None
                    status = STATUSES.get(due[2], f"0x{due[2]:02X}")
                    log.info("CTRL  %s -> %s (probe complete)",
                             _describe_request(request), status)
                    self._note_control(request, status)
                    self._control.hold(tag, due)

            # Try again without waiting for a callback that may not come. This
            # used to be a 250 ms last resort against a wedged device; it is
            # now the ordinary path, because `update_value` answers the same
            # question the callback does and answers it on demand. See
            # RETRY_BLOCKED_S for why the difference between 250 ms and 10 ms
            # is the difference between shedding and not.
            #
            # BEFORE the control loop, not after it. Placed after, the retry
            # set `_ready` and the stream block below sent on it in the same
            # pass, while a control response the loop had been refused on was
            # still held -- a GPS notification ahead of the response SPEC.md
            # §9.4 puts ahead of everything.
            if (not self._ready and self._blocked_since
                    and time.monotonic() - self._blocked_since > RETRY_BLOCKED_S):
                self._ready = True
                self._blocked_since = None
                self._timeouts += 1

            # Control responses first, and retried until they land. They are
            # the one thing on this link that is owed rather than offered, and
            # SPEC.md §9.4 puts a held response ahead of every notification
            # this device queues after it: the first buffer the streams give
            # back is the response's, so the hold ends when the queue next
            # drains and not when the streams happen to leave room.
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
            for characteristic, payload in self.device.poll(
                    undelivered=tuple(self._pending)):
                if subscribed is not None and characteristic not in subscribed:
                    unwanted[characteristic] += 1
                    continue
                stale = self._pending.get(characteristic)
                if stale is not None:
                    self.device.record_refused(characteristic, stale)
                    refused[characteristic] += 1
                self._pending[characteristic] = payload

            # Rotate which stream is offered first. The queue is finite, and a
            # fixed order means the LAST stream absorbs every refusal: with
            # GPS, IMU and CAN all subscribed, CAN was refused almost in full
            # while the other two flowed, purely because it was sent last.
            #
            # And nothing while a response is held. The control loop above
            # leaves `_ready` false when it was refused, but the ready
            # callback lands on CoreBluetooth's thread and can set it true
            # between that loop and this line; the queue's length is the
            # fact SPEC.md §9.4 is about, so it is the gate.
            if self._ready and self._pending and not len(self._control):
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
                        # SPEC.md §8.3 — a refusal is the transmit queue being
                        # full for a moment, not the device being overrun, so
                        # the payload STAYS pending and is retried. It used to
                        # be discarded here and its records counted as loss,
                        # which threw away a whole batch every time the stack
                        # said "not now" -- and `_deliver` had already been
                        # written for the other answer: it stamps seq and
                        # commits only on acceptance, precisely so the same
                        # number can go out on the next attempt (§8.2). There
                        # was no next attempt.
                        #
                        # Nothing accumulates: at most one payload per stream
                        # is held, and a newer one supersedes it above and IS
                        # counted. So a stall costs latency, and only a stall
                        # long enough to be overtaken costs data.
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
                # How the transmit queue is behaving. NOT gated on the panel:
                # these two describe the link, and hiding them behind the
                # display meant they vanished from exactly the run somebody
                # starts with --no-display to find out why throughput is bad.
                log.info("  pump: %.1f ticks/s (target %d); every stream is "
                         "sized against this, so a shortfall here is a "
                         "shortfall in all of them",
                         self._tick_hz, POLL_HZ)
                log.info("  delivery: ready-callbacks %d, unprompted retries %d",
                         self._ready_callbacks, self._timeouts)
                if self._paints:
                    log.info("  display: paint %.1f ms, pump %.1f ms, %d paints",
                             self._paint_ms, self._pump_ms, self._paints)
                # Both counters were being maintained and never printed, so a
                # client whose every chunk was discarded looked identical to
                # one that sent none -- the same pair of opposite faults the
                # monitor line below exists to separate.
                if self.device.aid_transfers_applied or self._aiding_discarded:
                    log.info("  aiding: transfers applied=%d | chunks "
                             "discarded=%d",
                             self.device.aid_transfers_applied,
                             self._aiding_discarded)
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
            if max_ticks is not None and ticks >= max_ticks:
                return

            tick_epoch_count += 1
            next_tick += interval
            now = time.monotonic()
            delay = next_tick - now
            if delay <= 0:
                # Behind the schedule rather than merely late for one tick.
                # Running the missed ticks back-to-back to catch up would send
                # a burst the link never asked for and starve everything else
                # on this loop, so the ticks are abandoned and the deadline is
                # taken from now. Falling behind is visible as a tick rate
                # below poll_hz, which is the honest way to report it.
                next_tick = now
                delay = 0
            if now - tick_epoch >= 1.0:
                self._tick_hz = tick_epoch_count / (now - tick_epoch)
                tick_epoch, tick_epoch_count = now, 0
            await asyncio.sleep(delay)

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
                           imu_hz=args.imu_hz, can_scale=args.can_scale,
                           can_rates=dict(args.can_rate or ()))

    bus = device.can_bus_rates(poll_hz=POLL_HZ)
    unknown = sorted(set(device.can_rates) - {cid for cid, _n, _e in bus})
    if unknown:
        log.error("--can-rate names %s, which this bus does not carry; its "
                  "ids are %s",
                  ", ".join(f"0x{cid:03X}" for cid in unknown),
                  ", ".join(f"0x{cid:03X}" for cid, _n, _e in bus))
        return
    if args.can_scale != 1.0 or device.can_rates:
        log.info("CAN bus rates: %s", "   ".join(
            f"0x{cid:03X} {eff:.4g} Hz (natural {nat:g})"
            for cid, nat, eff in bus))
        log.info("  %.4g frames/s across %d ids. These are what the pump can "
                 "deliver, not what was asked for: it polls at %d Hz and a "
                 "frame waits for the first poll at or after its interval, so "
                 "a rate that is not a whole number of polls rounds down to "
                 "one that is",
                 sum(eff for _c, _n, eff in bus), len(bus), POLL_HZ)

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


def _positive_float(text):
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("must be above 0")
    return value


def _can_rate(text):
    """`ID=HZ`, the id in any base Python reads, so 0x0C0 and 192 both work."""
    cid, sep, hz = text.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"expected ID=HZ, got {text!r}")
    try:
        cid = int(cid, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a CAN id: {cid!r}") from None
    return cid, _positive_float(hz)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", default="VTP",
                    help=f"advertised local name, at most {MAX_NAME_CHARS} "
                         f"characters beside the service UUID")
    ap.add_argument("--mtu", type=int, default=247,
                    help="assumed ATT MTU for batch sizing")
    ap.add_argument("--gps-hz", type=int, default=10)
    ap.add_argument("--imu-hz", type=int, default=100)
    ap.add_argument("--can-scale", type=_positive_float, default=1.0,
                    metavar="FACTOR",
                    help="multiply every CAN channel's natural bus rate. The "
                         "synthetic bus runs 0x0C0 at 50 Hz, 0x1A0 at 20 and "
                         "0x2E0 at 10, so 80 frames/s in total, and "
                         "--can-scale 4 asks for 320. What arrives is a little "
                         f"less: the pump polls at {POLL_HZ} Hz and a frame "
                         "waits for the first poll at or after its interval, "
                         "so rates that are not a whole number of polls round "
                         "down to ones that are, and none can exceed the poll "
                         "rate itself. The startup log reports what the bus "
                         "will actually carry")
    ap.add_argument("--can-rate", type=_can_rate, action="append",
                    metavar="ID=HZ",
                    help="give one CAN id an explicit rate, overriding its "
                         "natural rate and --can-scale both, e.g. "
                         "--can-rate 0x0C0=200. Repeatable")
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
