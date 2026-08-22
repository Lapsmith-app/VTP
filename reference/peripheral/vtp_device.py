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
CAP_MASKED_SUBS, CAP_ONCHANGE_SUBS = 1 << 6, 1 << 7

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
CAN_UNSUBSCRIBE, CAN_LIST = 0x04, 0x05
GPS_SET_RATE, IMU_SET_RATE, TIME_SYNC = 0x10, 0x20, 0x30
GET_LINK_PARAMS, MONITOR_LIST = 0x31, 0x40

# SPEC.md §13.2 — channels a Monitor device may ask the client for.
CH_LAP_TIME, CH_LAST_LAP_TIME, CH_BEST_LAP_TIME = 1, 2, 3
CH_DELTA_BEST, CH_PREDICTED_LAP_TIME, CH_LAP_NUMBER = 4, 5, 6
CH_SPEED, CH_SESSION_DISTANCE, CH_SESSION_TIME = 7, 8, 9

MONITOR_PRESENT = 0x01

PROTOCOL_MAJOR, PROTOCOL_MINOR = 1, 0
# SPEC.md §2 — read from the schema rather than restated, so the one place it
# is defined stays the only place it is defined.
MIN_ATT_MTU = enc.SCHEMA["protocol"]["min_att_mtu"]

ST_OK, ST_UNSUPPORTED, ST_BAD_PARAMS = 0, 1, 2
ST_TABLE_FULL, ST_RATE_EXCEEDED = 3, 4
ST_UNKNOWN_HANDLE = 7

# SPEC.md §9.2 — matching runs over bits 0-29: the arbitration identifier and
# the standard/extended format bit. Bits 30 and 31 say how a frame was
# transmitted, not which frame it is, and take no part. CAN_SUBSCRIBE is
# CAN_SUBSCRIBE_MASK with every one of those bits set.
CAN_MATCH_BITS = 0x3FFFFFFF
MASK_EXACT = 0x3FFFFFFF

SUB_EVERY_FRAME, SUB_PERIODIC, SUB_ON_CHANGE, SUB_EVERY_NTH = 0, 1, 2, 3

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

    def __init__(self, *, now_us=None, mtu=247, gps_hz=10, imu_hz=100,
                 circuit=None, monitor_channels=None):
        self._clock = now_us or self._monotonic_us
        self._origin_ns = time.monotonic_ns()
        self._wall_origin_ms = int(time.time() * 1000)

        self.mtu = mtu
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

        # handle -> {id, mask, mode, arg, last, seen}. Empty until a client
        # subscribes: SPEC.md §9.2 makes the cleared table the state after a
        # reconnect, and a device that streamed before being asked would be
        # inventing consent.
        self._subscriptions = {}
        self._next_handle = 1

        # SPEC.md §13 — this device has a display, so it asks the client for
        # what it cannot compute. The declaration is fixed for the connection.
        self._monitor_channels = list(enumerate(
            monitor_channels if monitor_channels is not None else
            (CH_LAP_TIME, CH_LAST_LAP_TIME, CH_BEST_LAP_TIME,
             CH_DELTA_BEST, CH_LAP_NUMBER, CH_SPEED)))
        # slot -> (value, present). Absent is a state the display renders, not
        # a value it substitutes.
        self._monitor_values = {}
        self._monitor_seq = None
        self._monitor_updates = 0
        self._link = None

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
        self._return_seq(stream)
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

    def simulate_loss(self, stream, count):
        """Pretend the device accepted `count` items and had to discard them.

        Not decoration: a client has to handle loss, and loss on a desktop
        peripheral otherwise never happens, so the path that reads `dropped`
        would go untested until a real device on a real track produced some.
        """
        if stream not in self._dropped:
            raise ValueError(f"unknown stream {stream!r}")
        self._dropped[stream] += count

    def _allocate_handle(self):
        """SPEC.md §9.2 — a handle MUST NOT be reused while the subscription
        it names still exists.

        The counter wraps at 65535, and wrapping onto a live handle used to
        overwrite it: the entry the client knew as handle 1 silently became a
        different subscription, so CAN_UNSUBSCRIBE(1) removed something the
        client had never installed. Reaching the wrap takes 65534 installs,
        which a long session can do and a short test never will.

        Skipping occupied handles terminates because the table holds at most
        CAN_SUBSCRIPTION_SLOTS entries out of 65535 numbers, so a free one
        always exists when a free slot does. The bound is belt and braces.
        """
        for _ in range(0xFFFF):
            handle = self._next_handle
            self._next_handle = (self._next_handle % 0xFFFF) + 1
            if handle not in self._subscriptions:
                return handle
        return None

    def _next_seq(self, stream):
        """SPEC.md §8.2 — the FIRST notification after a connection carries 0.

        Post-increment, not pre-increment. Returning the incremented value
        made the first notification of every connection seq 1, so a client
        counting from 0 saw a one-notification gap before anything had been
        lost -- on every stream, on every connection.
        """
        n = self._seq[stream]
        self._seq[stream] = (n + 1) & 0xFFFF
        return n

    def _return_seq(self, stream):
        """Give back a sequence number whose notification was never sent.

        SPEC.md §8.2 -- seq counts notifications *sent*. A notification the
        transport refused was not sent, so consuming its number would leave a
        gap, and §8.2 defines a gap as notifications the client did not
        receive in transit. The loss is real but it happened inside the
        device, which is what `dropped` is for; reporting it twice, in two
        fields that mean different things, tells a client less than reporting
        it once in the right one.
        """
        self._seq[stream] = (self._seq[stream] - 1) & 0xFFFF

    def _take_dropped(self, stream):
        """SPEC.md §8.3 — saturates at 65535 and MUST NOT wrap."""
        n = min(self._dropped[stream], 0xFFFF)
        self._dropped[stream] = 0
        return n

    @property
    def notify_bytes(self):
        """ATT payload available for one notification: MTU minus the 3-byte
        ATT notification header."""
        return self.mtu - 3

    # -- Info -------------------------------------------------------------

    def info(self):
        return enc.encode_info({
            "protocol_major": PROTOCOL_MAJOR,
            "protocol_minor": PROTOCOL_MINOR,
            "capabilities": (CAP_GPS | CAP_CAN | CAP_IMU | CAP_CONTROL
                             | CAP_MONITOR | CAP_MASKED_SUBS
                             | CAP_ONCHANGE_SUBS),
            "gps_rate_hz": self.gps_hz,
            "gps_max_rate_hz": 25,
            "can_subscription_slots": CAN_SUBSCRIPTION_SLOTS,
            "can_max_frames_per_s": CAN_MAX_FRAMES_PER_S,
            "imu_rate_hz": self.imu_hz,
            "imu_max_rate_hz": 833,
            "can_max_payload": 8,
            "clock_flags": 0b10,      # survives reconnect; not GNSS-disciplined
            "max_notify_bytes": self.notify_bytes,
        })

    # -- GPS --------------------------------------------------------------

    def _gps_fix(self, now):
        st = self.circuit.at(now / 1e6)

        validity = (V_T_UTC | V_T_UTC_RESOLVED | V_POSITION | V_ALT_MSL
                    | V_VELOCITY | V_HEAD_MOT | V_H_ACC | V_V_ACC | V_S_ACC
                    | V_P_DOP | V_NUM_SV)
        return enc.encode_gps_fix({
            "seq": self._next_seq("gps"),
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
            "az": 1000,                       # 1 g down, level car
            "gx": 0, "gy": 0,
            "gz": round(st["yaw_rate"] / 0.05),
        }

    def _flush_imu(self):
        if not self._imu_pending:
            return None
        payload = enc.encode_imu_batch({
            "seq": self._next_seq("imu"),
            "dropped": self._take_dropped("imu"),
            "t_base": self._imu_batch_t0,
            "period": self._imu_period_us,
            "count": len(self._imu_pending),
            "flags": IMU_ACCEL | IMU_GYRO,
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
        header = {
            "seq": self._next_seq("can"),
            "dropped": self._take_dropped("can"),
            "t_base": self._can_batch_t0 if self._can_pending else now,
            "count": len(self._can_pending),
            "flags": 0,
            "reserved": 0,
        }
        payload = enc.encode_can_batch(header, self._can_pending)
        self._can_pending, self._can_batch_t0 = [], None
        return payload

    # -- polling ----------------------------------------------------------

    def poll(self):
        """Notifications due now, as (characteristic, payload) pairs."""
        now = self.now_us()
        out, self._deferred = self._deferred, []

        if self.gps_hz and now >= self._next_gps_us:
            out.append(("gps", self._gps_fix(now)))
            self._next_gps_us = now + round(1_000_000 / self.gps_hz)

        if self.imu_hz:
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

        for frame in self._due_can_frames(now):
            if self._can_batch_t0 is None:
                self._can_batch_t0 = frame["_t"]
            # SPEC.md §6.1 — dt is 10 us ticks from t_base and spans 655.35 ms,
            # so a batch MUST be flushed before it would overflow.
            dt = (frame["_t"] - self._can_batch_t0) // 10
            if dt > 0xFFFF or len(self._can_pending) >= self._can_capacity():
                out.append(("can", self._flush_can(now)))
                self._can_batch_t0 = frame["_t"]
                dt = 0
            self._can_pending.append({
                "dt": dt, "id": frame["id"], "extended": False,
                "fd": False, "rtr": False,
                "len": len(frame["payload"]), "payload": frame["payload"],
            })

        # Flush partial batches on a timer so a quiet bus or a slow ODR still
        # delivers, rather than waiting for a batch that may never fill.
        if self._subscriptions and now >= self._next_can_flush_us:
            out.append(("can", self._flush_can(now)))
            self._next_can_flush_us = now + 100_000
        return [(c, p) for c, p in out if p is not None]

    def _governing(self, cid):
        """SPEC.md §9.3 — of the subscriptions matching `cid`, the one that
        governs: most specific mask first, then lowest handle. A frame is
        forwarded at most once, whatever else matches it."""
        matches = [(h, s) for h, s in self._subscriptions.items()
                   if (cid & s["mask"]) == (s["id"] & s["mask"])]
        if not matches:
            return None, None
        return min(matches, key=lambda hs: (-bin(hs[1]["mask"]).count("1"), hs[0]))

    def _due_can_frames(self, now):
        for cid, rate_hz, payload in self._bus_frames(now):
            handle, sub = self._governing(cid)
            if sub is None:
                continue
            # SPEC.md §6.8 — one set of mode state per matching identifier.
            st = sub["per_id"].setdefault(
                cid, {"last": 0, "seen": 0, "emitted_at": 0,
                      "last_payload": None})
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
            elif sub["mode"] == SUB_EVERY_NTH and sub["arg"]:
                emit = ((st["seen"] - 1) % sub["arg"]) == 0
            elif sub["mode"] == SUB_ON_CHANGE and not first:
                emit = payload != st["last_payload"]
                if emit and sub["arg"]:
                    emit = (now - st["emitted_at"]) >= sub["arg"] * 1000
            st["last"] = now
            if emit:
                st["emitted_at"] = now
                st["last_payload"] = payload
                yield {"id": cid, "payload": payload, "_t": now}

    # -- Control ----------------------------------------------------------

    def set_link_params(self, **kwargs):
        """Called by the transport as it learns each part of the link.

        Merged rather than replaced: a host stack exposes these one at a time
        and from different callbacks, and a later report of the PHY must not
        erase an earlier report of the MTU.
        """
        self._link = dict(self._link or {}, **kwargs)

    def set_negotiated_mtu(self, att_mtu):
        """The real ATT MTU, as opposed to the one this device assumed.

        Batch sizing had been driven entirely by the --mtu argument, so a
        device told 247 while the link negotiated 185 built notifications the
        link could not carry -- refused by the stack, or truncated, depending
        on how forgiving it is. SPEC.md §9.1 also requires GET_LINK_PARAMS to
        report the negotiated value or none at all, and a value taken from a
        command-line flag is neither.
        """
        self.mtu = att_mtu
        self.set_link_params(att_mtu=att_mtu)

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

        if opcode == CAN_RESET:
            self._subscriptions.clear()
            self._can_pending, self._can_batch_t0 = [], None
            return reply(ST_OK)

        if opcode in (CAN_SUBSCRIBE, CAN_SUBSCRIBE_MASK):
            want = 7 if opcode == CAN_SUBSCRIBE else 11
            if len(params) != want:
                return reply(ST_BAD_PARAMS)
            if opcode == CAN_SUBSCRIBE:
                cid, mode, arg = struct.unpack("<IBH", params)
                mask = MASK_EXACT
            else:
                cid, mask, mode, arg = struct.unpack("<IIBH", params)
            if mode > SUB_EVERY_NTH:
                return reply(ST_BAD_PARAMS)
            # SPEC.md §6.8 — N of 0 selects no frames at all and is meaningless.
            if mode == SUB_EVERY_NTH and arg == 0:
                return reply(ST_BAD_PARAMS)
            cid &= CAN_MATCH_BITS
            mask &= CAN_MATCH_BITS

            # SPEC.md §9.2 — the same (id, mask) updates in place and keeps its
            # handle, so a client reprogramming on every connect cannot exhaust
            # the table.
            for h, s in self._subscriptions.items():
                if s["id"] == cid and s["mask"] == mask:
                    s.update(mode=mode, arg=arg)
                    return reply(ST_OK, struct.pack("<H", h))

            if len(self._subscriptions) >= CAN_SUBSCRIPTION_SLOTS:
                return reply(ST_TABLE_FULL)
            # SPEC.md §9.4 — admission is only decidable where the subscription
            # itself bounds the rate. every_frame and on_change are admitted and
            # shed if they overrun; refusing them would be a prediction about
            # bus traffic the device cannot make.
            if mode in (SUB_PERIODIC, SUB_EVERY_NTH) and arg:
                if self._predicted_rate(mode, arg) > CAN_MAX_FRAMES_PER_S:
                    return reply(ST_RATE_EXCEEDED)

            handle = self._allocate_handle()
            if handle is None:
                return reply(ST_TABLE_FULL)
            self._subscriptions[handle] = {
                "id": cid, "mask": mask, "mode": mode, "arg": arg,
                # SPEC.md §6.8 — mode state is per matching identifier, not per
                # subscription. A mask covering three identifiers keeps three
                # independent sets; sharing one would let whichever frame
                # arrived first consume the interval for the whole group.
                "per_id": {},
            }
            return reply(ST_OK, struct.pack("<H", handle))

        if opcode == CAN_UNSUBSCRIBE:
            if len(params) != 2:
                return reply(ST_BAD_PARAMS)
            (handle,) = struct.unpack("<H", params)
            if self._subscriptions.pop(handle, None) is None:
                return reply(ST_UNKNOWN_HANDLE)
            return reply(ST_OK)

        if opcode == CAN_LIST:
            if len(params) != 2:
                return reply(ST_BAD_PARAMS)
            (start,) = struct.unpack("<H", params)
            return reply(ST_OK, self._list_page(start))

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
            if len(params) != 8:
                return reply(ST_BAD_PARAMS)
            # SPEC.md §9.7 — two readings: when it arrived, and now. The
            # client subtracts the difference from its own round trip and is
            # left with the flight time rather than the flight time plus
            # however long this device took to think about it.
            return reply(ST_OK, enc.encode_time_sync({
                "t_device_rx": t_rx, "t_device_tx": self.now_us()}))

        if opcode == GET_LINK_PARAMS:
            return reply(ST_OK, self._link_params())

        if opcode == MONITOR_LIST:
            if len(params) != 2:
                return reply(ST_BAD_PARAMS)
            (start,) = struct.unpack("<H", params)
            return reply(ST_OK, self._monitor_page(start))

        return reply(ST_UNSUPPORTED)

    def _predicted_rate(self, mode, arg):
        """Frames per second the installed table would produce, plus a
        candidate. Only rate-bounded modes are counted, because only they can
        be predicted at all (SPEC.md §9.4)."""
        def rate(m, a):
            if m == SUB_PERIODIC and a:
                return 1000 / a
            if m == SUB_EVERY_NTH and a:
                return 100 / a          # against a nominal 100 Hz signal
            return 0
        total = sum(rate(s["mode"], s["arg"]) for s in self._subscriptions.values())
        return total + rate(mode, arg)

    def _list_page(self, start):
        """SPEC.md §9.5. One page from `start`, sized to the notification
        budget. A start beyond the end is ok with count 0, not an error."""
        table = sorted(self._subscriptions.items())
        # 3 bytes of opcode/tag/status, then the page header, then entries.
        room = (self.notify_bytes - 3 - 6) // 13
        page = table[start:start + max(0, room)]
        header = {"total": len(table), "index": start,
                  "count": len(page), "reserved": 0}
        entries = [{"handle": h, "id": s["id"], "mask": s["mask"],
                    "mode": s["mode"], "arg": s["arg"]} for h, s in page]
        return enc.encode_can_list(header, entries)

    # -- Monitor (SPEC.md §13) --------------------------------------------

    def _monitor_page(self, start):
        room = (self.notify_bytes - 3 - 6) // 4
        page = self._monitor_channels[start:start + max(0, room)]
        return enc.encode_monitor_list(
            {"total": len(self._monitor_channels), "index": start,
             "count": len(page), "reserved": 0},
            [{"slot": s, "channel": c, "reserved": 0} for s, c in page])

    def handle_monitor_write(self, payload):
        """SPEC.md §13.4 — a client-to-device batch of values.

        Returns None on success, or a reason string. A device rejects a
        malformed write for the same reason a client rejects a malformed
        notification: a partly-applied update is a display showing a mixture of
        two moments.
        """
        hsize, vsize = 4, 6
        if len(payload) < hsize:
            return "length"
        seq, count = struct.unpack_from("<HB", payload, 0)
        if len(payload) != hsize + count * vsize:
            return "length"

        known = {slot for slot, _ in self._monitor_channels}
        staged = {}
        for i in range(count):
            slot, validity, value = struct.unpack_from(
                "<BBi", payload, hsize + i * vsize)
            # SPEC.md §13.1 — a slot this device never asked for is ignored,
            # not an error: the client may be a version ahead.
            if slot not in known:
                continue
            present = bool(validity & MONITOR_PRESENT)
            staged[slot] = (value if present else 0, present)

        self._monitor_values.update(staged)
        self._monitor_seq = seq
        self._monitor_updates += 1
        return None

    def can_table(self):
        """The installed CAN subscriptions, as CAN_LIST would report them.

        Exposed for the debug panel: the difference between "three ids
        installed" and "three ids installed and the client is listening" is
        most of the diagnostic work in this protocol, and neither number means
        much without the other.
        """
        return [(handle, s["id"], s["mask"], s["mode"], s["arg"])
                for handle, s in sorted(self._subscriptions.items())]

    def pending_dropped(self):
        """Discards accumulated but not yet reported on a notification."""
        return dict(self._dropped)

    def rates(self):
        """The rates this device is configured to produce, for display."""
        return {"gps": self.gps_hz, "imu": self.imu_hz}

    def monitor_state(self):
        """(slot, channel, value, present) for every channel this device asked
        for. Structured rather than formatted: rendering is display.py's job,
        and it must be testable without a screen."""
        return [(slot, channel, *self._monitor_values.get(slot, (0, False)))
                for slot, channel in self._monitor_channels]

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

    def _link_params(self):
        link = self._link or {}
        validity, fields = 0, {
            "att_mtu": 0, "ll_max_tx_octets": 0, "ll_max_rx_octets": 0,
            "conn_interval": 0, "peripheral_latency": 0,
            "supervision_timeout": 0, "phy_tx": 0, "phy_rx": 0,
        }
        if link.get("att_mtu"):
            validity |= 1 << 0
            fields["att_mtu"] = link["att_mtu"]
        if link.get("ll_max_tx_octets"):
            validity |= 1 << 1
            fields["ll_max_tx_octets"] = link["ll_max_tx_octets"]
            fields["ll_max_rx_octets"] = link.get("ll_max_rx_octets", 0)
        if link.get("conn_interval"):
            validity |= 1 << 2
            fields["conn_interval"] = link["conn_interval"]
            fields["peripheral_latency"] = link.get("peripheral_latency", 0)
            fields["supervision_timeout"] = link.get("supervision_timeout", 0)
        if link.get("phy_tx"):
            validity |= 1 << 3
            fields["phy_tx"] = link["phy_tx"]
            fields["phy_rx"] = link.get("phy_rx", link["phy_tx"])
        # Everything a host stack does not expose stays absent rather than
        # being guessed — SPEC.md §9.1 is explicit that a cleared bit is the
        # only honest answer, and a desktop CoreBluetooth or BlueZ peripheral
        # genuinely cannot see most of this.
        return enc.encode_link_params({"validity": validity, **fields})
