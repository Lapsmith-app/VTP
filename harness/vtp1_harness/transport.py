"""The link to the device under test, and the one platform-specific part.

Everything above this file is arithmetic on bytes and is identical on every
operating system. Everything platform-specific is here, behind `Transport`:
`BleakTransport` speaks to real firmware over WinRT, CoreBluetooth or BlueZ,
and `LoopbackTransport` speaks to the software peripheral in-process, with no
radio at all.

That second implementation is not a convenience. It is how the harness is
tested: the checks run against a device whose behaviour is known, including
deliberately broken versions of it, on a CI runner that has no Bluetooth
adapter. A conformance tool nobody has tested is an opinion.
"""
import asyncio
import contextlib
import struct
import time

from . import refdec


def _load_peripheral():
    """Import the software peripheral, which lives outside any package."""
    import sys
    path = str(refdec.ROOT / "reference" / "peripheral")
    if path not in sys.path:
        sys.path.insert(0, path)
    import vtp_device
    return vtp_device


class TransportError(Exception):
    """The link failed, or the platform refused the operation."""


class DeviceRefused(TransportError):
    """The device answered an operation with an ATT error.

    Distinguished from `TransportError` because for several checks it is the
    *expected* outcome — SPEC.md §13.4 asks a device to reject a malformed
    Monitor write, and at the GATT layer rejecting is all an error response is.
    """


class Characteristic:
    __slots__ = ("uuid", "properties", "service_uuid")

    def __init__(self, uuid, properties, service_uuid):
        self.uuid = uuid.lower()
        self.properties = frozenset(properties)
        self.service_uuid = service_uuid.lower()

    def __repr__(self):
        return f"<{refdec.CHAR_NAME.get(self.uuid, self.uuid)} {sorted(self.properties)}>"


class Advert:
    """What a scan saw, before anything was connected to.

    `service_data_available` is separate from an empty `service_data` because
    the two are different findings. Every desktop platform this runs on does
    surface Service Data to a central, so an absent one is the device's silence
    and can be reported as such (SPEC.md §3.3).
    """

    def __init__(self, address, name=None, rssi=None, service_uuids=(),
                 service_data=None, manufacturer_data=None):
        self.address = address
        self.name = name
        self.rssi = rssi
        self.service_uuids = [u.lower() for u in service_uuids]
        self.service_data = {k.lower(): bytes(v)
                             for k, v in (service_data or {}).items()}
        self.manufacturer_data = dict(manufacturer_data or {})

    @property
    def is_vtp(self):
        return refdec.SERVICE_UUID in self.service_uuids

    @property
    def vtp_service_data(self):
        return self.service_data.get(refdec.SERVICE_UUID)

    def __repr__(self):
        return f"<Advert {self.name or '(unnamed)'} {self.address}>"


class Transport:
    """What a check may ask of the link. Deliberately small."""

    #: True when the platform can report the negotiated ATT MTU.
    reports_mtu = True
    #: A human-readable name for the report header.
    kind = "transport"

    async def scan(self, timeout): raise NotImplementedError
    async def connect(self, target): raise NotImplementedError
    async def disconnect(self): raise NotImplementedError
    async def read(self, uuid): raise NotImplementedError
    async def write(self, uuid, data, response=True): raise NotImplementedError
    async def subscribe(self, uuid, callback): raise NotImplementedError
    async def unsubscribe(self, uuid): raise NotImplementedError

    @property
    def mtu(self): raise NotImplementedError

    def characteristics(self): raise NotImplementedError

    def services(self): raise NotImplementedError


# ---------------------------------------------------------------------------
# Real hardware
# ---------------------------------------------------------------------------

class BleakTransport(Transport):
    """A BLE central over bleak: WinRT, CoreBluetooth or BlueZ."""

    kind = "bluetooth"

    def __init__(self, *, use_cached_services=False, connect_timeout=20.0):
        self._client = None
        self._advert = None
        self._use_cached = use_cached_services
        self._connect_timeout = connect_timeout
        self._subscribed = set()
        self._disconnected = asyncio.Event()

    async def scan(self, timeout):
        from bleak import BleakScanner
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        out = []
        for device, adv in found.values():
            out.append((device, Advert(
                address=device.address,
                name=adv.local_name or device.name,
                rssi=adv.rssi,
                service_uuids=adv.service_uuids or (),
                service_data=adv.service_data,
                manufacturer_data=adv.manufacturer_data,
            )))
        self._scanned = {a.address: d for d, a in out}
        return [a for _, a in out]

    async def connect(self, target):
        from bleak import BleakClient
        from bleak.exc import BleakError

        handle = getattr(self, "_scanned", {}).get(
            target.address if isinstance(target, Advert) else target, None)
        if handle is None:
            handle = target.address if isinstance(target, Advert) else target
        self._advert = target if isinstance(target, Advert) else None
        self._disconnected.clear()

        def on_disconnect(_client):
            self._disconnected.set()

        # Both platforms cache a peer's GATT table across connections, and a
        # device under development changes its GATT table constantly. A stale
        # cache makes the harness report a layout the firmware no longer has,
        # which is the worst possible failure for a tool whose whole job is
        # telling you what your device looks like from outside.
        self._client = BleakClient(
            handle, disconnected_callback=on_disconnect,
            timeout=self._connect_timeout,
            winrt={"use_cached_services": self._use_cached},
        )
        try:
            await self._client.connect()
        except BleakError as exc:
            raise TransportError(str(exc)) from exc
        except asyncio.TimeoutError as exc:
            raise TransportError("timed out connecting") from exc

    async def disconnect(self):
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.disconnect()
        self._client = None
        self._subscribed.clear()

    @property
    def connected(self):
        return self._client is not None and self._client.is_connected

    @property
    def mtu(self):
        try:
            return self._client.mtu_size
        except Exception:
            return None

    @property
    def advert(self):
        return self._advert

    def services(self):
        return {s.uuid.lower() for s in self._client.services}

    def characteristics(self):
        out = {}
        for service in self._client.services:
            for ch in service.characteristics:
                out[ch.uuid.lower()] = Characteristic(
                    ch.uuid, ch.properties, service.uuid)
        return out

    def _wrap(self, exc):
        """An operation failed. Was it the device refusing, or the link dying?

        The platforms do not agree on how an ATT error reaches an application
        and none of them reports the error code reliably, so the distinction is
        made from the one thing all three agree on: whether the link is still
        up afterwards. A device that answered an error response is still
        connected; a device that went away is not.
        """
        if self.connected:
            return DeviceRefused(str(exc))
        return TransportError(str(exc))

    async def read(self, uuid):
        try:
            return bytes(await self._client.read_gatt_char(uuid))
        except Exception as exc:
            raise self._wrap(exc) from exc

    async def write(self, uuid, data, response=True):
        try:
            await self._client.write_gatt_char(uuid, bytes(data), response=response)
        except Exception as exc:
            raise self._wrap(exc) from exc

    async def subscribe(self, uuid, callback):
        loop = asyncio.get_running_loop()

        def handler(_char, data):
            # Stamped at arrival rather than at processing: several checks
            # measure the device's clock against the host's, and a queue behind
            # this callback would be charged to the device.
            callback(bytes(data), loop.time())

        try:
            await self._client.start_notify(uuid, handler)
        except Exception as exc:
            raise self._wrap(exc) from exc
        self._subscribed.add(uuid.lower())

    async def unsubscribe(self, uuid):
        try:
            await self._client.stop_notify(uuid)
        except Exception as exc:
            raise self._wrap(exc) from exc
        self._subscribed.discard(uuid.lower())


# ---------------------------------------------------------------------------
# The software peripheral, in-process
# ---------------------------------------------------------------------------

#: Faults the loopback device can be told to exhibit. Each one is a real
#: mistake a firmware could make, and each is here because some check in this
#: harness claims to catch it — tests/test_faults.py asserts that it does.
FAULTS = {
    "seq_starts_at_one": "SPEC.md §8.2 — first notification carries 1, not 0",
    "seq_repeats": "SPEC.md §8.2 — the sequence number does not advance",
    "detail_on_error": "SPEC.md §9 — a refused request answered with a detail",
    "no_tag_echo": "SPEC.md §9 — the response tag does not echo the request",
    "timesync_single_reading": "SPEC.md §9.7 — t_device_rx and t_device_tx are one reading",
    "monitor_accepts_partial": "SPEC.md §13.4 — an incomplete write is accepted",
    "monitor_accepts_duplicate_slot": "SPEC.md §13.4 — a slot twice in one write is accepted",
    "subs_survive_reconnect": "SPEC.md §9.2 — the subscription table is not cleared",
    "unknown_handle_ok": "SPEC.md §9.2 — an unknown handle is answered ok",
    "stream_before_subscribe": "SPEC.md §9.2 — CAN frames arrive with no subscription installed",
    "caps_reserved_bits": "SPEC.md §4 — a reserved capability bit is set",
    "absent_field_nonzero": "SPEC.md §5.1 — a field whose validity bit is clear is not zero",
    "clock_per_stream": "SPEC.md §8.1 — the streams are not on one clock",
    "drop_fourth_request": "SPEC.md §9 — a fourth outstanding request is silently discarded",
    "phy_half_reported": "SPEC.md §9.1 — the phy validity bit is set with only one PHY known",
    "list_reserved_nonzero": "SPEC.md §9.5 — a reserved page byte is not zero",
}


class LoopbackTransport(Transport):
    """The software peripheral wired straight to the harness, no radio.

    This is `reference/peripheral/serve.py` with the Bluetooth taken out: the
    same `VtpDevice`, the same control dispatch, the same notification pump.
    What it adds is `faults` — a way to ask for a device that is wrong in one
    specific, named way, so that a check claiming to catch that mistake can be
    made to prove it.
    """

    kind = "loopback"

    def __init__(self, *, faults=(), mtu=247, gps_hz=10, imu_hz=100,
                 poll_interval=0.005, device_kwargs=None):
        unknown = set(faults) - set(FAULTS)
        if unknown:
            raise ValueError(f"unknown fault(s): {sorted(unknown)}")
        self.faults = set(faults)
        self._mtu = mtu
        self._poll_interval = poll_interval
        self._device_kwargs = dict(device_kwargs or {})
        self._device_kwargs.setdefault("gps_hz", gps_hz)
        self._device_kwargs.setdefault("imu_hz", imu_hz)
        self.device = None
        self._pump = None
        self._subs = {}
        self._connected = False
        self._stale_subs = {}
        self._requests = 0

    # -- lifecycle --------------------------------------------------------

    async def scan(self, timeout):
        minor = refdec.SCHEMA["protocol"]["minor"]
        caps = self._capabilities() & 0xFF
        return [Advert(
            address="00:00:00:00:00:01",
            name="VTP",
            rssi=-40,
            service_uuids=[refdec.SERVICE_UUID],
            service_data={refdec.SERVICE_UUID: bytes([minor, caps, 0x01])},
        )]

    _declared = None

    def _capabilities(self):
        """What the peripheral itself declares, read from its own Info.

        Taken from the device rather than restated here, so the advertisement,
        the GATT layout and Info cannot disagree for any reason except a fault
        this transport was asked to inject.
        """
        if LoopbackTransport._declared is None:
            probe = _load_peripheral().VtpDevice()
            (caps,) = struct.unpack_from(
                "<I", probe.info(), refdec.offset("info", "capabilities"))
            LoopbackTransport._declared = caps
        caps = LoopbackTransport._declared
        if "caps_reserved_bits" in self.faults:
            caps |= 1 << 12
        return caps

    async def connect(self, target=None):
        vtp_device = _load_peripheral()
        self.device = vtp_device.VtpDevice(mtu=self._mtu, **self._device_kwargs)
        self.device.set_negotiated_mtu(self._mtu)
        self.device.on_connect()
        if "subs_survive_reconnect" in self.faults and self._stale_subs:
            # SPEC.md §9.2 — the table MUST be cleared when the link drops. A
            # device that keeps it hands the next client state it never
            # installed and cannot account for.
            self.device._subscriptions.update(self._stale_subs)
        if "stream_before_subscribe" in self.faults:
            # A device that streams what nobody asked for: one subscription
            # matching every identifier, installed by the device itself.
            self.device._subscriptions[self.device._allocate_handle()] = {
                "id": 0, "mask": 0, "mode": 0, "arg": 0, "per_id": {}}
        self._connected = True
        self._first_sent = set()
        self._pump = asyncio.create_task(self._run())

    async def disconnect(self):
        self._stale_subs = dict(getattr(self.device, "_subscriptions", {}))
        self._connected = False
        if self._pump is not None:
            self._pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pump
            self._pump = None
        if self.device is not None:
            self.device.on_disconnect()
        self._subs.clear()

    @property
    def connected(self):
        return self._connected

    @property
    def mtu(self):
        return self._mtu

    @property
    def advert(self):
        return None

    def services(self):
        return {refdec.SERVICE_UUID, refdec.DIS_SERVICE}

    def characteristics(self):
        caps = self._capabilities()
        chars = {refdec.CHAR["info"]: ("read",)}
        if caps & (1 << refdec.CAPABILITIES["gps"]):
            chars[refdec.CHAR["gps"]] = ("notify",)
        if caps & (1 << refdec.CAPABILITIES["can"]):
            chars[refdec.CHAR["can"]] = ("notify",)
        if caps & (1 << refdec.CAPABILITIES["imu"]):
            chars[refdec.CHAR["imu"]] = ("notify",)
        if caps & (1 << refdec.CAPABILITIES["control"]):
            chars[refdec.CHAR["control"]] = ("write", "indicate")
        if caps & (1 << refdec.CAPABILITIES["monitor"]):
            chars[refdec.CHAR["monitor_values"]] = ("write",)
        out = {u: Characteristic(u, p, refdec.SERVICE_UUID)
               for u, p in chars.items()}
        for uuid in refdec.DIS_CHARS.values():
            out[uuid] = Characteristic(uuid, ("read",), refdec.DIS_SERVICE)
        return out

    # -- GATT operations --------------------------------------------------

    _DIS_VALUES = {
        "manufacturer_name": b"VTP Reference",
        "model_number": b"Software Peripheral",
        "firmware_revision": b"0.1.0",
    }

    async def read(self, uuid):
        uuid = uuid.lower()
        if uuid == refdec.CHAR["info"]:
            info = bytearray(self.device.info())
            if "caps_reserved_bits" in self.faults:
                struct.pack_into("<I", info, refdec.offset("info", "capabilities"),
                                 self._capabilities())
            return bytes(info)
        for name, char_uuid in refdec.DIS_CHARS.items():
            if uuid == char_uuid:
                return self._DIS_VALUES[name]
        raise DeviceRefused(f"not readable: {uuid}")

    async def write(self, uuid, data, response=True):
        uuid, data = uuid.lower(), bytes(data)
        if uuid == refdec.CHAR["control"]:
            return await self._control_write(data)
        if uuid == refdec.CHAR["monitor_values"]:
            return self._monitor_write(data)
        raise DeviceRefused(f"not writable: {uuid}")

    def _monitor_write(self, data):
        reason = self.device.handle_monitor_write(data)
        if reason == "duplicate-slot" and "monitor_accepts_duplicate_slot" in self.faults:
            return
        if reason and reason.startswith("incomplete") and "monitor_accepts_partial" in self.faults:
            return
        if reason:
            # SPEC.md §13.4 — the write length must be exactly right and a
            # device MUST reject any other. At the GATT layer, rejecting is an
            # error response and nothing else.
            raise DeviceRefused(reason)

    async def _control_write(self, request):
        # SPEC.md §9.6 -- deliverability is decided BEFORE dispatch. With
        # indications disabled the answer has nowhere to go, so the request MUST
        # NOT take effect and MUST NOT be counted as received.
        if refdec.CHAR["control"] not in self._subs:
            return
        t_rx = self.device.now_us()
        if "drop_fourth_request" in self.faults:
            self._requests += 1
            if self._requests % 4 == 0:
                return                              # answered by nobody
        # A real device answers on its own schedule; the delay keeps requests
        # genuinely outstanding so the overlap checks have something to see.
        await asyncio.sleep(0)
        response = self.device.handle_control(request, t_rx=t_rx)
        if response is None:
            return
        response = self._corrupt_response(bytearray(response), request)
        cb = self._subs.get(refdec.CHAR["control"])
        if cb is not None:
            cb(bytes(response), asyncio.get_running_loop().time())

    def _corrupt_response(self, response, request):
        opcode, status = response[0], response[2]
        if "no_tag_echo" in self.faults:
            response[1] = (response[1] + 1) & 0xFF
        if "detail_on_error" in self.faults and status != 0:
            response += b"\x00\x00"
        if "unknown_handle_ok" in self.faults and \
                opcode == refdec.OPCODE["CAN_UNSUBSCRIBE"] and \
                status == refdec.STATUS_VALUE["unknown_handle"]:
            response[2] = 0
        if "timesync_single_reading" in self.faults and \
                opcode == refdec.OPCODE["TIME_SYNC"] and status == 0:
            struct.pack_into("<Q", response, 3 + 8, *struct.unpack_from("<Q", response, 3))
        if "phy_half_reported" in self.faults and \
                opcode == refdec.OPCODE["GET_LINK_PARAMS"] and status == 0:
            base = 3
            validity = struct.unpack_from("<H", response, base)[0]
            validity |= 1 << refdec.bit("link_validity", "phy")
            struct.pack_into("<H", response, base, validity)
            response[base + refdec.offset("link_params", "phy_tx")] = 1
            response[base + refdec.offset("link_params", "phy_rx")] = 0
        if "list_reserved_nonzero" in self.faults and status == 0 and opcode in (
                refdec.OPCODE["CAN_LIST"], refdec.OPCODE["MONITOR_LIST"]):
            record = "can_list_page" if opcode == refdec.OPCODE["CAN_LIST"] else "monitor_page"
            response[3 + refdec.offset(record, "reserved")] = 1
        return response

    async def subscribe(self, uuid, callback):
        self._subs[uuid.lower()] = callback

    async def unsubscribe(self, uuid):
        self._subs.pop(uuid.lower(), None)

    # -- the notification pump -------------------------------------------

    _STREAM_CHAR = {"gps": "gps", "can": "can", "imu": "imu"}

    async def _run(self):
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(self._poll_interval)
            for stream, payload in self.device.poll():
                uuid = refdec.CHAR[self._STREAM_CHAR[stream]]
                cb = self._subs.get(uuid)
                if cb is None:
                    # Not subscribed: SPEC.md §8.2 counts notifications SENT,
                    # so an undelivered one must not spend a sequence number.
                    self.device.record_refused(stream, payload)
                    continue
                stamped = bytearray(self.device.stamp_seq(stream, payload))
                self._apply_stream_faults(stream, stamped)
                self.device.commit_seq(stream)
                cb(bytes(stamped), loop.time())

    _SEQ_RECORD = {"gps": "gps_fix", "can": "can_header", "imu": "imu_header"}

    def _apply_stream_faults(self, stream, payload):
        if "seq_starts_at_one" in self.faults or "seq_repeats" in self.faults:
            off = refdec.offset(self._SEQ_RECORD[stream], "seq")
            seq = struct.unpack_from("<H", payload, off)[0]
            if "seq_repeats" in self.faults:
                seq = 0
            else:
                seq = (seq + 1) & 0xFFFF
            struct.pack_into("<H", payload, off, seq)
        if "clock_per_stream" in self.faults and stream == "imu":
            # One clock per sensor is the mistake §8.1 exists to forbid: each
            # stream looks perfectly self-consistent and no two agree.
            off = refdec.offset("imu_header", "t_base")
            t = struct.unpack_from("<Q", payload, off)[0]
            struct.pack_into("<Q", payload, off, t + 3_600_000_000)
        if "absent_field_nonzero" in self.faults and stream == "gps":
            # A value written into a field whose validity bit is clear: the
            # plausible wrong value SPEC.md §1.1 exists to prevent, and one no
            # byte vector can catch because the bytes decode perfectly. Which
            # field is left to the schema rather than named here, so this keeps
            # working when the peripheral changes what it can measure.
            bit_of = refdec.bits("gps_validity")
            pack = {"u8": "B", "i8": "b", "u16": "H", "i16": "h",
                    "u32": "I", "i32": "i", "u64": "Q", "i64": "q"}
            validity = struct.unpack_from(
                "<I", payload, refdec.offset("gps_fix", "validity"))[0]
            for field in refdec.SCHEMA["records"]["gps_fix"]["fields"]:
                valid_bit = field.get("valid_bit")
                if valid_bit is None or validity & (1 << bit_of[valid_bit]):
                    continue
                struct.pack_into("<" + pack[field["type"]], payload,
                                 field["offset"], 1234)
                break
