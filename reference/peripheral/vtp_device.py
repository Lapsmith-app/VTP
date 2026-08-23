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
import math
import pathlib
import struct
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "reference" / "python"))

import vtp1_encode as enc  # noqa: E402

# ---------------------------------------------------------------------------
# Capability and layout constants
# ---------------------------------------------------------------------------

CAP_GPS, CAP_CAN, CAP_IMU = 1 << 0, 1 << 1, 1 << 2
CAP_MONITOR = 1 << 3
CAP_CONTROL, CAP_CAN_FD = 1 << 4, 1 << 5
CAP_MASKED_SUBS = 1 << 6

V_T_UTC, V_T_UTC_RESOLVED, V_POSITION = 1 << 0, 1 << 1, 1 << 2
V_ALT_MSL, V_ALT_ELLIPSOID, V_VELOCITY = 1 << 3, 1 << 4, 1 << 5
V_HEAD_MOT, V_H_ACC, V_V_ACC = 1 << 6, 1 << 7, 1 << 8
V_S_ACC, V_P_DOP, V_NUM_SV = 1 << 9, 1 << 10, 1 << 11

IMU_ACCEL, IMU_GYRO = 0x01, 0x02
FIX_3D = 3
# SPEC.md §5.6 — fix_flags bit 4.
FIX_FLAG_SOLUTION_EPOCH = 1 << 4

# Control opcodes (SPEC.md §9).
CAN_RESET, CAN_SUBSCRIBE, CAN_SUBSCRIBE_MASK = 0x01, 0x02, 0x03
CAN_UNSUBSCRIBE = 0x04
GPS_SET_RATE, IMU_SET_RATE, TIME_SYNC = 0x10, 0x20, 0x30
MONITOR_LIST = 0x40

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
                            | CAP_MONITOR | CAP_MASKED_SUBS)

    def __init__(self, *, now_us=None, mtu=247, gps_hz=10, imu_hz=100,
                 circuit=None, monitor_channels=None, capabilities=None):
        self._clock = now_us or self._monotonic_us
        self._origin_ns = time.monotonic_ns()
        self._wall_origin_ms = int(time.time() * 1000)

        self.capabilities = (self.DEFAULT_CAPABILITIES if capabilities is None
                             else capabilities)
        self.mtu = mtu
        # The largest MTU this build will ever accept. `self.mtu` moves with
        # the link; this does not, and batches are never sized above it.
        self._device_mtu_ceiling = mtu
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
            "count": len(self._can_pending),
            "flags": 0,
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

        # The CAN branch is already gated by `_subscriptions`, which stays
        # empty on a device whose CAN opcodes all answer unsupported_opcode --
        # but relying on a side effect of the control plane to enforce a
        # capability is how the rule stops holding the moment either changes.
        for frame in (self._due_can_frames(now) if caps & CAP_CAN else ()):
            if self._can_batch_t0 is None:
                self._can_batch_t0 = frame["_t"]
            # SPEC.md §6.1 — dt is 10 us ticks from t_base and spans 655.35 ms,
            # so a batch MUST be flushed before it would overflow.
            dt = (frame["_t"] - self._can_batch_t0) // 10
            if dt > 0xFFFF or len(self._can_pending) >= self._can_capacity():
                batch = self._flush_can(now)
                if batch is not None:
                    out.append(("can", batch))
                self._can_batch_t0 = frame["_t"]
                dt = 0
            self._can_pending.append({
                "dt": dt, "id": frame["id"], "extended": False,
                "fd": False, "rtr": False,
                "len": len(frame["payload"]), "payload": frame["payload"],
            })

        # Flush partial batches on a timer so a quiet bus or a slow ODR still
        # delivers, rather than waiting for a batch that may never fill.
        if (caps & CAP_CAN and self._subscriptions
                and "can" not in undelivered
                and now >= self._next_can_flush_us):
            batch = self._flush_can(now)
            if batch is not None:
                out.append(("can", batch))
            self._next_can_flush_us = now + 100_000
        return [(c, p) for c, p in out if p is not None]

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
            # SPEC.md §6.8 — one set of mode state per matching identifier.
            st = sub["per_id"].setdefault(
                cid, {"last": 0, "seen": 0, "emitted_at": 0})
            interval = round(1_000_000 / rate_hz)
            if now - st["last"] < interval:
                continue
            st["seen"] += 1
            # SPEC.md §6.8 — the first matching frame is forwarded in every
            # mode. A client that installs a subscription and waits for a value
            # to display should not have to wait for a second frame.
            first = st["seen"] == 1
            emit = True
            if sub["mode"] == SUB_PERIODIC and sub["arg"] and not first:
                emit = (now - st["emitted_at"]) >= sub["arg"] * 1000
            st["last"] = now
            if emit:
                st["emitted_at"] = now
                yield {"id": cid, "payload": payload, "_t": now}

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
            self._can_pending, self._can_batch_t0 = [], None
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
