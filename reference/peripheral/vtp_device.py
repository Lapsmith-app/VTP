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

ST_OK, ST_UNSUPPORTED, ST_BAD_PARAMS = 0, 1, 2
ST_TABLE_FULL, ST_RATE_EXCEEDED = 3, 4
ST_UNKNOWN_HANDLE = 7

# SPEC.md §9.2 — CAN_SUBSCRIBE is CAN_SUBSCRIBE_MASK with every id bit set.
MASK_EXACT = 0x1FFFFFFF
CAN_ID_BITS = 0x1FFFFFFF

SUB_EVERY_FRAME, SUB_PERIODIC, SUB_ON_CHANGE, SUB_EVERY_NTH = 0, 1, 2, 3

CAN_SUBSCRIPTION_SLOTS = 32
CAN_MAX_FRAMES_PER_S = 4000

METRES_PER_DEG_LAT = 111_320.0


# ---------------------------------------------------------------------------
# Motion
# ---------------------------------------------------------------------------

class Circuit:
    """A constant-speed circular lap, in metres and seconds."""

    def __init__(self, lat_deg=51.5074, lon_deg=-1.3970,
                 radius_m=180.0, speed_mps=38.0):
        self.lat0, self.lon0 = lat_deg, lon_deg
        self.radius, self.speed = radius_m, speed_mps
        self._m_per_deg_lon = METRES_PER_DEG_LAT * math.cos(math.radians(lat_deg))

    def at(self, t_s):
        """Position, velocity and body-frame motion at `t_s` seconds."""
        omega = self.speed / self.radius          # rad/s around the circle
        theta = omega * t_s
        north = self.radius * math.cos(theta)
        east = self.radius * math.sin(theta)

        # Heading of motion is the tangent, 90 degrees ahead of the radius.
        heading = (math.degrees(theta) + 90.0) % 360.0
        hrad = math.radians(heading)

        return {
            "lat": self.lat0 + north / METRES_PER_DEG_LAT,
            "lon": self.lon0 + east / self._m_per_deg_lon,
            "vel_n": self.speed * math.cos(hrad),
            "vel_e": self.speed * math.sin(hrad),
            "heading": heading,
            # Centripetal acceleration is constant on a circle and points at
            # the centre, i.e. along the body Y axis.
            "lat_g": (self.speed ** 2 / self.radius) / 9.80665,
            "yaw_rate": math.degrees(omega),
            "speed": self.speed,
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
                 circuit=None):
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
        self._monitor_channels = [
            (0, CH_LAP_TIME), (1, CH_LAST_LAP_TIME),
            (2, CH_DELTA_BEST), (3, CH_LAP_NUMBER),
        ]
        # slot -> (value, present). Absent is a state the display renders, not
        # a value it substitutes.
        self._monitor_values = {}
        self._monitor_seq = None
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

    def simulate_loss(self, stream, count):
        """Pretend the device accepted `count` items and had to discard them.

        Not decoration: a client has to handle loss, and loss on a desktop
        peripheral otherwise never happens, so the path that reads `dropped`
        would go untested until a real device on a real track produced some.
        """
        if stream not in self._dropped:
            raise ValueError(f"unknown stream {stream!r}")
        self._dropped[stream] += count

    def _next_seq(self, stream):
        self._seq[stream] = (self._seq[stream] + 1) & 0xFFFF
        return self._seq[stream]

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
            "protocol_major": 1,
            "protocol_minor": 0,
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
            "fix_flags": 0,
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
            "ax": 0,
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
        st = self.circuit.at(now / 1e6)
        rpm = int(1500 + st["speed"] * 120)
        kph = int(st["speed"] * 3.6)
        yield 0x0C0, 50, struct.pack("<HH4x", rpm, kph)
        yield 0x1A0, 20, struct.pack("<BB6x", 62, 0)          # throttle, brake
        yield 0x2E0, 10, struct.pack("<h6x", round(st["lat_g"] * 100))

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
            interval = round(1_000_000 / rate_hz)
            if now - sub["last"] < interval:
                continue
            sub["seen"] += 1
            # SPEC.md §6.8 — the first matching frame is forwarded in every
            # mode. A client that installs a subscription and waits for a value
            # to display should not have to wait for a second frame.
            first = sub["seen"] == 1
            emit = True
            if sub["mode"] == SUB_PERIODIC and sub["arg"] and not first:
                emit = (now - sub["emitted_at"]) >= sub["arg"] * 1000
            elif sub["mode"] == SUB_EVERY_NTH and sub["arg"]:
                emit = ((sub["seen"] - 1) % sub["arg"]) == 0
            elif sub["mode"] == SUB_ON_CHANGE and not first:
                emit = payload != sub["last_payload"]
                if emit and sub["arg"]:
                    emit = (now - sub["emitted_at"]) >= sub["arg"] * 1000
            sub["last"] = now
            if emit:
                sub["emitted_at"] = now
                sub["last_payload"] = payload
                yield {"id": cid, "payload": payload, "_t": now}

    # -- Control ----------------------------------------------------------

    def set_link_params(self, **kwargs):
        """Called by the transport once it knows the negotiated link."""
        self._link = kwargs

    def handle_control(self, request):
        """SPEC.md §9. `[opcode][tag][params]` in, `[opcode][tag][status]
        [detail]` out. A device MUST respond to every request."""
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
            cid &= CAN_ID_BITS
            mask &= CAN_ID_BITS

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

            handle = self._next_handle
            self._next_handle = (self._next_handle % 0xFFFF) + 1
            self._subscriptions[handle] = {
                "id": cid, "mask": mask, "mode": mode, "arg": arg,
                "last": 0, "seen": 0, "emitted_at": 0, "last_payload": None,
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
            # SPEC.md §9: "Response echoes the device t_device at receipt".
            return reply(ST_OK, struct.pack("<Q", self.now_us()))

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
        return None

    def display_lines(self):
        """What the device's screen would show. Absent renders as '--', never
        as a number nobody supplied."""
        names = {CH_LAP_TIME: "LAP", CH_LAST_LAP_TIME: "LAST",
                 CH_DELTA_BEST: "DELTA", CH_LAP_NUMBER: "NO."}
        out = []
        for slot, channel in self._monitor_channels:
            value, present = self._monitor_values.get(slot, (0, False))
            label = names.get(channel, f"CH{channel}")
            out.append(f"{label}: {value if present else '--'}")
        return out

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
