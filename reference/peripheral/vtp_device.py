#!/usr/bin/env python3
"""A synthetic VTP/1 device: everything a peripheral does except the radio.

Deliberately free of any Bluetooth dependency. The BLE transport lives in
serve.py, and this holds the parts worth testing: one monotonic clock, the
three roles timestamped against it, batching that respects the negotiated MTU,
and the control plane. That split is what lets CI check the device against the
reference decoder on a machine with no Bluetooth adapter at all — see
selftest.py, which decodes every notification this produces.

The vehicle is a car lapping a circular circuit at constant speed. A circle is
not much of a track, but it exercises what matters: position advances, heading
rotates, lateral acceleration is non-zero and constant, yaw rate is non-zero,
and the CAN signals derive from the same motion as the GPS fix and the IMU
sample. A client that aligns the three channels sees them agree.
"""
import itertools
import math
import pathlib
import struct
import sys
import time
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "reference" / "python"))

import vtp1_encode as enc  # noqa: E402

# ---------------------------------------------------------------------------
# Capability and layout constants
# ---------------------------------------------------------------------------

# Read from the schema rather than restated, for the reason _OPCODE below
# gives: a hand-transcribed bit survives a schema change holding its old
# value, and a peripheral declaring a different role than the schema defines
# is diagnosed on the wire, weeks later, by whoever holds the client. A
# renamed or moved bit arrives here as a KeyError at import instead.
_CAP = {b["name"]: 1 << b["bit"]
        for b in enc.SCHEMA["bitmasks"]["capabilities"]["bits"]}
CAP_GPS, CAP_CAN, CAP_IMU = _CAP["gps"], _CAP["can"], _CAP["imu"]
CAP_MONITOR = _CAP["monitor"]
CAP_CONTROL, CAP_CAN_FD = _CAP["control"], _CAP["can_fd"]
CAP_MASKED_SUBS = _CAP["masked_subscriptions"]
# Bit 7 was on_change_subscriptions in a pre-1.0 draft and stays unassigned.
CAP_POWER = _CAP["power"]
CAP_GNSS_AIDING = _CAP["gnss_aiding"]
CAP_OBD = _CAP["obd"]

V_T_UTC, V_T_UTC_RESOLVED, V_POSITION = 1 << 0, 1 << 1, 1 << 2
V_ALT_MSL, V_ALT_ELLIPSOID, V_VELOCITY = 1 << 3, 1 << 4, 1 << 5
V_HEAD_MOT, V_H_ACC, V_V_ACC = 1 << 6, 1 << 7, 1 << 8
V_S_ACC, V_P_DOP, V_NUM_SV = 1 << 9, 1 << 10, 1 << 11

IMU_ACCEL, IMU_GYRO = 0x01, 0x02
FIX_3D = 3
# SPEC.md §5.6 — fix_flags bit 4.
FIX_FLAG_SOLUTION_EPOCH = 1 << 4

# Control opcodes (SPEC.md §9), read from the schema rather than restated, so
# the one place they are defined stays the only place -- as MIN_ATT_MTU and
# OPCODE_CAPABILITY below already are.
#
# Each lookup also asserts its name still exists in the schema, so a rename
# arrives here as a KeyError at import. That is the failure a peripheral wants:
# a restated constant survives a rename holding its old value, and a device
# that answers an opcode the specification no longer defines is diagnosed from
# the wire, weeks later, by whoever is holding the client.
#
# Named one per line rather than bound in a loop. These are the names the
# dispatch in handle_control reads, and a name that cannot be grepped for is
# worse than the number it replaced.
_OPCODE = {op["name"]: op["value"] for op in enc.SCHEMA["control"]["opcodes"]}

CAN_RESET = _OPCODE["CAN_RESET"]
CAN_SUBSCRIBE = _OPCODE["CAN_SUBSCRIBE"]
CAN_SUBSCRIBE_MASK = _OPCODE["CAN_SUBSCRIBE_MASK"]
CAN_UNSUBSCRIBE = _OPCODE["CAN_UNSUBSCRIBE"]
GPS_SET_RATE = _OPCODE["GPS_SET_RATE"]
IMU_SET_RATE = _OPCODE["IMU_SET_RATE"]
TIME_SYNC = _OPCODE["TIME_SYNC"]
MONITOR_LIST = _OPCODE["MONITOR_LIST"]
GET_POWER = _OPCODE["GET_POWER"]
GNSS_AID_INFO = _OPCODE["GNSS_AID_INFO"]
GNSS_AID_BEGIN = _OPCODE["GNSS_AID_BEGIN"]
# 0x14 was GNSS_AID_ABORT; unassigned.
GNSS_AID_COMMIT = _OPCODE["GNSS_AID_COMMIT"]
OBD_INFO = _OPCODE["OBD_INFO"]
OBD_POLL_SET = _OPCODE["OBD_POLL_SET"]

#: handle_control's answer when the response is not ready to send yet.
#: SPEC.md 15.2 -- the OBD_INFO response reports a COMPLETED probe, so it is
#: sent only once the probe's last request has had its collection window; the
#: transport keeps the request outstanding (busy to anything written meanwhile)
#: and calls `due_control_response()` until the device hands the payload over.
#: An object, not None: None already means "not addressable, answer nothing".
RESPONSE_PENDING = object()

# SPEC.md 9.7 -- power_source members and power_validity bits.
SRC_EXTERNAL, SRC_DISCHARGING, SRC_CHARGING, SRC_CHARGED = 1, 2, 3, 4
PWR_SOURCE, PWR_PERCENT = 1 << 0, 1 << 1

# SPEC.md §14.1 — the one aiding format this device accepts. It forwards the
# bytes to its receiver without interpreting them, so this names what the
# RECEIVER speaks; the value is here so a client knows what to fetch.
AID_FORMAT_UBX_MGA = 1

AID_RESULT_APPLIED, AID_RESULT_INCOMPLETE = 1, 2
AID_RESULT_BAD_CRC, AID_RESULT_REJECTED = 3, 4

AID_V_HELD_UNTIL = 0x01          # aid_validity bit 0
COMMIT_V_FIRST_MISSING = 0x01    # commit_validity bit 0

# SPEC.md §14.2 — a device ceiling, and the reason GNSS_AID_BEGIN can refuse
# before a single chunk is written. Sized for an AssistNow Offline product.
AID_MAX_BYTES = 131_072

# SPEC.md §14.3 — three bytes of ATT Write Command header and three of chunk
# header come off the negotiated MTU before any payload fits.
AID_CHUNK_OVERHEAD = 6

# SPEC.md §14.3 — `index` and `first_missing` are u16, so this is the most a
# transfer may need. Derived from the field rather than written as a number
# somebody has to keep in step with it.
AID_MAX_CHUNKS = 0xFFFF

# SPEC.md §13.2 — channels a Monitor device may ask the client for.
CH_LAP_TIME, CH_LAST_LAP_TIME, CH_BEST_LAP_TIME = 1, 2, 3
CH_DELTA_BEST, CH_PREDICTED_LAP_TIME, CH_LAP_NUMBER = 4, 5, 6
CH_SPEED, CH_SESSION_DISTANCE, CH_SESSION_TIME = 7, 8, 9

# SPEC.md §7.2 — imu_header.flags bit 2.
IMU_SATURATED = 0x04

MONITOR_PRESENT = 0x01

# SPEC.md §13.5 — how long each channel may be displayed without a refresh, in
# 100 ms units. Never zero: every declared channel carries a deadline, so a
# value the client stops sending always stops being shown.
#
# Per channel because the channels differ in kind, and the spread here is the
# argument for that. A lap time ticking up is wrong within a second of going
# stale; a best lap stays true until it is beaten, so it gets the longest
# deadline this field can express rather than none at all. Several times the
# expected update interval throughout, because this bounds how wrong a display
# may be rather than how often a client must talk.
#
# The three that read 0 used to mean "no deadline of its own", and SPEC.md then
# derived a device-wide liveness bound to expire them anyway. One deadline per
# channel replaced both rules; 255 is 25.5 s, the ceiling of a u8 in 100 ms
# units, and is still a bound.
MONITOR_MAX_AGE = {
    CH_LAP_TIME: 20,             # 2 s — ticks continuously
    CH_PREDICTED_LAP_TIME: 20,
    CH_SPEED: 10,                # 1 s — changes fastest
    CH_SESSION_TIME: 20,
    CH_SESSION_DISTANCE: 30,
    CH_LAST_LAP_TIME: 255,       # 25.5 s — true until the next lap ends
    CH_BEST_LAP_TIME: 255,       # true until it is beaten
    CH_DELTA_BEST: 20,
    CH_LAP_NUMBER: 255,          # true until the next lap starts
}

PROTOCOL_MAJOR, PROTOCOL_MINOR = 1, 0
# SPEC.md §2 — read from the schema rather than restated, so the one place it
# is defined stays the only place it is defined.
MIN_ATT_MTU = enc.SCHEMA["protocol"]["min_att_mtu"]

# SPEC.md 9 -- which capability bit owns each opcode, read from the schema
# rather than restated. A device without the bit answers unsupported_opcode,
# and answers it BEFORE parsing parameters.
_CAP_BIT = {b["name"]: 1 << b["bit"]
            for b in enc.SCHEMA["bitmasks"]["capabilities"]["bits"]}
OPCODE_CAPABILITY = {
    op["value"]: (_CAP_BIT[op["capability"]] if op["capability"] else 0)
    for op in enc.SCHEMA["control"]["opcodes"]
}

ST_OK, ST_UNSUPPORTED, ST_BAD_PARAMS = 0, 1, 2
ST_TABLE_FULL, ST_RATE_EXCEEDED = 3, 4
ST_UNKNOWN_SUBSCRIPTION = 7

# SPEC.md §9.1 — matching runs over bits 0-29: the arbitration identifier and
# the standard/extended format bit. Bits 30 and 31 say how a frame was
# transmitted, not which frame it is, and take no part. CAN_SUBSCRIBE is
# CAN_SUBSCRIBE_MASK with every one of those bits set.
CAN_MATCH_BITS = 0x3FFFFFFF
MASK_EXACT = 0x3FFFFFFF

SUB_EVERY_FRAME, SUB_PERIODIC = 0, 1

CAN_SUBSCRIPTION_SLOTS = 32
CAN_MAX_FRAMES_PER_S = 4000

# SPEC.md 15.4 -- the one OBD capacity this build declares in Info. There is
# no declared rate: polling is response-paced, so what bounds the request rate
# is the car, and a device publishing a "safe" interval would be guessing about
# a vehicle it has never met.
OBD_POLL_SLOTS = 16
# SPEC.md 15.1 -- an unanswered request is abandoned after this long, so a PID
# nothing answers cannot stall the schedule. Deliberately generous rather than
# ISO 15765-4's P2max of 50 ms: a logger that gives up on a slow gatewayed ECU
# has lost the reading a tester would have waited for.
OBD_RESPONSE_TIMEOUT_US = 100_000
# SPEC.md 15.4.1 -- bit 7 of a PID byte groups it with the byte that follows.
# PIDs are 0x01..0x60, so bit 7 is space that reads zero on a device without
# capability bit 11 and is refused there by rule 5 as a value out of range.
OBD_PID_MORE = 0x80
# The ONLY bound on grouping the device checks. Seven PIDs would not fit the
# request frame: `[1+g, 0x01, p1..pg]` is 2+g bytes and a classic CAN frame
# holds eight. Whether the RESPONSE fits is arithmetic over J1979 lengths,
# whose tables live in the client (SPEC.md 15.5) -- the device does not check
# it and must not, because a table of PID sizes in firmware is exactly what
# SPEC.md 15.9 excludes. An oversize group is answered with a first frame and
# dies for want of a flow control this device will not send.
OBD_MAX_GROUP = 6
# SPEC.md 15.6 -- can_header.flags `polling`: the poll set is non-empty.
# Schema-derived like the capability bits above.
CAN_FLAG_POLLING = next(1 << b["bit"]
                        for b in enc.SCHEMA["bitmasks"]["can_flags"]["bits"]
                        if b["name"] == "polling")
# SPEC.md 15.1 -- everything a Mode 01 request may name.
OBD_PID_FLOOR, OBD_PID_CEILING = 0x01, 0x60


def _pid_mask(base, pids):
    """SPEC.md 15.3 -- bit n = PID base+n, LSB first (NOT J1979's order)."""
    mask = 0
    for pid in pids:
        assert base <= pid < base + 32
        mask |= 1 << (pid - base)
    return mask


def _j1979_mask_bytes(vtp_mask):
    """The four data bytes of a supported-PID response, J1979's own order:
    bit 7 of the first byte is the lowest PID of the window. This is the
    transcription SPEC.md 15.3 warns about, exercised deliberately -- the
    probe DETAIL carries the LSB-first field, the BUS FRAME carries this."""
    j = 0
    for i in range(32):
        if vtp_mask & (1 << i):
            j |= 1 << (31 - i)
    return j.to_bytes(4, "big")

METRES_PER_DEG_LAT = 111_320.0


# ---------------------------------------------------------------------------
# Motion
# ---------------------------------------------------------------------------

class Circuit:
    """A lap with a speed that actually changes.

    A constant-speed circle was the first version of this and it was useless
    for testing anything but position: every CAN value it produced — engine
    speed, road speed, throttle, lateral g — was a constant, so a client that
    decoded a channel correctly and a client that decoded a fixed byte offset
    wrongly looked identical on screen.

    Speed is therefore a function of time, `v(t) = v_mid + v_amp·sin(2πt/T)`,
    and distance is its exact integral. Deriving position from the integral
    rather than assuming a constant angular rate keeps the channels honest: the
    speed on the CAN bus is the derivative of the GPS track, and the
    longitudinal acceleration on the IMU is the derivative of that. A client
    that cross-checks them finds them consistent, which is the property this
    protocol exists for and therefore the one a test device must not fake.
    """

    def __init__(self, lat_deg=51.5074, lon_deg=-1.3970, radius_m=180.0,
                 speed_mid_mps=30.0, speed_amp_mps=12.0, period_s=20.0):
        self.lat0, self.lon0 = lat_deg, lon_deg
        self.radius = radius_m
        self.v_mid, self.v_amp, self.period = speed_mid_mps, speed_amp_mps, period_s
        self._m_per_deg_lon = METRES_PER_DEG_LAT * math.cos(math.radians(lat_deg))

    # Four gears, by the road speed at which each is at its limit. RPM
    # therefore sawtooths on every shift, which is far easier to recognise on a
    # dashboard than a smooth curve — a wrongly decoded channel does not
    # sawtooth.
    GEAR_TOPS = (15.0, 25.0, 35.0, 50.0)
    IDLE_RPM, REDLINE_RPM = 1200, 7000

    def at(self, t_s):
        w = 2.0 * math.pi / self.period
        speed = self.v_mid + self.v_amp * math.sin(w * t_s)
        # Exact derivative of `speed`, so the IMU agrees with the CAN bus.
        accel = self.v_amp * w * math.cos(w * t_s)
        # Exact integral of `speed`, so the GPS track agrees with both.
        distance = (self.v_mid * t_s
                    + (self.v_amp / w) * (1.0 - math.cos(w * t_s)))

        theta = distance / self.radius
        north = self.radius * math.cos(theta)
        east = self.radius * math.sin(theta)
        heading = (math.degrees(theta) + 90.0) % 360.0
        hrad = math.radians(heading)

        gear = next((i + 1 for i, top in enumerate(self.GEAR_TOPS)
                     if speed <= top), len(self.GEAR_TOPS))
        top = self.GEAR_TOPS[gear - 1]
        floor_ = 0.0 if gear == 1 else self.GEAR_TOPS[gear - 2]
        through = (speed - floor_) / (top - floor_) if top > floor_ else 0.0
        rpm = self.IDLE_RPM + through * (self.REDLINE_RPM - self.IDLE_RPM)

        a_max = self.v_amp * w
        return {
            "lat": self.lat0 + north / METRES_PER_DEG_LAT,
            "lon": self.lon0 + east / self._m_per_deg_lon,
            "vel_n": speed * math.cos(hrad),
            "vel_e": speed * math.sin(hrad),
            "heading": heading,
            "speed": speed,
            "accel": accel,
            "distance": distance,
            "gear": gear,
            "rpm": rpm,
            # Centripetal acceleration, which now varies with v squared.
            "lat_g": (speed ** 2 / self.radius) / 9.80665,
            "long_g": accel / 9.80665,
            "yaw_rate": math.degrees(speed / self.radius),
            "throttle": max(0.0, min(100.0, accel / a_max * 100.0)),
            "brake": max(0.0, min(100.0, -accel / a_max * 100.0)),
        }


# ---------------------------------------------------------------------------
# The device
# ---------------------------------------------------------------------------

class VtpDevice:
    """A conforming VTP/1 peripheral over a synthetic vehicle.

    `now_us` is injectable so a test can drive the device deterministically
    rather than sleeping; the default is a real monotonic clock.
    """

    # SPEC.md 4.1 -- what this build declares. Configurable rather than a
    # constant because the bits change how the control plane ANSWERS, and a
    # device that hard-codes them can only ever demonstrate one half of each
    # rule. selftest.py builds a device without them to check the other.
    DEFAULT_CAPABILITIES = (CAP_GPS | CAP_CAN | CAP_IMU | CAP_CONTROL
                            | CAP_MONITOR | CAP_MASKED_SUBS
                            | CAP_POWER | CAP_GNSS_AIDING | CAP_OBD)

    #: SPEC.md §14 — what this device declares in gnss_aid_caps. Named on the
    #: class so the conformance harness can seed a fault against the device's
    #: own numbers rather than a copy of them.
    AID_FORMAT = AID_FORMAT_UBX_MGA
    AID_MAX_BYTES_DECLARED = AID_MAX_BYTES

    #: SPEC.md §15.2 -- the synthetic car's diagnostic side. Response id ->
    #: the three Mode 01 supported-PID masks that ECU declares (VTP LSB-first
    #: order). An empty dict models a gatewayed port: the probe transmits and
    #: nothing answers. Named on the class so the harness can seed faults
    #: against the device's own car rather than a copy of it.
    OBD_REQUEST_ID = 0x7DF
    OBD_ECUS = {
        0x7E8: (_pid_mask(0x01, [0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x0B,
                                 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x13,
                                 0x15, 0x1C, 0x1F, 0x20]),
                _pid_mask(0x21, [0x21, 0x2E, 0x2F, 0x30, 0x31, 0x33, 0x3C,
                                 0x40]),
                _pid_mask(0x41, [0x42, 0x43, 0x44, 0x45, 0x46, 0x47, 0x49,
                                 0x4C, 0x51, 0x56])),
        0x7E9: (_pid_mask(0x01, [0x01, 0x0C, 0x0D, 0x11, 0x20]),
                _pid_mask(0x21, [0x2F, 0x40]),
                _pid_mask(0x41, [0x42, 0x51])),
    }

    #: How long this synthetic car takes to answer, in microseconds. Zero --
    #: answering in the same tick the request went out -- is what the model did
    #: before SPEC.md 15.4 was response-paced, and it makes pacing invisible:
    #: with an instant car the next request is due immediately and `interval_ms`
    #: is the only thing left pacing the loop. Set it to model a real bus.
    OBD_ECU_LATENCY_US = 0

    def __init__(self, *, now_us=None, mtu=247, gps_hz=10, imu_hz=100,
                 circuit=None, monitor_channels=None, capabilities=None,
                 obd_latency_us=None):
        self._clock = now_us or self._monotonic_us
        self.obd_latency_us = (self.OBD_ECU_LATENCY_US if obd_latency_us is None
                               else obd_latency_us)
        self._origin_ns = time.monotonic_ns()
        self._wall_origin_ms = int(time.time() * 1000)

        self.capabilities = (self.DEFAULT_CAPABILITIES if capabilities is None
                             else capabilities)
        self.mtu = mtu
        # The largest MTU this build will ever accept. `self.mtu` moves with
        # the link; this does not, and batches are never sized above it.
        self._device_mtu_ceiling = mtu
        # False until a backend tells us what the link actually negotiated.
        # `--mtu` is what this build was CONFIGURED for, not what it got.
        self._mtu_observed = False
        self.gps_hz = gps_hz
        self.imu_hz = imu_hz
        self.circuit = circuit or Circuit()

        # SPEC.md §8.2 — seq counts notifications on its own characteristic and
        # restarts at 0 per connection, so a client never has to tell a
        # reconnection from a wrap.
        self._seq = {"gps": 0, "can": 0, "imu": 0}
        # SPEC.md §8.3 — items accepted and then discarded. Saturating, so a
        # catastrophic loss can never read as perfect health.
        self._dropped = {"gps": 0, "can": 0, "imu": 0}
        self._next_gps_us = 0
        self._next_imu_us = 0
        self._imu_pending = []
        self._imu_batch_t0 = None
        self._can_pending = []
        self._can_batch_t0 = None
        self._next_can_flush_us = 0
        # Notifications produced outside poll() -- a rate change flushes the
        # batch it invalidates -- wait here for the next poll to drain them.
        self._deferred = []

        # (id, mask) -> {mode, arg, order, per_id}. SPEC.md §9.1 -- the pair IS
        # the subscription's identity, so the table needs no handles. `order`
        # is the installation order, which §9.2 uses to break specificity ties.
        # Empty until a client subscribes: §9.1 makes the cleared table the
        # state after a reconnect, and a device that streamed before being
        # asked would be inventing consent.
        self._subscriptions = {}
        self._install_counter = 0

        # SPEC.md §13 — this device has a display, so it asks the client for
        # what it cannot compute. The declaration is fixed for the connection.
        self._monitor_channels = list(enumerate(
            monitor_channels if monitor_channels is not None else
            (CH_LAP_TIME, CH_LAST_LAP_TIME, CH_BEST_LAP_TIME,
             CH_DELTA_BEST, CH_LAP_NUMBER, CH_SPEED)))
        # SPEC.md 13.4 -- more channels than fit in one complete client write
        # is a device that has made its own rule unsatisfiable. Refused HERE,
        # where the mistake is, rather than at the first MONITOR_LIST: the
        # encoder would have caught it too, but by then the device is running
        # and the traceback names the wrong thing.
        if len(self._monitor_channels) > enc.MONITOR_MAX_CHANNELS:
            raise ValueError(
                f"{len(self._monitor_channels)} monitor channels, but SPEC.md "
                f"13.4 allows {enc.MONITOR_MAX_CHANNELS}: every write must "
                f"carry every slot, and more than that does not fit in one "
                f"write at the minimum ATT MTU")
        # slot -> (value, present, written_at). Absent is a state the display
        # renders, not a value it substitutes -- and SPEC.md §13.5 makes an
        # expired value another way of being absent.
        self._monitor_values = {}
        self._monitor_seq = None
        self._monitor_updates = 0
        # SPEC.md §14.3 — at most one transfer open, so this is one slot and
        # not a table. None when nothing is in flight.
        self._aid = None
        self._aid_last_token = None
        # What this device is holding, as GNSS_AID_INFO reports it. None means
        # it holds nothing, which is the cleared validity bit rather than a
        # zero -- "valid until the Unix epoch" is a different claim.
        self._aid_held_until = None
        # A COUNT, not the payloads. Retaining each applied transfer kept up to
        # AID_MAX_BYTES per commit for the life of the process, with nothing
        # reading it -- and a client re-pushing aiding on every reconnect is
        # the normal pattern, not an unusual one.
        self._aid_applied = 0

        # SPEC.md 9.7 -- a device on its own pack with a gauge that works, so
        # this build exercises both validity bits. `set_power` moves it;
        # nothing else here does, because a supply reading is not on the
        # protocol's clock and does not belong in poll().
        self._power = {"source": SRC_DISCHARGING, "percent": 63}

        # SPEC.md §15 -- the OBD role's per-connection state, all assigned by
        # _obd_clear so a field added there is reset at every edge at once
        # (a field forgotten at one of four hand-copied sites is a §15.7
        # leak). `_obd_masks` is what the most recent probe of THIS
        # connection read (None until one has); nothing is pollable without
        # it, which is what makes declare-verify-use structural rather than
        # convention (SPEC.md 15.4). `_obd_ecu_ids` is the probe's reported
        # response identifiers -- the ones SPEC.md 15.5's fallback delivers
        # on while the poll set is non-empty. `_obd_poll` is the ordered PID
        # schedule; empty means the transmitter is off, and empty is the
        # only state a connection ever starts in.
        #
        # `_obd_last_tx_us` is the schedule: SPEC.md 15.1 bounds SPACING --
        # never two requests closer than the interval -- so what is tracked
        # is the last transmission, not the next one. A next-transmit time
        # was tried first and reset on every accepted OBD_POLL_SET, which
        # let a replacement transmit immediately after a request the old set
        # had just sent. It survives every edge short of construction,
        # because the bus does not care why two frames were close together.
        self._obd_last_tx_us = None
        # SPEC.md 15.4 -- response pacing needs one more fact than the fixed
        # clock did: whether the request already sent is still outstanding.
        # None means nothing is in flight and the next group may go as soon as
        # `interval_ms` allows.
        self._obd_outstanding_since = None
        # How many frames on probe-reported response identifiers have been
        # admitted, and the value that counter held when the outstanding
        # request went out. A COUNTER and not a timestamp: with a car that
        # answers within one tick the answer's bus-arrival time equals the
        # transmit time, and any comparison of instants then reads a request's
        # own tick as having answered it -- which measured as 193 Hz on a car
        # physically capable of 50. Counting cannot tie.
        self._obd_answers = 0
        self._obd_answer_mark = None
        # SPEC.md 15.4 -- the PIDs of the outstanding request, so a frame can
        # be told from a straggler. Functional addressing means several ECUs
        # answer one request and they do NOT answer together: with replies to
        # request N at 10 ms and 15 ms, the first released N+1 and the second
        # -- still answering N -- released N+2 before N+1 had been answered at
        # all, so "one request outstanding" held only on a single-ECU car.
        # Mode 01 responses echo their PID (SPEC.md 15.5 says so, and that is
        # why no client needs correlation state), so matching the echo against
        # the group just asked separates the two without a correlation table
        # and without any J1979 knowledge beyond reading one byte.
        self._obd_outstanding_pids = frozenset()
        # SPEC.md 15.4.2 -- each schedule entry is [group, min_ms, last_tx],
        # one structure rather than parallel lists. Held together because they
        # were briefly apart: anything that restored the poll set without
        # restoring the timestamps beside it left the cursor indexing a list
        # that was no longer the same length.
        self._obd_clear()

    # -- clock ------------------------------------------------------------

    def _monotonic_us(self):
        return (time.monotonic_ns() - self._origin_ns) // 1000

    def now_us(self):
        """SPEC.md §8.1 — one monotonic microsecond clock for every role."""
        return self._clock()

    def on_connect(self):
        """SPEC.md §8.2 and §9.2 — a fresh connection starts from a known
        state: sequence numbers from zero and no subscriptions inherited."""
        self._seq = {k: 0 for k in self._seq}
        self._dropped = {k: 0 for k in self._dropped}
        self._subscriptions.clear()
        self._can_pending, self._can_batch_t0 = [], None
        self._imu_pending, self._imu_batch_t0 = [], None
        self._deferred = []
        # A new connection starts with nothing supplied, so the display shows
        # every slot as unavailable rather than the last connection's numbers.
        self._monitor_values.clear()
        self._monitor_seq = None
        self._monitor_updates = 0
        # A transfer belongs to the connection that opened it (SPEC.md 14.3):
        # the client that would have committed it is gone, and a new client
        # has no way to learn its shape. Without this rule the one-open-
        # transfer slot would be held by a client that cannot close it.
        #
        # `_aid_held_until` deliberately survives: aiding already handed to
        # the receiver is in the receiver, not in this connection, and the
        # next client learns what is held by reading GNSS_AID_INFO.
        self._aid = None
        # SPEC.md 15.7 -- polling never survives a connection edge, and the
        # probe result belongs to the connection that asked for it.
        self._obd_clear()

    def record_refused(self, stream, payload):
        """A notification the transport would not accept.

        The host stack refuses when its transmit queue is full, and the
        notification is then simply never sent. SPEC.md §8.3 is explicit about
        what a device does with data it cannot deliver: discard it and report
        the count, so `dropped` covers loss inside the device *and* loss the
        link refused. Counting the source items rather than the notification
        keeps `dropped` in the units the field is defined in.

        Not retried. Without a ready-to-send callback from the host stack a
        retry is a spin, and a batch redelivered out of order is worse than one
        counted honestly — every record carries the time it was taken, so a
        late batch misrepresents nothing except by being late.
        """
        record = {"gps": "gps_fix", "can": "can_header",
                  "imu": "imu_header"}[stream]
        fields = enc.SCHEMA["records"][record]["fields"]

        def field(name):
            f = next(g for g in fields if g["name"] == name)
            if len(payload) < f["offset"] + f["size"]:
                return 0
            return int.from_bytes(
                payload[f["offset"]:f["offset"] + f["size"]], "little")

        # One GPS notification is one fix; CAN and IMU say how many they hold.
        items = 1 if stream == "gps" else field("count")
        # The header also *reported* a backlog, and building it zeroed the
        # counter. Refusing the notification threw that report away with it,
        # so a device that had already lost 500 frames went on to report the
        # one it lost next. Credit both back: the items this payload was
        # carrying, and the count it was carrying the news of.
        self._dropped[stream] += items + field("dropped")
        return items

    def on_disconnect(self):
        """SPEC.md §9.2 — the table is cleared when the link drops.

        Not when the next connection starts. The difference is only visible
        from the device's own side, but it is the difference between a
        disconnected device holding a stale table and one holding none.
        """
        self._subscriptions.clear()
        self._can_pending, self._can_batch_t0 = [], None
        self._monitor_values.clear()
        self._monitor_seq = None
        self._aid = None
        # SPEC.md 15.7 -- transmit MUST NOT outlive the client that asked for
        # it. Cleared when the link DROPS, exactly as the subscription table
        # is: a disconnected device that kept polling would be transmitting
        # on a car whose owner has walked away with the phone. The fallback
        # delivery dies with the poll set it serves.
        self._obd_clear()
        # The MTU was NEGOTIATED, so it describes a link that has gone. It used
        # to persist until a new central happened to replace it, so batches for
        # the next connection were sized to this one's link.
        self.mtu = self._device_mtu_ceiling

    def simulate_loss(self, stream, count):
        """Pretend the device accepted `count` items and had to discard them.

        Not decoration: a client has to handle loss, and loss on a desktop
        peripheral otherwise never happens, so the path that reads `dropped`
        would go untested until a real device on a real track produced some.
        """
        if stream not in self._dropped:
            raise ValueError(f"unknown stream {stream!r}")
        self._dropped[stream] += count

    # SPEC.md §8.2 — seq counts notifications **sent**. That makes it a fact
    # about delivery, not about encoding, and it is now assigned at delivery.
    #
    # It used to be consumed while building the payload, which two different
    # bugs then had to work around: a notification nobody was subscribed to
    # burned a number and never gave it back, so the first one actually
    # delivered carried 2; and returning a number on refusal handed back one a
    # LATER notification had already taken, so a superseded batch produced the
    # delivered sequence 1, 1, 2, 3. The second was introduced fixing the
    # first, which is the clearest possible sign the number was being owned in
    # the wrong place.
    #
    # A payload is now encoded with a placeholder, `stamp` writes the pending
    # number into its header, and `commit` advances the counter only once the
    # transport reports the notification went out. A refusal consumes nothing,
    # so there is nothing to give back.
    SEQ_PLACEHOLDER = 0

    def _seq_offset(self, stream):
        record = {"gps": "gps_fix", "can": "can_header",
                  "imu": "imu_header"}[stream]
        return next(f["offset"] for f in enc.SCHEMA["records"][record]["fields"]
                    if f["name"] == "seq")

    def stamp_seq(self, stream, payload):
        """Write the pending sequence number in, without consuming it."""
        off = self._seq_offset(stream)
        out = bytearray(payload)
        struct.pack_into("<H", out, off, self._seq[stream])
        return bytes(out)

    def commit_seq(self, stream):
        """The notification went out; the number is spent."""
        self._seq[stream] = (self._seq[stream] + 1) & 0xFFFF

    def peek_seq(self, stream):
        return self._seq[stream]

    def _take_dropped(self, stream):
        """SPEC.md §8.3 — saturates at 65535 and MUST NOT wrap."""
        n = min(self._dropped[stream], 0xFFFF)
        self._dropped[stream] = 0
        return n

    @property
    def notify_bytes(self):
        """ATT payload available for one notification on the CURRENT link:
        the negotiated MTU minus the 3-byte ATT notification header. This is
        what batching is sized against; SPEC.md §2 gives a client the same
        bound from its own stack, so Info carries no copy of it.
        """
        return self.mtu - 3

    # -- Info -------------------------------------------------------------

    def info(self):
        """SPEC.md 4 and 4.1.

        Every capacity follows its capability bit. A device declaring no GPS
        that still reports gps_rate_hz has published a role it does not have,
        and a client sizing anything from it has been told something false --
        which is why the encoder refuses to emit one. This method used to
        report all three groups unconditionally, and only ever ran on a build
        that declared all three, so nothing noticed.
        """
        caps = self.capabilities
        gps = bool(caps & CAP_GPS)
        can = bool(caps & CAP_CAN)
        imu = bool(caps & CAP_IMU)
        obd = bool(caps & CAP_OBD)
        return enc.encode_info({
            "protocol_major": PROTOCOL_MAJOR,
            "protocol_minor": PROTOCOL_MINOR,
            "capabilities": caps,
            "gps_rate_hz": self.gps_hz if gps else 0,
            "gps_max_rate_hz": 25 if gps else 0,
            "can_subscription_slots": CAN_SUBSCRIPTION_SLOTS if can else 0,
            "can_max_frames_per_s": CAN_MAX_FRAMES_PER_S if can else 0,
            "imu_rate_hz": self.imu_hz if imu else 0,
            "imu_max_rate_hz": 833 if imu else 0,
            "obd_poll_slots": OBD_POLL_SLOTS if obd else 0,
            "reserved_22": 0,
            "clock_flags": 0b10,      # survives reconnect; not GNSS-disciplined
        })

    # -- GPS --------------------------------------------------------------

    def _gps_fix(self, now):
        st = self.circuit.at(now / 1e6)

        validity = (V_T_UTC | V_T_UTC_RESOLVED | V_POSITION | V_ALT_MSL
                    | V_VELOCITY | V_HEAD_MOT | V_H_ACC | V_V_ACC | V_S_ACC
                    | V_P_DOP | V_NUM_SV)
        return enc.encode_gps_fix({
            "seq": self.SEQ_PLACEHOLDER,   # stamped at delivery (§8.2)
            "dropped": self._take_dropped("gps"),
            "validity": validity,
            "t_device": now,
            "t_utc": self._wall_origin_ms + now // 1000,
            "lat": round(st["lat"] * 1e7),
            "lon": round(st["lon"] * 1e7),
            "alt_msl": 120_000,
            # alt_ellipsoid's bit is deliberately clear: this device does not
            # compute it, and SPEC.md §5.1 requires the field to read absent
            # rather than as a plausible zero.
            "alt_ellipsoid": 0,
            "vel_n": round(st["vel_n"] * 1000),
            "vel_e": round(st["vel_e"] * 1000),
            "vel_d": 0,
            "head_mot": round(st["heading"] * 1e5),
            "h_acc": 850, "v_acc": 1400, "s_acc": 90,
            "p_dop": 130,
            "fix_type": FIX_3D,
            "num_sv": 14,
            # SPEC.md §5.6 — this device computes its fix from its own model
            # at a known instant, so t_device IS the solution epoch. Real
            # firmware sets this only when the receiver gives it the epoch.
            "fix_flags": FIX_FLAG_SOLUTION_EPOCH,
            "ext_count": 0,
        })

    # -- IMU --------------------------------------------------------------

    @property
    def _imu_period_us(self):
        return round(1_000_000 / self.imu_hz)

    def _imu_capacity(self):
        header = 20      # imu_header
        return max(1, (self.notify_bytes - header) // 12)

    def _imu_sample(self, now):
        st = self.circuit.at(now / 1e6)
        return {
            # Longitudinal acceleration is now real, and is the exact
            # derivative of the speed on the CAN bus.
            "ax": round(st["long_g"] * 1000),
            "ay": round(st["lat_g"] * 1000),
            # SPEC.md §7.1 — specific force, not acceleration: a level car at
            # rest pushes back against gravity, so the UP axis reads +1 g. The
            # comment here used to say "1 g down" beside this same +1000, which
            # is the exact ambiguity §7.1 was written to remove.
            "az": 1000,
            "gx": 0, "gy": 0,
            "gz": round(st["yaw_rate"] / 0.05),
        }

    def _imu_saturation(self):
        """SPEC.md §7.2 — IMU_SATURATED if any pending sample is at a rail.

        This synthetic vehicle never gets near one, so the flag stays clear and
        the branch looks like dead code. It is not: it is the shape real
        firmware needs, and without it this device would model a sensor that
        cannot saturate, which is not a sensor.
        """
        rails = (-32768, 32767)
        for sample in self._imu_pending:
            if any(sample[axis] in rails
                   for axis in ("ax", "ay", "az", "gx", "gy", "gz")):
                return IMU_SATURATED
        return 0

    def _flush_imu(self):
        if not self._imu_pending:
            return None
        payload = enc.encode_imu_batch({
            "seq": self.SEQ_PLACEHOLDER,   # stamped at delivery (§8.2)
            "dropped": self._take_dropped("imu"),
            "t_base": self._imu_batch_t0,
            "period": self._imu_period_us,
            "count": len(self._imu_pending),
            "flags": IMU_ACCEL | IMU_GYRO | self._imu_saturation(),
            "reserved": 0,
        }, self._imu_pending)
        self._imu_pending, self._imu_batch_t0 = [], None
        return payload

    # -- CAN --------------------------------------------------------------

    # The synthetic bus. Each signal has an id, a natural bus rate and an
    # encoder over the motion state.
    def _bus_frames(self, now):
        """The synthetic bus. Little-endian throughout, matching the protocol.

        Layouts are documented in README.md so a client can be configured
        against them without reading this.
        """
        st = self.circuit.at(now / 1e6)
        # 0x0C0 @ 50 Hz — engine
        yield 0x0C0, 50, struct.pack("<HHBB2x", round(st["rpm"]),
                                     round(st["speed"] * 3.6),
                                     st["gear"], 90)
        # 0x1A0 @ 20 Hz — driver inputs
        yield 0x1A0, 20, struct.pack("<BBh4x", round(st["throttle"]),
                                     round(st["brake"]),
                                     round(st["heading"] * 10))
        # 0x2E0 @ 10 Hz — chassis
        yield 0x2E0, 10, struct.pack("<hhh2x", round(st["lat_g"] * 100),
                                     round(st["long_g"] * 100),
                                     round(st["yaw_rate"] * 10))

    def _can_capacity(self):
        header = 16
        return max(1, (self.notify_bytes - header) // (7 + 8))

    def _flush_can(self, now):
        """The pending batch, or None when there is nothing to send.

        SPEC.md 6.2 -- t_base is the bus-arrival time of record 0, so a batch
        with no record 0 has no honest timestamp to carry. A quiet bus is
        reported by sending nothing, and the timer below therefore produces a
        notification only when there is something in it.
        """
        if not self._can_pending:
            return None
        header = {
            "seq": self.SEQ_PLACEHOLDER,   # stamped at delivery (§8.2)
            "dropped": self._take_dropped("can"),
            "t_base": self._can_batch_t0,
            # SPEC.md 15.6 -- set exactly while the poll set is non-empty, on
            # every batch, so anyone reading the stream can tell a
            # transmitting device from a listening one. The probe does not
            # set it: it is a bounded handful of frames inside one control
            # round trip, disclosed by the OBD_INFO exchange itself.
            "flags": CAN_FLAG_POLLING if self._obd_poll else 0,
            "count": len(self._can_pending),
            "reserved": 0,
        }
        payload = enc.encode_can_batch(header, self._can_pending)
        self._can_pending, self._can_batch_t0 = [], None
        return payload

    # -- polling ----------------------------------------------------------

    def poll(self, undelivered=()):
        """Notifications due now, as (characteristic, payload) pairs.

        A stream is produced only if its capability bit is set. SPEC.md 4.1
        says an inert characteristic never notifies, and this is where that
        becomes true rather than merely stated: a device configured with only
        `control` used to emit GPS and IMU regardless, because poll() had
        never been told what the device claimed to be.

        `undelivered` names streams whose previous notification the transport
        has not taken yet. Only the DISCRETIONARY flush is held back for those
        -- the partial-batch timer below, which exists so a quiet bus still
        delivers. The bounded flushes are not discretionary and still happen:
        SPEC.md 6.1 caps a CAN batch at a 655.35 ms span because `dt` cannot
        express more, and capacity is what fits in one notification.

        Building a batch the transport cannot yet take only means superseding
        it a moment later, and a superseded batch is loss (SPEC.md 8.3). This
        is the difference between reporting a busy radio as dropped frames and
        sending one fuller batch when the radio frees up.
        """
        now = self.now_us()
        out, self._deferred = self._deferred, []
        caps = self.capabilities

        if caps & CAP_GPS and self.gps_hz and now >= self._next_gps_us:
            out.append(("gps", self._gps_fix(now)))
            self._next_gps_us = now + round(1_000_000 / self.gps_hz)

        if caps & CAP_IMU and self.imu_hz:
            # A device that has not been polled for a while must not replay the
            # gap. Delivering a backlog means stale samples arriving as fast as
            # the radio will take them, which is worse than losing them: the
            # timestamps say when they were taken, so a client cannot tell the
            # stream is behind. SPEC.md §8.3 says discard and report it.
            period = self._imu_period_us
            capacity = self._imu_capacity()
            if now > self._next_imu_us:
                behind = (now - self._next_imu_us) // period
                if behind > capacity:
                    skipped = behind - capacity
                    self._dropped["imu"] += skipped
                    self._next_imu_us += skipped * period
            while now >= self._next_imu_us:
                t = self._next_imu_us
                if self._imu_batch_t0 is None:
                    self._imu_batch_t0 = t
                self._imu_pending.append(self._imu_sample(t))
                self._next_imu_us = t + self._imu_period_us
                if len(self._imu_pending) >= self._imu_capacity():
                    out.append(("imu", self._flush_imu()))

        # SPEC.md 15.4 -- the transmitter. One request per interval, walking
        # the list in order and wrapping; a request the bus did not answer is
        # abandoned when the next transmission is due, which in this model is
        # implicit -- the synthetic ECUs answer immediately or not at all.
        #
        # Spacing is measured FROM THE LAST TRANSMISSION, whatever caused it
        # (§15.1): a replacement poll set does not reset it, a probe advances
        # it, and a device not polled for a while emits one request rather
        # than a backlog, because `_obd_last_tx_us` only moves when a frame
        # actually goes out.
        # Snapshot, because control writes run outside this loop on a real
        # transport: an empty OBD_POLL_SET landing between the truthiness
        # check and the indexing rebinds `_obd_poll` to [], and len() of the
        # NEW list divides by zero. Every mutation of the pair rebinds
        # rather than mutating in place, so a local reference stays a
        # consistent pre- or post-write view.
        # SPEC.md 15.4 -- answers are admitted BEFORE the transmit decision,
        # and the order is load-bearing. Taken afterwards, an answer arriving
        # in the same tick as a transmission counts against the request that
        # transmission just sent, and the loop fires again one tick later: the
        # spacing measured as an alternating 5 ms / 20 ms on a car whose
        # latency is a flat 20. Deliver first, then decide.
        obd_frames = (list(self._due_obd_frames(now))
                      if caps & CAP_CAN else [])

        obd_poll = self._obd_poll
        obd_interval_us = self._obd_interval_ms * 1000
        if caps & CAP_OBD and obd_poll:
            # SPEC.md 15.4 -- "answered" is a frame on a probe-reported
            # response identifier at or after the outstanding request went
            # out. The device does not correlate a response to a request
            # (SPEC.md 15.5 is why it never needs to), so this is the only
            # signal available.
            #
            # This runs BEFORE the transmit decision, and must: functional
            # addressing means several ECUs answer one request, and if the
            # queue were walked afterwards the SECOND ECU's answer would clear
            # the state belonging to the request just sent. That defect
            # measured as 78 requests/s where the car's 20 ms latency allows
            # 50 -- pacing off a car that had not answered yet.
            if (self._obd_outstanding_since is not None
                    and self._obd_answer_mark is not None
                    and self._obd_answers > self._obd_answer_mark):
                self._obd_outstanding_since = None
            # SPEC.md 15.1 -- an unanswered request is abandoned, not retried.
            # Abandoning is what clears the way for the next group; the ECU may
            # still answer afterwards and SPEC.md 15.5 delivers that frame like
            # any other.
            if (self._obd_outstanding_since is not None
                    and now - self._obd_outstanding_since
                    >= OBD_RESPONSE_TIMEOUT_US):
                self._obd_outstanding_since = None
            # SPEC.md 15.4 -- both conditions, and `interval_ms` is a MINIMUM
            # rather than a period. With interval 0 the car is the only pacing
            # there is, which is safe precisely because the first condition
            # waits for it.
            answered = self._obd_outstanding_since is None
            spaced = (self._obd_last_tx_us is None
                      or now - self._obd_last_tx_us >= obd_interval_us)
            if answered and spaced:
                due = self._obd_next_group(now)
                if due is not None:
                    entry, group = due
                    entry[2] = now
                    self._obd_last_tx_us = now
                    self._obd_outstanding_since = now
                    self._obd_answer_mark = self._obd_answers
                    self._obd_outstanding_pids = frozenset(group)
                    self._obd_transmit(group, now)

        # OBD responses first: anything a probe put on the synthetic bus
        # between polls carries an older bus-arrival time than the broadcast
        # frames generated at `now`, and records within a batch must not run
        # backwards. Collected above, before the transmitter ran.
        # The CAN branch is already gated by `_subscriptions`, which stays
        # empty on a device whose CAN opcodes all answer unsupported_opcode --
        # but relying on a side effect of the control plane to enforce a
        # capability is how the rule stops holding the moment either changes.
        for frame in itertools.chain(
                obd_frames,
                self._due_can_frames(now) if caps & CAP_CAN else ()):
            self._accept_frame(frame, now, out)

        # Flush partial batches on a timer so a quiet bus or a slow ODR still
        # delivers, rather than waiting for a batch that may never fill. The
        # fallback (SPEC.md 15.5) is a delivery path like the table, so an
        # active poll set keeps the timer alive with no subscription
        # installed -- the common OBD-only client has exactly that shape.
        if (caps & CAP_CAN and (self._subscriptions or self._obd_poll)
                and "can" not in undelivered
                and now >= self._next_can_flush_us):
            batch = self._flush_can(now)
            if batch is not None:
                out.append(("can", batch))
            self._next_can_flush_us = now + 100_000
        return [(c, p) for c, p in out if p is not None]

    def _admit(self, sub, cid, now):
        """SPEC.md §6.8 -- one admission decision for every frame source.

        Per-identifier mode state: the first matching frame is forwarded in
        every mode, and `periodic` then rations by `emitted_at`. Stated once
        and called from both frame generators, because two copies of the
        governance rule are two rules the moment either is edited -- the
        copies had already drifted before this was extracted."""
        st = sub["per_id"].setdefault(
            cid, {"last": 0, "seen": 0, "emitted_at": 0})
        st["seen"] += 1
        first = st["seen"] == 1
        emit = True
        if sub["mode"] == SUB_PERIODIC and sub["arg"] and not first:
            emit = (now - st["emitted_at"]) >= sub["arg"] * 1000
        if emit:
            st["emitted_at"] = now
        return emit

    def _accept_frame(self, frame, now, out):
        """One admitted frame into the pending batch, flushing when the dt
        window or capacity forces it (SPEC.md §6.1). The identity carries
        the format bit in bit 29 -- can_record's own layout -- so `extended`
        is derived here rather than assumed: a 29-bit OBD response id copied
        into `id` with extended forced false was an identifier the encoder
        rightly refused, and poll() crashed on a conforming car."""
        if self._can_batch_t0 is None:
            self._can_batch_t0 = frame["_t"]
        dt = (frame["_t"] - self._can_batch_t0) // 10
        if dt > 0xFFFF or len(self._can_pending) >= self._can_capacity():
            batch = self._flush_can(now)
            if batch is not None:
                out.append(("can", batch))
            self._can_batch_t0 = frame["_t"]
            dt = 0
        raw = frame["id"]
        self._can_pending.append({
            "dt": dt, "id": raw & 0x1FFFFFFF,
            "extended": bool(raw & (1 << 29)),
            "fd": False, "rtr": False,
            "len": len(frame["payload"]), "payload": frame["payload"],
        })

    def _governing(self, cid):
        """SPEC.md §9.2 — of the subscriptions matching `cid`, the one that
        governs: most specific mask first, then the one installed earliest. A
        frame is forwarded at most once, whatever else matches it."""
        matches = [(key, s) for key, s in self._subscriptions.items()
                   if (cid & key[1]) == (key[0] & key[1])]
        if not matches:
            return None
        return min(matches,
                   key=lambda ks: (-bin(ks[0][1]).count("1"),
                                   ks[1]["order"]))[1]

    def _due_can_frames(self, now):
        for cid, rate_hz, payload in self._bus_frames(now):
            sub = self._governing(cid)
            if sub is None:
                continue
            # The natural bus rate of this synthetic broadcast signal -- a
            # property of the frame GENERATOR, not of admission, so it stays
            # here while the §6.8 decision lives in _admit.
            st = sub["per_id"].setdefault(
                cid, {"last": 0, "seen": 0, "emitted_at": 0})
            interval = round(1_000_000 / rate_hz)
            if now - st["last"] < interval:
                continue
            st["last"] = now
            if self._admit(sub, cid, now):
                yield {"id": cid, "payload": payload, "_t": now}

    # -- OBD (SPEC.md §15) --------------------------------------------------

    def _obd_stop(self, *, flush):
        """SPEC.md 15.7 -- the transmitter off, and nothing stranded.

        `flush` runs everything already accepted out before the poll set
        clears: bus arrivals still queued for admission go through it under
        the pre-stop state (so the fallback they were accepted under still
        delivers them), and the pending batch is flushed -- BEFORE the set
        clears, so the batch's polling flag is truthful. Arrivals scheduled
        beyond `now` have not happened yet and are dropped unheard. The
        connection edges pass flush=False: the link those frames belonged
        to is gone.
        """
        if flush:
            now = self.now_us()
            for frame in self._due_obd_frames(now):
                self._accept_frame(frame, now, self._deferred)
            batch = self._flush_can(now)
            if batch is not None:
                self._deferred.append(("can", batch))
        self._obd_poll, self._obd_interval_ms = [], 0
        self._obd_index = 0
        self._obd_rx = []
        # `_obd_outstanding_since` is NOT cleared, for the reason
        # `_obd_last_tx_us` is not: it is a fact about the BUS, and the bus
        # does not care why the client changed its mind. Clearing it let a
        # stop-and-re-arm -- or an OBD_INFO, which calls this helper -- launch
        # a second request while the first was still unanswered on the wire:
        # with an 80 ms car, a stop 10 ms after a request put the next one out
        # 5 ms later, two outstanding at once, which SPEC.md 15.1 forbids
        # outright. The request now stays outstanding until it is answered or
        # abandoned at OBD_RESPONSE_TIMEOUT_US, exactly as if nothing had
        # happened -- and since `_obd_rx` is cleared here, an unanswered one
        # reaches the timeout rather than being released by its own reply.

    def _obd_clear(self, *, flush=False):
        """Everything _obd_stop clears, plus the probe result -- the
        connection edges, and a probe nothing answered (SPEC.md 15.2)."""
        self._obd_stop(flush=flush)
        self._obd_masks = None
        self._obd_ecu_ids = frozenset()
        # A pending OBD_INFO response belongs to the connection that asked
        # for it: the edges run this, so it dies with the link rather than
        # being handed to the next central as an answer to nothing.
        self._obd_pending_info = None

    @staticmethod
    def _mask_has(masks, pid):
        """SPEC.md 15.3 -- bit n of window w is PID 0x01 + 32w + n, LSB
        first. The ONE statement of the window arithmetic: the probe, the
        transmit loop and the poll-set gate all call this, so an off-by-one
        cannot leave them disagreeing about which PIDs one car supports."""
        window, bit = divmod(pid - 0x01, 32)
        return bool(masks[window] & (1 << bit))

    def _obd_pid_supported(self, pid):
        """SPEC.md 15.4 -- pollable means inside 0x01..0x60 AND declared by
        the most recent probe of this connection. With no probe, nothing is:
        `_obd_masks` starts None and only OBD_INFO fills it."""
        if not OBD_PID_FLOOR <= pid <= OBD_PID_CEILING:
            return False
        if self._obd_masks is None:
            return False
        return self._mask_has(self._obd_masks, pid)

    def _obd_pid_data(self, pid, st, masks=None):
        """Data bytes of a positive Mode 01 response.

        J1979 encodings for the PIDs a lap-timing client actually reads --
        derived from the same motion state as the GPS fix and the IMU sample,
        so a client cross-checking channels finds them consistent -- and a
        deterministic one-byte filler for the rest of the declared set. The
        device does NOT decode any of this; it is the synthetic CAR.

        `masks` is the answering ECU's own supported-PID windows, and is
        required for the MASK PIDs. 0x20 and 0x40 are inside 0x01..0x60 and
        the probe's union claims them, so a client may poll them like any
        other PID -- and their answer is four bytes, and is DIFFERENT PER
        ECU. The filler below returned one byte for both, identical from
        every ECU, which made a grouped `(0x20, 0x40)` decode as 0x20 with
        four bytes of data taken from its neighbour: a plausible wrong value
        of exactly the kind SPEC.md 1.1 exists to prevent, and the reason
        this is a parameter rather than a lookup.
        """
        if masks is not None and pid in (0x00, 0x20, 0x40):
            # 0x00 -> window 0, 0x20 -> 1, 0x40 -> 2. NOT `_mask_has`'s
            # divmod(pid - 1, 32): that maps a PID to the window CONTAINING
            # it, and a mask PID names the window it DESCRIBES, which is the
            # next one up. The two disagree for exactly these three values.
            return _j1979_mask_bytes(masks[pid // 32])
        if pid == 0x04:            # engine load, A*100/255
            return bytes([round(st["throttle"] * 255 / 100)])
        if pid == 0x05:            # coolant temperature, A-40
            return bytes([90 + 40])
        if pid == 0x0C:            # engine speed, (256A+B)/4 -- big-endian!
            q = round(st["rpm"] * 4)
            return bytes([(q >> 8) & 0xFF, q & 0xFF])
        if pid == 0x0D:            # vehicle speed, km/h
            return bytes([min(255, round(st["speed"] * 3.6))])
        if pid == 0x0F:            # intake air temperature, A-40
            return bytes([35 + 40])
        if pid == 0x11:            # throttle position, A*100/255
            return bytes([round(st["throttle"] * 255 / 100)])
        # The two-byte PIDs matter to grouping and not to decoding: a group's
        # answer must fit seven bytes (SPEC.md 15.4.1), so a car that returned
        # one byte for every PID it does not model would let a client pack
        # groups no real car would answer in a single frame, and the rate
        # measured against it would be a number no vehicle produces.
        if pid == 0x10:            # MAF, (256A+B)/100 g/s
            q = min(0xFFFF, round(st["throttle"] * 45))
            return bytes([(q >> 8) & 0xFF, q & 0xFF])
        if pid == 0x1F:            # run time since start, 256A+B seconds
            return bytes([0x00, 0x96])
        if pid == 0x42:            # control module voltage, (256A+B)/1000 V
            return bytes([0x36, 0xB0])
        if pid == 0x43:            # absolute load, (256A+B)*100/255
            q = min(0xFFFF, round(st["throttle"] * 2.55))
            return bytes([(q >> 8) & 0xFF, q & 0xFF])
        return bytes([pid ^ 0x55])

    @staticmethod
    def _obd_mode01_frame(body):
        """The car's answer to one Mode 01 request, `body` being the
        concatenated `pid`+`data` pairs.

        A single frame carries seven data bytes; one is the `41` echo, so
        six bytes of pairs fit and a seventh does not. Past that the car
        answers with an ISO-TP FIRST FRAME, which is what makes the failure
        mode of a badly sized group real rather than theoretical: SPEC.md
        15.5 says such a frame is ordinary -- forwarded if subscribed,
        otherwise dropped -- and the transfer it opens dies unanswered,
        because SPEC.md 15.1 forbids this device the flow control that would
        continue it. No consecutive frames are ever queued here, and that is
        the point: the client sees one useless frame and regroups.
        """
        if 1 + len(body) <= 7:
            frame = bytes([1 + len(body), 0x41]) + body
            return frame + b"\x00" * (8 - len(frame))
        total = 1 + len(body)
        return bytes([0x10 | (total >> 8), total & 0xFF, 0x41]) + body[:5]

    @classmethod
    def _obd_response_frame(cls, pid, data):
        """The single-PID case, kept for the probe (SPEC.md 15.2), whose mask
        requests are never grouped."""
        return cls._obd_mode01_frame(bytes([pid]) + data)

    @staticmethod
    def _obd_request_frame(group):
        """SPEC.md 15.1 -- the request this device puts on the bus.

        `[1+g, 0x01, p1..pg]` and padding, to DLC 8. This is the whole of why
        grouping does not move SPEC.md 15.1's bus bound: a six-PID request
        occupies exactly the eight bytes a one-PID request occupies, so the
        worst case stays one short frame per obd_min_interval_ms. That claim
        is load-bearing enough to be BUILT rather than asserted in prose --
        `_obd_transmit` answers what this frame says and not what its caller
        meant, so a builder that got the PCI or the padding wrong would show
        up as wrong responses in every OBD test rather than as a comment
        nobody can check.
        """
        body = bytes([1 + len(group), 0x01]) + bytes(group)
        if len(body) > 8:
            # Unreachable through the control plane: SPEC.md 15.4.1 rule 6
            # refuses a group of seven before it is ever installed. Raised
            # rather than truncated because a request frame that does not fit
            # a classic CAN frame is not a frame to put on a car.
            raise ValueError(f"group of {len(group)} exceeds the request frame")
        return body + b"\x00" * (8 - len(body))

    @staticmethod
    def _obd_request_pids(frame):
        """The PIDs a request frame names, read back off the bus.

        The car answers what it heard. Going through the frame rather than
        the caller's list is what makes _obd_request_frame testable at all:
        a wrong PCI length silently changes which PIDs the ECUs see.
        """
        return tuple(frame[2:1 + frame[0]])

    def _obd_transmit(self, group, now):
        """One Mode 01 request on the synthetic bus, and what answers it.

        `group` is one or more PIDs (SPEC.md 15.4.1) and goes out as ONE
        request frame, built by `_obd_request_frame` and read back by the car
        exactly as an ECU would read it.

        The REQUEST frame never reaches `_obd_rx`: the CAN stream carries
        what the device hears, never what it says (SPEC.md 15.5). Every ECU
        whose own masks cover ANY PID in the group answers -- functional
        addressing asks the car, not an ECU -- and each answers with the
        subset it implements, which is why a client sizing a group against
        "one ECU answers everything" is sizing conservatively.
        """
        st = self.circuit.at(now / 1e6)
        asked = self._obd_request_pids(self._obd_request_frame(group))
        for ecu_id in sorted(self.OBD_ECUS):
            masks = self.OBD_ECUS[ecu_id]
            body = b"".join(bytes([pid]) + self._obd_pid_data(pid, st, masks)
                            for pid in asked if self._mask_has(masks, pid))
            if not body:
                # An ECU implementing none of the group says nothing. Real
                # ECUs vary here -- some refuse a group they support only
                # partly -- which is a car behaviour, not a device one, and
                # the reason SPEC.md 15.4.1 tells a client to learn per-ECU
                # attribution by polling singly first.
                continue
            self._obd_rx.append((now + self.obd_latency_us, ecu_id,
                                 self._obd_mode01_frame(body)))

    def _obd_next_group(self, now):
        """SPEC.md 15.4.2 -- the next group due, or None if none is.

        Each group carries its own minimum interval, so "due" is a question
        about that group's own last transmission and not about a pass counter.
        A ratio could not express what a client actually wants: under response
        pacing the cycle time is set by the car, so `one pass in five` is a
        different rate on every vehicle and drifts within a session. An
        interval holds its rate whatever the car does.

        Returns `(entry, group)`; the caller stamps the entry. Bounded
        by the schedule length, so a moment when nothing is due returns None
        rather than spinning.
        """
        for _ in range(len(self._obd_poll)):
            entry = self._obd_poll[self._obd_index % len(self._obd_poll)]
            self._obd_index += 1
            group, min_ms, last = entry
            if min_ms == 0 or last is None or now - last >= min_ms * 1000:
                return entry, group
        return None

    @staticmethod
    def _obd_echoed_pid(payload):
        """The PID a Mode 01 response frame answers for, or None.

        Single frame: `[pci, 0x41, pid, ...]`. ISO-TP first frame, which is
        how an oversized group is answered (SPEC.md 15.4.1):
        `[0x1L, LL, 0x41, pid, ...]`. Anything else -- a consecutive frame,
        another tester's transfer, a negative response -- answers for nothing
        this device asked.
        """
        if len(payload) >= 3 and payload[0] >> 4 == 0 and payload[1] == 0x41:
            return payload[2]
        if len(payload) >= 4 and payload[0] >> 4 == 1 and payload[2] == 0x41:
            return payload[3]
        return None

    def _obd_fallback_delivers(self, cid):
        """SPEC.md 15.5 -- the one delivery rule beside the table: while the
        poll set is non-empty, a frame on a probe-reported response
        identifier that matches no installed subscription is forwarded
        every_frame. A fallback and not an entry: it holds no slot, keeps no
        mode state, and dies with the poll set (SPEC.md 15.7)."""
        return bool(self._obd_poll) and cid in self._obd_ecu_ids

    def _due_obd_frames(self, now):
        """Bus arrivals the device's own requests caused.

        Admission runs the table first, exactly as for any other frame: a
        frame a subscription matches is governed by that subscription's mode
        (SPEC.md §6.8), so a client that installs a periodic entry on a
        response identifier gets exactly what it asked for. Only a frame the
        table ignores falls through to SPEC.md 15.5's rule."""
        pending, self._obd_rx = self._obd_rx, []
        hold = []
        for t, cid, payload in pending:
            # A probe schedules its arrivals at the transmit instants
            # SPEC.md 15.1 requires, which may still be ahead of the clock:
            # a frame is not on the bus until its time comes, so it is held,
            # not admitted early.
            if t > now:
                hold.append((t, cid, payload))
                continue
            if cid in self._obd_ecu_ids and \
                    self._obd_echoed_pid(payload) in self._obd_outstanding_pids:
                self._obd_answers += 1
            sub = self._governing(cid)
            if sub is None:
                if self._obd_fallback_delivers(cid):
                    yield {"id": cid, "payload": payload, "_t": t}
                continue
            if self._admit(sub, cid, now):
                yield {"id": cid, "payload": payload, "_t": t}
        self._obd_rx = hold + self._obd_rx

    def _obd_probe(self, now):
        """SPEC.md 15.2 -- transmit the mask requests, report what answered.

        Measured when asked, like GET_POWER: each OBD_INFO re-probes, so the
        answer describes the car the device is plugged into now -- and every
        completed probe replaces the probe result and clears the poll set
        with it (§15.7): the set never outlives the result it was verified
        against.

        The probe runs on the SAME transmit schedule as the poll loop
        (SPEC.md 15.1): each request is placed at the next legal instant --
        after the last transmission by the greater of the 50 ms collection
        window (§15.2) and the declared floor -- and the response frames it
        causes carry those instants as bus-arrival times. _due_obd_frames
        releases a frame only once the clock reaches it, so the stream
        shows the spacing and the one-outstanding bound rather than a burst
        at one instant. What stays compressed is the indication:
        The indication is NOT compressed: this returns `(done_us, detail)`,
        where `done_us` is the instant the probe completes -- the last
        request's transmit instant plus its 50 ms collection window -- and
        the caller holds the detail until then (`due_control_response`), so
        the response is sent only when the probe is complete and no probe
        event is still scheduled ahead of the clock at delivery.

        The mask RESPONSES are real bus frames and enter `_obd_rx` like any
        other arrival -- a client subscribed to 0x7E8 sees `41 00 ...` go
        past, carrying J1979's OWN bit order, while the detail below
        carries SPEC.md 15.3's. The transcription between them is exactly
        what the conformance vector pins."""
        # SPEC.md 15.2 -- the probe replaces the result the poll set was
        # verified against, so the set clears first, flushing what it had
        # already accepted (§15.7).
        self._obd_stop(flush=True)
        # SPEC.md 15.2 -- the probe stays clock-paced: it has its own
        # fallback-addressing logic, runs once, and has no schedule to
        # pace against. 50 ms is a collection window, not a rate bound.
        step_us = 50_000
        t = now
        if self._obd_last_tx_us is not None:
            t = max(t, self._obd_last_tx_us + step_us)
        answered = {}
        union = [0, 0, 0]
        # PID 0x00 first; 0x20 and 0x40 only if the union so far claims them
        # (SPEC.md 15.2 -- a device MUST NOT request a mask PID the union
        # does not claim).
        for window, mask_pid in enumerate((0x00, 0x20, 0x40)):
            if mask_pid and not self._mask_has(union, mask_pid):
                continue
            for ecu_id in sorted(self.OBD_ECUS):
                masks = self.OBD_ECUS[ecu_id]
                if mask_pid and not self._mask_has(masks, mask_pid):
                    continue
                answered[ecu_id] = True
                union[window] |= masks[window]
                self._obd_rx.append(
                    (t, ecu_id,
                     self._obd_response_frame(
                         mask_pid, _j1979_mask_bytes(masks[window]))))
            self._obd_last_tx_us = t
            t += step_us
        # Window 0 always transmits, so `_obd_last_tx_us` names the final
        # request whether or not anything answered; the probe is complete
        # one collection window after it (SPEC.md 15.2), and every response
        # frame scheduled above carries an earlier instant than this.
        done_us = self._obd_last_tx_us + 50_000
        if not answered:
            self._obd_masks = None
            self._obd_ecu_ids = frozenset()
            return done_us, enc.encode_obd_info(dict(validity=0, count=0), [])
        self._obd_masks = tuple(union)
        self._obd_ecu_ids = frozenset(answered)
        return done_us, enc.encode_obd_info(
            dict(validity=1, count=len(answered),
                 request_id=self.OBD_REQUEST_ID,
                 supported_01_20=union[0], supported_21_40=union[1],
                 supported_41_60=union[2]),
            [dict(id=ecu_id) for ecu_id in sorted(answered)])

    def due_control_response(self, now=None):
        """The deferred OBD_INFO response, once its probe has completed.

        None until then. SPEC.md 15.2 -- the response reports a completed
        probe, so the transport polls this from its pump and delivers what
        comes back; the state changes (the cleared poll set, the replaced
        probe result) took effect when the request was applied, exactly as
        SPEC.md 9.6 orders them, and only the indication waits.
        """
        if self._obd_pending_info is None:
            return None
        if now is None:
            now = self.now_us()
        done_us, response = self._obd_pending_info
        if now < done_us:
            return None
        self._obd_pending_info = None
        return response

    # -- Control ----------------------------------------------------------

    def set_negotiated_mtu(self, att_mtu):
        """The real ATT MTU, as opposed to the one this device assumed.

        Batch sizing had been driven entirely by the --mtu argument, so a
        device told 247 while the link negotiated 185 built notifications the
        link could not carry -- refused by the stack, or truncated, depending
        on how forgiving it is. Batches are also never sized above the
        configured ceiling, so a central that negotiates a larger MTU than
        this build was configured for does not get larger batches than the
        build was designed to hold.
        """
        self.mtu = min(att_mtu, self._device_mtu_ceiling)
        # Recorded because SPEC.md §14.3 needs the difference. Sizing a
        # notification from an assumed MTU costs a refused packet the pump
        # retries; sizing a CHUNK from one costs every chunk of the transfer,
        # silently, because the client writes what the device asked for and
        # the device then rejects its own arithmetic.
        self._mtu_observed = True

    def handle_control(self, request, t_rx=None):
        """SPEC.md §9. `[opcode][tag][params]` in, `[opcode][tag][status]
        [detail]` out. A device MUST respond to every request.

        `t_rx` is the device clock at the instant the write ARRIVED, which only
        the transport can observe. SPEC.md §9.7 requires it be taken then and
        not when the reply is composed: the gap between the two is exactly the
        processing time TIME_SYNC exists to expose, so a device reading its
        clock once and reporting it as both has quietly reverted to the
        single-timestamp form while appearing to implement the other. Defaults
        to now for callers with no better answer -- which makes the reported
        processing time an understatement, never an overstatement.
        """
        if t_rx is None:
            t_rx = self.now_us()
        if len(request) < 2:
            return None                      # not addressable: no tag to echo
        opcode, tag, params = request[0], request[1], request[2:]

        def reply(status, detail=b""):
            return bytes([opcode, tag, status]) + detail

        # SPEC.md 9 -- availability before parameters. An opcode whose owning
        # capability this device has not declared is unsupported_opcode, and a
        # malformed one is STILL unsupported_opcode rather than bad_params:
        # the two refusals mean different things to a client ("never on this
        # device" against "try better arguments"), and getting them the wrong
        # way round either loops a client forever or makes it give up on a
        # device that would have worked.
        #
        # This gate is why `capabilities` is constructor state. Without it a
        # device declaring only `control` answered ok to CAN_SUBSCRIBE,
        # GPS_SET_RATE, IMU_SET_RATE and MONITOR_LIST alike.
        needed = OPCODE_CAPABILITY.get(opcode)
        if needed is None:
            return reply(ST_UNSUPPORTED)          # not an opcode we know
        if needed and not self.capabilities & needed:
            return reply(ST_UNSUPPORTED)

        if opcode == CAN_RESET:
            # Parameterless. It used to clear the table regardless of what
            # followed the tag, so a malformed request still took effect --
            # exactly what §9.6 forbids for a request a device cannot answer,
            # applied to one it should not have answered at all.
            if params:
                return reply(ST_BAD_PARAMS)
            self._subscriptions.clear()
            # SPEC.md 8.3 -- frames already accepted into the pending batch
            # are being discarded by this reset, so they are COUNTED: if the
            # client re-subscribes, the next batch reports the loss instead
            # of the reset silently eating accepted frames.
            self._dropped["can"] = min(
                0xFFFF, self._dropped["can"] + len(self._can_pending))
            self._can_pending, self._can_batch_t0 = [], None
            # SPEC.md 15.7 -- the CAN role has one reset, and it resets
            # everything the role does: the opcode that clears the receiver
            # clears the transmitter with it. The probe result survives; it
            # is a fact about the car, not about the poll set. flush=False:
            # the pending batch was accounted for above, and arrivals not
            # yet admitted matched a table this reset just cleared.
            self._obd_stop(flush=False)
            return reply(ST_OK)

        if opcode in (CAN_SUBSCRIBE, CAN_SUBSCRIBE_MASK):
            # SPEC.md 4.1's masked_subscriptions rule is the capability gate
            # above: the schema names masked_subscriptions as the bit owning
            # CAN_SUBSCRIBE_MASK. CAN_SUBSCRIBE is owned by `can`, because it
            # is a separate opcode every CAN device implements.
            want = 7 if opcode == CAN_SUBSCRIBE else 11
            if len(params) != want:
                return reply(ST_BAD_PARAMS)
            if opcode == CAN_SUBSCRIBE:
                cid, mode, arg = struct.unpack("<IBH", params)
                mask = MASK_EXACT
            else:
                cid, mask, mode, arg = struct.unpack("<IIBH", params)
            # SPEC.md §6.8 — a mode this version does not define (2 and 3 were
            # pre-1.0 drafts') is bad_params, never silently substituted.
            if mode > SUB_PERIODIC:
                return reply(ST_BAD_PARAMS)
            cid &= CAN_MATCH_BITS
            mask &= CAN_MATCH_BITS

            # SPEC.md §9.1 — the same (id, mask) updates in place and keeps its
            # installation order, so a client reprogramming on every connect
            # cannot exhaust the table.
            sub = self._subscriptions.get((cid, mask))
            if sub is not None:
                sub.update(mode=mode, arg=arg)
                return reply(ST_OK)

            if len(self._subscriptions) >= CAN_SUBSCRIPTION_SLOTS:
                return reply(ST_TABLE_FULL)
            # SPEC.md §9.3 — no rate admission. The load a subscription adds
            # depends on what the bus carries and, for a mask, on how many
            # identifiers it matches (§6.8) — neither knowable at install. It
            # admits, and sheds what it cannot forward.
            self._install_counter += 1
            self._subscriptions[(cid, mask)] = {
                "mode": mode, "arg": arg, "order": self._install_counter,
                # SPEC.md §6.8 — mode state is per matching identifier, not per
                # subscription. A mask covering three identifiers keeps three
                # independent sets; sharing one would let whichever frame
                # arrived first consume the interval for the whole group.
                "per_id": {},
            }
            return reply(ST_OK)

        if opcode == CAN_UNSUBSCRIBE:
            # SPEC.md §9.1 — named by the same (id, mask) that installed it.
            if len(params) != 8:
                return reply(ST_BAD_PARAMS)
            cid, mask = struct.unpack("<II", params)
            key = (cid & CAN_MATCH_BITS, mask & CAN_MATCH_BITS)
            if self._subscriptions.pop(key, None) is None:
                return reply(ST_UNKNOWN_SUBSCRIPTION)
            return reply(ST_OK)

        if opcode == GPS_SET_RATE or opcode == IMU_SET_RATE:
            if len(params) != 2:
                return reply(ST_BAD_PARAMS)
            (hz,) = struct.unpack("<H", params)
            ceiling = 25 if opcode == GPS_SET_RATE else 833
            if hz > ceiling:
                return reply(ST_RATE_EXCEEDED)
            if opcode == GPS_SET_RATE:
                self.gps_hz = hz
            else:
                flushed = self._flush_imu()   # the old period no longer applies
                self.imu_hz = hz
                self._next_imu_us = self.now_us()
                if flushed:
                    self._deferred.append(("imu", flushed))
            return reply(ST_OK)

        if opcode == TIME_SYNC:
            # SPEC.md §9.5 — parameterless. It used to carry the host's UTC
            # milliseconds, which the equations could not use and this device
            # discarded.
            if params:
                return reply(ST_BAD_PARAMS)
            # SPEC.md §9.5 — two readings: when it arrived, and now. The
            # client subtracts the difference from its own round trip and is
            # left with the flight time rather than the flight time plus
            # however long this device took to think about it.
            return reply(ST_OK, enc.encode_time_sync({
                "t_device_rx": t_rx, "t_device_tx": self.now_us()}))

        if opcode == GET_POWER:
            # SPEC.md 9.7 -- parameterless, and measured when the request
            # arrives rather than sampled on a timer: the answer a client gets
            # is the reading this device had at the moment it was asked.
            if params:
                return reply(ST_BAD_PARAMS)
            return reply(ST_OK, self._power_state())

        if opcode == GNSS_AID_INFO:
            if params:
                return reply(ST_BAD_PARAMS)
            return reply(ST_OK, self._aid_caps())

        if opcode == GNSS_AID_BEGIN:
            if len(params) != 5:
                return reply(ST_BAD_PARAMS)
            fmt, total = struct.unpack("<BI", params)
            # SPEC.md §14.1 -- a format this device did not declare. The bytes
            # are opaque to the protocol, so this refusal is the only place a
            # wrong product can be caught at all; accepting it would mean a
            # 40 kB transfer the receiver silently discards.
            if fmt != self.AID_FORMAT:
                return reply(ST_BAD_PARAMS)
            # Zero is not a transfer, and SPEC.md §14.2 makes max_bytes a
            # ceiling this device refuses at rather than discovers by running
            # out of memory somewhere in the middle.
            if total == 0 or total > self.AID_MAX_BYTES_DECLARED:
                return reply(ST_BAD_PARAMS)

            chunk_bytes = self._aid_chunk_bytes()
            if chunk_bytes <= 0:
                return reply(ST_BAD_PARAMS)

            # SPEC.md §14.3 -- `index` and `first_missing` are both u16 while
            # total_bytes is u32, so a large enough transfer has chunks that
            # cannot be named. Refused here, where both numbers are first
            # known, rather than discovered at a commit the client cannot
            # express.
            if -(-total // chunk_bytes) > AID_MAX_CHUNKS:
                return reply(ST_BAD_PARAMS)

            # SPEC.md §14.3 -- one transfer open, and a BEGIN over an open one
            # discards it. The fresh token is what keeps them apart: with
            # EATT a chunk of the discarded transfer can still be queued on
            # another bearer, and it MUST fail the token check below rather
            # than land in the new transfer.
            token = self._allocate_aid_token()
            self._aid = {
                "token": token,
                "total_bytes": total,
                "chunk_bytes": chunk_bytes,
                "chunks": {},
            }
            return reply(ST_OK, enc.encode_aid_begin_result(
                {"token": token, "chunk_bytes": chunk_bytes}))

        if opcode == GNSS_AID_COMMIT:
            if len(params) != 4:
                return reply(ST_BAD_PARAMS)
            (crc,) = struct.unpack("<I", params)
            # SPEC.md §14.4 -- no transfer open. Note this is bad_params and
            # not a result: there is no transfer to report on.
            if not self._aid:
                return reply(ST_BAD_PARAMS)
            return reply(ST_OK, self._aid_commit(crc))

        if opcode == OBD_INFO:
            # SPEC.md 15.2 -- parameterless; probes afresh on every request,
            # and the response is sent only when the probe is complete: the
            # probe runs NOW (the set clears, the result replaces -- 9.6's
            # apply-then-answer order), the reply is held until `done_us`.
            if params:
                return reply(ST_BAD_PARAMS)
            done_us, detail = self._obd_probe(self.now_us())
            self._obd_pending_info = (done_us, reply(ST_OK, detail))
            return RESPONSE_PENDING

        if opcode == OBD_POLL_SET:
            # SPEC.md 15.4 -- refusals in the stated order: shape, capacity,
            # the empty-set stop, then the PIDs and the group bounds. A
            # refused request leaves the installed poll set unchanged.
            if len(params) < 3:
                return reply(ST_BAD_PARAMS)
            interval_ms, count = struct.unpack("<HB", params[:3])
            body = params[3:]
            # SPEC.md 15.4 -- shape first, capacity second. Checking capacity
            # ahead of the parse answered `table_full` to a payload that was
            # malformed as well as oversized (count 17 with an empty body),
            # naming a capacity the request never actually reached. Rule 1 is
            # the parse, and the parse of an empty body is what rejects it.
            if count == 0:
                # The stop. Accepted whatever the probe state, and its
                # interval MUST be 0 -- there is no schedule for it to pace.
                # flush=True: frames already accepted while polling are
                # delivered now rather than stranded until some later
                # subscription surfaces them with a stale t_base (SPEC.md 15.7).
                if interval_ms != 0 or body:
                    return reply(ST_BAD_PARAMS)
                self._obd_stop(flush=True)
                return reply(ST_OK)

            # SPEC.md 15.4.1 -- one pass: PID bytes until one without `more`,
            # then that group's divisor byte. The parse is its own length
            # check, which is why rule 1 is "the payload the parse does not
            # consume exactly" rather than an arithmetic on `count`: the
            # number of groups is not known until the walk finds them.
            groups, run, i = [], [], 0
            while i < len(body):
                byte = body[i]
                i += 1
                run.append(byte & ~OBD_PID_MORE)
                if byte & OBD_PID_MORE:
                    continue
                if i + 2 > len(body):
                    return reply(ST_BAD_PARAMS)   # group with no interval
                groups.append((tuple(run),
                               int.from_bytes(body[i:i + 2], "little")))
                run, i = [], i + 2
            if run:
                # SPEC.md 15.4.1 -- bit 7 left set on the last byte: a group
                # that continues into nothing is not a schedule.
                return reply(ST_BAD_PARAMS)
            if sum(len(g) for g, _ in groups) != count:
                return reply(ST_BAD_PARAMS)
            # SPEC.md 15.4 rule 2 -- the capacity was in Info, so the answer
            # is a fact the client could have read, which is 9.6's argument
            # for a typed refusal. Reached only once the shape is known good.
            if count > OBD_POLL_SLOTS:
                return reply(ST_TABLE_FULL)

            # SPEC.md 15.4 rule 4 -- pollable means declared supported by the
            # most recent probe of this connection. With no probe, nothing is,
            # which is what makes declare-verify-use structural.
            if any(not self._obd_pid_supported(pid)
                   for group, _ in groups for pid in group):
                return reply(ST_BAD_PARAMS)
            # SPEC.md 15.4 rule 5 -- seven PIDs do not fit the request frame.
            # A minimum of 0 is legal and means "every pass", so there is no
            # zero case here: unlike a divisor, a floor of zero subtracts
            # nothing rather than naming a group that never transmits.
            if any(len(group) > OBD_MAX_GROUP for group, _ in groups):
                return reply(ST_BAD_PARAMS)

            # The schedule is NOT reset: spacing is measured from the last
            # transmission (SPEC.md 15.1), so a replacement mid-interval waits
            # out the remainder instead of transmitting immediately.
            #
            # SPEC.md 15.4 -- and neither is the CURSOR or the per-group
            # timers, for any group the new schedule names again. Rebuilding
            # both from scratch made re-issuing a poll set -- ordinary client
            # behaviour, since it is the only way to change a PID -- silently
            # destructive twice over. Resetting the cursor meant a client
            # replacing its set faster than the schedule cycles never reached
            # the tail of its own list: six groups re-issued every 40 ms on a
            # 20 ms car left four of them at exactly 0 Hz, indistinguishable
            # from PIDs the car does not implement, which SPEC.md 15.4 makes
            # legal. Resetting `last` restarted every minimum interval: a
            # group asking for 0.1 Hz ran at 9.8 Hz when the set was
            # reinstalled every 100 ms, which is diagnostic traffic on a live
            # bus that the client explicitly declined.
            # Per OCCURRENCE, not per group: SPEC.md 15.4 lets a schedule
            # name the same group twice, and a dict collapsed the duplicates
            # onto one timestamp, so reinstalling an identical repeated
            # schedule moved its occurrences around -- delaying one and
            # letting another transmit sooner than its own minimum. A queue
            # per group hands the occurrences back in the order they appear,
            # which makes replacing a set with itself exactly idempotent.
            carried = {}
            for group, _m, last in self._obd_poll:
                carried.setdefault(group, []).append(last)
            fresh = []
            for g, m in groups:
                queue = carried.get(g)
                fresh.append([g, m, queue.pop(0) if queue else None])
            self._obd_poll = fresh
            self._obd_interval_ms = interval_ms
            return reply(ST_OK)

        if opcode == MONITOR_LIST:
            # SPEC.md §13.3 — parameterless: the declaration is not paged, so
            # there is no `start` to take. It used to take one, mirroring the
            # since-removed CAN_LIST, and the index could never be anything
            # but zero.
            if params:
                return reply(ST_BAD_PARAMS)
            return reply(ST_OK, self._monitor_declaration())

        return reply(ST_UNSUPPORTED)

    # -- Monitor (SPEC.md §13) --------------------------------------------

    def _monitor_declaration(self):
        """SPEC.md §13.3 — every channel this device asks for, in one response.

        No paging and no room calculation. §13.4 caps a device at 15 channels,
        which is 2 + 15*4 = 62 bytes, and the smallest response this protocol
        allows carries 97. The declaration always fits by construction.
        """
        return enc.encode_monitor_list(
            {"count": len(self._monitor_channels), "reserved": 0},
            [{"slot": s, "channel": c, "max_age": MONITOR_MAX_AGE.get(c, 20)}
             for s, c in self._monitor_channels])

    # -- aiding (SPEC.md §14) ---------------------------------------------

    def _allocate_aid_token(self):
        """A token that is not the one just discarded.

        SPEC.md §14.3. Reusing it would let a chunk still queued on another
        ATT bearer for the abandoned transfer be accepted into the new one,
        at whatever offset its index names -- silent corruption of a payload
        whose CRC is computed by somebody else.
        """
        previous = self._aid["token"] if self._aid else self._aid_last_token
        token = 1 if previous is None else (previous % 255) + 1
        self._aid_last_token = token
        return token

    def _aid_caps(self):
        """SPEC.md §14.2 — what this device accepts, and what it already holds."""
        held = self._aid_held_until
        return enc.encode_gnss_aid_caps({
            "validity": AID_V_HELD_UNTIL if held is not None else 0,
            "format": self.AID_FORMAT,
            "max_bytes": self.AID_MAX_BYTES_DECLARED,
            "held_until": held or 0,
        })

    def _aid_chunk_bytes(self):
        """SPEC.md §14.3 — the payload size every chunk but the last carries.

        `self.mtu` is only the negotiated value once a backend has observed it.
        Bless learns it on CoreBluetooth and nowhere else, so on BlueZ and
        WinRT this device is holding whatever `--mtu` said -- 247 by default.
        Sizing chunks from that on a link that negotiated 185 asks the client
        for 241-byte writes it cannot make, and the 179-byte ones it can make
        are then rejected by this device as the wrong length: every chunk of
        the transfer discarded, with the only symptom a commit that reports
        everything missing.

        So when the MTU has not been observed, chunks are sized from the
        minimum ATT MTU this protocol requires (§2.1). That is smaller than
        necessary on a link that negotiated more, and it is writable on every
        conforming link, which is the correct way round for a number the client
        cannot second-guess.
        """
        mtu = self.mtu if self._mtu_observed else MIN_ATT_MTU
        return mtu - AID_CHUNK_OVERHEAD

    def _aid_expected_chunks(self, transfer):
        return -(-transfer["total_bytes"] // transfer["chunk_bytes"])

    def handle_aiding_write(self, payload):
        """SPEC.md §14.3 — one chunk, written without a response.

        Returns a reason string for the caller's log and nothing else: there is
        no response path, and every rule below is one a client cannot break
        without having ignored the GNSS_AID_BEGIN that answered it. Silence is
        the specified behaviour, not an omission.
        """
        if not self.capabilities & CAP_GNSS_AIDING:
            return "aiding-not-supported"
        if len(payload) < 3:
            return "length"
        token, index = struct.unpack_from("<BH", payload, 0)
        body = payload[3:]

        if not self._aid:
            return "no-open-transfer"
        t = self._aid
        # SPEC.md §14.3 -- a stale chunk for a discarded transfer. EATT means
        # it can arrive after the BEGIN that discarded its transfer, so this
        # check is what keeps it out of the new one.
        if token != t["token"]:
            return "wrong-token"

        expected = self._aid_expected_chunks(t)
        if index >= expected:
            return "index-beyond-transfer"

        # SPEC.md §14.3 -- every chunk but the last carries exactly
        # chunk_bytes. The device knows both numbers from its own BEGIN, so a
        # short chunk is detectable here rather than at the CRC, where it would
        # be indistinguishable from corruption and cost the whole transfer.
        last = index == expected - 1
        want = (t["total_bytes"] - index * t["chunk_bytes"]) if last \
            else t["chunk_bytes"]
        if len(body) != want:
            return "wrong-chunk-length"

        # A repeat is explicitly allowed: it is how a client fills a gap
        # SPEC.md §14.4 told it about.
        t["chunks"][index] = bytes(body)
        return None

    def _aid_commit(self, crc):
        """SPEC.md §14.4 — what became of the transfer.

        The status of the RESPONSE is ok throughout: a transfer was open and
        the request was well formed, so the device applied it. What it found
        is in the result, which is the only place an index to resend from can
        travel (§14.5).
        """
        t = self._aid
        expected = self._aid_expected_chunks(t)

        def result(value, first_missing=None):
            return enc.encode_aid_commit_result({
                "validity": COMMIT_V_FIRST_MISSING if first_missing is not None
                            else 0,
                "result": value,
                "first_missing": first_missing or 0,
            })

        missing = [i for i in range(expected) if i not in t["chunks"]]
        if missing:
            # SPEC.md §14.4 -- the LOWEST index, and the transfer stays open so
            # the client can resend just the gap. That is the whole reason a
            # write-without-response path is safe to use for this. The index is
            # always one the device genuinely does not hold, so a client
            # resending from it makes progress on every round.
            return result(AID_RESULT_INCOMPLETE, missing[0])

        data = b"".join(t["chunks"][i] for i in range(expected))
        # SPEC.md §14.4 -- CRC-32 over the reassembled payload, not the chunks.
        # zlib.crc32 is the IEEE 802.3 polynomial, reflected, which is what the
        # specification names exactly so that two implementations agree.
        if zlib.crc32(data) != crc:
            self._aid = None
            return result(AID_RESULT_BAD_CRC)

        # SPEC.md §14.6 -- applied whole, at commit, and to the RECEIVER. A
        # real device writes these bytes out of the UART here and nowhere else;
        # in particular nothing from `data` reaches a gps_fix.
        self._aid = None
        applied = self.apply_aiding(data)
        if not applied:
            return result(AID_RESULT_REJECTED)
        self._aid_applied += 1
        return result(AID_RESULT_APPLIED)

    @property
    def aid_transfers_applied(self):
        """Transfers handed to the receiver since this device started."""
        return self._aid_applied

    def apply_aiding(self, data):
        """Hand a completed transfer to the receiver. Overridable.

        This synthetic device has no receiver, so it accepts anything and
        records the fact. A real one writes `data` to the GNSS module and
        answers on what the module said -- u-blox returns UBX-MGA-ACK, and a
        NAK is what SPEC.md §14.4's `rejected` exists to carry: the bytes
        arrived intact and this receiver would not take them.
        """
        return True

    def handle_monitor_write(self, payload):
        """SPEC.md §13.4 — a client-to-device batch of values.

        Returns None on success, or a reason string. A device rejects a
        malformed write for the same reason a client rejects a malformed
        notification: a partly-applied update is a display showing a mixture of
        two moments.
        """
        # SPEC.md 4.1 -- monitor_values is inert without the bit: the write is
        # rejected and changes nothing. A device that quietly accepted values
        # for a role it does not have would then display them.
        if not self.capabilities & CAP_MONITOR:
            return "monitor-not-supported"
        hsize, vsize = 4, 6
        if len(payload) < hsize:
            return "length"
        seq, count = struct.unpack_from("<HB", payload, 0)
        if len(payload) != hsize + count * vsize:
            return "length"
        # SPEC.md §13.4 — a write naming no slots is not a complete statement.
        # This device already rejected it (the completeness check below sees a
        # subset of size zero); saying so explicitly is what makes the reason
        # match the rule rather than arriving as "incomplete".
        if count == 0:
            return "empty-update"

        known = {slot for slot, _ in self._monitor_channels}
        staged, seen = {}, set()
        now = self.now_us()
        for i in range(count):
            slot, validity, value = struct.unpack_from(
                "<BBi", payload, hsize + i * vsize)
            # SPEC.md §13.4 — a slot twice in one write, and nothing says which
            # wins. Rejected whole rather than resolved arbitrarily.
            if slot in seen:
                return "duplicate-slot"
            seen.add(slot)
            # SPEC.md §13.1 — a slot this device never asked for is ignored,
            # not an error: the client may be a version ahead.
            if slot not in known:
                continue
            present = bool(validity & MONITOR_PRESENT)
            staged[slot] = (value if present else 0, present, now)

        # SPEC.md §13.4 — every write carries every slot the device asked for.
        # Merging a subset was the whole failure the snapshot rule exists to
        # prevent: an omitted slot kept its previous value AND its previous
        # timestamp, so it stayed on screen looking current while the client
        # had stopped saying anything about it.
        missing = known - set(staged)
        if missing:
            return f"incomplete: {len(missing)} slot(s) not carried"

        self._monitor_values.update(staged)
        self._monitor_seq = seq
        self._monitor_updates += 1
        return None

    def can_table(self):
        """The installed CAN subscriptions, in installation order.

        Exposed for the debug panel: the difference between "three ids
        installed" and "three ids installed and the client is listening" is
        most of the diagnostic work in this protocol, and neither number means
        much without the other.
        """
        return [(cid, mask, s["mode"], s["arg"])
                for (cid, mask), s in sorted(self._subscriptions.items(),
                                             key=lambda ks: ks[1]["order"])]

    def pending_dropped(self):
        """Discards accumulated but not yet reported on a notification."""
        return dict(self._dropped)

    def rates(self):
        """The rates this device is configured to produce, for display."""
        return {"gps": self.gps_hz, "imu": self.imu_hz}

    def monitor_state(self):
        """(slot, channel, value, present) for every channel this device asked
        for. Structured rather than formatted: rendering is display.py's job,
        and it must be testable without a screen.

        SPEC.md §13.5 — a value older than its channel's max_age is reported
        NOT PRESENT, exactly as one whose present bit was clear. A client that
        crashed, was backgrounded or wedged leaves the link up and simply stops
        writing, so silence is the only symptom the device ever sees; without
        this the screen shows a lap time from four minutes ago and the driver
        reading it has no way to tell.

        One rule, applied per channel. There used to be a second — a derived
        device-wide "liveness bound", the largest max_age declared, which
        expired the channels that declared none — and the two together were
        what nobody could keep straight. Every channel carries a deadline now,
        so the bound has nothing left to catch.
        """
        now = self.now_us()
        out = []
        for slot, channel in self._monitor_channels:
            value, present, written_at = self._monitor_values.get(
                slot, (0, False, None))
            max_age = MONITOR_MAX_AGE.get(channel, 20)
            if (present and written_at is not None
                    and now - written_at > max_age * 100_000):
                value, present = 0, False
            out.append((slot, channel, value, present))
        return out

    @property
    def monitor_seq(self):
        return self._monitor_seq

    @property
    def monitor_updates(self):
        return self._monitor_updates

    def display_lines(self):
        """A plain-text rendering, for logs. Absence renders as absence, never
        as a number nobody supplied."""
        from display import render_lines
        return render_lines(self.monitor_state())

    def set_power(self, source=None, percent=None):
        """What the device's own supply monitoring found.

        Both arguments are independent, and None means "this build cannot
        measure it" rather than zero -- which is the whole shape of SPEC.md 9.7.
        A device wired to the ignition feed passes `percent=None` forever and
        reports `external` truthfully, rather than reporting 100% and leaving a
        client to render a gauge for a battery that does not exist. A device
        that is plugged in AND has a pack passes both -- `external` claims
        nothing about a battery, so `set_power(SRC_EXTERNAL, 40)` is an
        ordinary state and not a contradiction.

        SPEC.md 9.7's one device rule is refused HERE, where the mistake is,
        rather than at the next GET_POWER: by then the device is running and
        the traceback names a control response rather than the call that made
        it wrong.
        """
        if (self.capabilities & CAP_POWER
                and source is None and percent is None):
            raise ValueError(
                "a device declaring `power` MUST report at least one valid "
                "field (SPEC.md 9.7); with nothing to say it declares no "
                "`power` capability instead")
        self._power = {"source": source, "percent": percent}

    def _power_state(self):
        """SPEC.md 9.7 -- the detail of a GET_POWER response.

        Presence, not truthiness: a percent of 0 is a flat battery and a real
        measurement, and reading it as "unknown" would hide the one state a
        driver most needs to see.
        """
        fields, validity = {"source": 0, "percent": 0}, 0
        for bit, name in ((PWR_SOURCE, "source"), (PWR_PERCENT, "percent")):
            if self._power.get(name) is not None:
                validity |= bit
                fields[name] = self._power[name]
        return enc.encode_power_state({"validity": validity, **fields})
