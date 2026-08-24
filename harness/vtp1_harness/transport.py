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
    "timesync_single_reading": "SPEC.md §9.5 — t_device_rx and t_device_tx are one reading",
    "monitor_accepts_partial": "SPEC.md §13.4 — an incomplete write is accepted",
    "monitor_accepts_duplicate_slot": "SPEC.md §13.4 — a slot twice in one write is accepted",
    "subs_survive_reconnect": "SPEC.md §9.1 — the subscription table is not cleared",
    "duplicate_consumes_slot": "SPEC.md §9.1 — re-installing the same id and mask silently takes a second slot",
    "duplicate_double_entry": "SPEC.md §9.1 — re-installing the same id and mask creates a second removable entry",
    "table_full_early": "SPEC.md §9.1 — table_full arrives one subscription before the capacity Info declares",
    "overlap_wrong_governor": "SPEC.md §9.2 — the least specific matching mask governs the frame",
    "tie_break_latest": "SPEC.md §9.2 — equally specific masks tie-break to the latest installed",
    "can_duplicate_across_batches": "SPEC.md §9.2 — a forwarded frame is repeated in a later batch",
    "unknown_subscription_ok": "SPEC.md §9.1 — an unknown id and mask is answered ok",
    "stream_before_subscribe": "SPEC.md §9.1 — CAN frames arrive with no subscription installed",
    "caps_reserved_bits": "SPEC.md §4 — a reserved capability bit is set",
    "absent_field_nonzero": "SPEC.md §5.1 — a field whose validity bit is clear is not zero",
    "clock_per_stream": "SPEC.md §8.1 — the streams are not on one clock",
    "drops_a_response": "SPEC.md §9 — a request is silently discarded rather than answered",
    "pipelines_silently": "SPEC.md §9 — a second request is applied instead of answered busy",
    "busy_but_applied": "SPEC.md §9 — a request answered busy is applied anyway",
    "list_reserved_nonzero": "SPEC.md §13.3 — a reserved declaration byte is not zero",
    "missing_characteristic": "SPEC.md §4.1 — a characteristic is absent rather than inert",
    "extra_characteristic": "SPEC.md §4.1 — the service carries a characteristic it must not",
    "inert_cccd_rejected": "SPEC.md §4.1 — a CCCD write on an inert stream is refused",
    "implication_broken": "SPEC.md §4.1 — a capability bit without the bit it requires",
    "opcode_capability_late": "SPEC.md §9 — an unowned opcode answered bad_params, not unsupported_opcode",
    "rate_not_applied": "SPEC.md §9.6 — a rate answered ok and never applied",
    # Everything below is a defect a device could be shipping today and this
    # harness would have said nothing about, because no seeded fault ever made
    # the check that covers it fail. See harness/selftest.py: the reverse
    # coverage gate is what turned each of these from an untested claim into a
    # tested one.
    "timesync_unsupported": "SPEC.md §9.5 — TIME_SYNC answered unsupported_opcode, an opcode with no owning capability",
    "monitor_paged_declaration": "SPEC.md §13.3 — MONITOR_LIST answers the superseded paged declaration",
    "monitor_accepts_bad_length": "SPEC.md §13.4 — a write whose length contradicts its count is accepted",
    "monitor_rejects_unknown_slot": "SPEC.md §13.1 — a value for an undeclared slot is refused rather than ignored",
    "params_ignored": "SPEC.md §9 — wrong-length parameters are parsed leniently and answered ok",
    "unallocated_opcode_ok": "SPEC.md §9 — an opcode this version does not define is answered ok",
    "info_truncated": "SPEC.md §4 — Info is shorter than the record it must be",
    "info_major_wrong": "SPEC.md §4 — protocol_major disagrees with the service UUID's major",
    "capacity_zero": "SPEC.md §4.1 — a declared role publishes a capacity of zero",
    "advert_no_service_uuid": "SPEC.md §3.3 — the advertisement omits the VTP/1 service UUID",
    "advert_caps_disagree": "SPEC.md §3.3 — advertised Service Data contradicts Info",
    "clock_steps_backwards": "SPEC.md §8.1 — the device clock jumps backwards while connected",
    "stream_truncated": "SPEC.md §5 — a notification is shorter than the record it carries",
    "seq_survives_reconnect": "SPEC.md §8.2 — sequence numbers continue rather than restarting at 0",
    "rate_ceiling_ignored": "SPEC.md §9.6 — a rate above the declared maximum is accepted, not refused rate_exceeded",
    "info_rate_above_ceiling": "SPEC.md §4 — Info publishes a current rate above its own maximum",
    "inert_control_accepts_writes": "SPEC.md §4.1 — a device that has not declared Control answers writes to it",
    "power_unsupported": "SPEC.md §9.7 — GET_POWER refused by a device that declares the capability owning it",
    "power_percent_impossible": "SPEC.md §9.7 — a percent above 100, which a device MUST NOT emit and a client MUST NOT clamp",
    "power_stale_behind_bit": "SPEC.md §9.7 — a charge reading left in the bytes with its validity bit clear",
    "power_declared_but_empty": "SPEC.md §9.7 — the `power` capability declared and nothing reported valid",
    "power_reserved_nonzero": "SPEC.md §9.7 — the reserved byte of power_state is not zero",
    # SPEC.md §14. Every one of these is a defect that costs a client its
    # transfer without ever refusing a request, which is what makes the role
    # worth checking at all: the bulk path carries no errors by construction,
    # so a device that gets these wrong looks exactly like one that works.
    "aid_stale_held_until": "SPEC.md §14.2 — held_until carries a value behind a cleared validity bit",
    "aid_chunk_exceeds_mtu": "SPEC.md §14.3 — chunk_bytes is larger than one Write Command can carry",
    "aid_accepts_undeclared_format": "SPEC.md §14.1 — a transfer opens in a format the device never declared",
    "aid_accepts_oversized": "SPEC.md §14.2 — a transfer above the declared ceiling is accepted",
    "aid_applied_with_missing_index": "SPEC.md §14.4 — an applied transfer still reports a missing chunk",
    "aid_reports_first_chunk_missing": "SPEC.md §14.4 — the gap is always reported as chunk 0",
    "aid_ignores_crc": "SPEC.md §14.4 — a transfer whose CRC does not match is applied anyway",
    "aid_begin_keeps_transfer": "SPEC.md §14.3 — a BEGIN over an open transfer is answered ok and the old transfer kept",
    "aid_token_reused": "SPEC.md §14.3 — a superseding BEGIN reuses the discarded transfer's token",
    "aid_token_ignored": "SPEC.md §14.3 — a chunk naming the wrong token is accepted instead of ignored",
    # SPEC.md §15. The OBD role transmits on the vehicle bus, so most of what
    # is worth seeding is a device that transmits when it said it would not,
    # or misreports what the car said when it did.
    "obd_probe_unsupported": "SPEC.md §15.2 — OBD_INFO refused by a device that declares the capability owning it",
    "obd_responded_without_ecus": "SPEC.md §15.2 — `responded` set with no ECU listed",
    "obd_entries_descending": "SPEC.md §15.2 — the ECU list is not strictly ascending",
    "obd_stale_behind_bit": "SPEC.md §15.2 — a previous car's probe left in the bytes behind a cleared `responded` bit",
    "obd_reserved_nonzero": "SPEC.md §15.2 — the reserved bytes of obd_probe are not zero",
    "obd_capacity_zero": "SPEC.md §15 — the `obd` bit declared with obd_poll_slots 0, a poll set nothing fits",
    "obd_accepts_unsupported_pid": "SPEC.md §15.4 — a PID the probe's union does not claim is polled anyway",
    "obd_ignores_stop": "SPEC.md §15.7 — the empty poll set is answered ok and the transmitter keeps going",
    "obd_flag_never_set": "SPEC.md §15.6 — the poll set is non-empty and no batch carries the polling flag",
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
                 poll_interval=0.005, control_latency=0.03, device_kwargs=None):
        unknown = set(faults) - set(FAULTS)
        if unknown:
            raise ValueError(f"unknown fault(s): {sorted(unknown)}")
        self.faults = set(faults)
        self._mtu = mtu
        self._poll_interval = poll_interval
        # Roughly one connection interval: long enough that a client writing two
        # requests back to back has the second arrive while the first is owed.
        self._control_latency = control_latency
        self._owed = False
        self._device_kwargs = dict(device_kwargs or {})
        self._device_kwargs.setdefault("gps_hz", gps_hz)
        self._device_kwargs.setdefault("imu_hz", imu_hz)
        self.device = None
        self._pump = None
        self._subs = {}
        self._connected = False
        self._stale_subs = {}
        self._seen_a_connection = False
        self._dup_entries = {}
        self._dup_shunt = 0
        self._pending_dup_unsub = None

    # -- lifecycle --------------------------------------------------------

    async def scan(self, timeout):
        minor = refdec.SCHEMA["protocol"]["minor"]
        caps = self._capabilities() & 0xFF
        if "advert_caps_disagree" in self.faults:
            # SPEC.md §3.3 — the scan list is built from this byte, so a device
            # whose advertisement and Info disagree is labelled wrong in the
            # one place a user chooses between devices.
            caps ^= 1 << refdec.CAPABILITIES["gps"]
        return [Advert(
            address="00:00:00:00:00:01",
            name="VTP",
            rssi=-40,
            service_uuids=([] if "advert_no_service_uuid" in self.faults
                           else [refdec.SERVICE_UUID]),
            service_data={refdec.SERVICE_UUID: bytes([minor, caps, 0x01])},
        )]

    _default_capabilities = None

    def _capabilities(self):
        """What the peripheral itself declares, read from its own Info.

        Taken from the device rather than restated here, so the advertisement,
        the GATT layout and Info cannot disagree for any reason except a fault
        this transport was asked to inject.
        """
        caps = self._device_kwargs.get("capabilities")
        if caps is None:
            if LoopbackTransport._default_capabilities is None:
                probe = _load_peripheral().VtpDevice()
                (caps,) = struct.unpack_from(
                    "<I", probe.info(), refdec.offset("info", "capabilities"))
                LoopbackTransport._default_capabilities = caps
            caps = LoopbackTransport._default_capabilities
        if "caps_reserved_bits" in self.faults:
            caps |= 1 << 12
        if "implication_broken" in self.faults:
            # SPEC.md §4.1 -- can and monitor both require control. Clearing it
            # leaves an Info a client MUST treat as non-conforming.
            caps &= ~(1 << refdec.CAPABILITIES["control"])
        return caps

    async def connect(self, target=None):
        vtp_device = _load_peripheral()
        self.device = vtp_device.VtpDevice(mtu=self._mtu, **self._device_kwargs)
        self.device.set_negotiated_mtu(self._mtu)
        self.device.on_connect()
        if "subs_survive_reconnect" in self.faults and self._stale_subs:
            # SPEC.md §9.1 — the table MUST be cleared when the link drops. A
            # device that keeps it hands the next client state it never
            # installed and cannot account for.
            self.device._subscriptions.update(self._stale_subs)
        if "seq_survives_reconnect" in self.faults and self._seen_a_connection:
            # SPEC.md §8.2 — a device whose counters are global rather than
            # per-connection. A client then cannot tell a reconnection from a
            # wrap, which is the whole reason this protocol needs no session
            # identifier.
            self.device._seq = {k: 100 for k in self.device._seq}
        self._seen_a_connection = True
        if "stream_before_subscribe" in self.faults:
            # A device that streams what nobody asked for: one subscription
            # matching every identifier, installed by the device itself.
            self.device._subscriptions[(0, 0)] = {
                "mode": 0, "arg": 0, "order": 0, "per_id": {}}
        if "overlap_wrong_governor" in self.faults:
            # SPEC.md §9.2 -- the LEAST specific matching mask governs. The
            # frames themselves stay well-formed; only which subscription's
            # mode shapes them changes, which is exactly why the check needs
            # the two subscriptions to differ in mode to see it.
            dev = self.device
            def least_specific(cid, _dev=dev):
                matches = [(k, sub) for k, sub in _dev._subscriptions.items()
                           if (cid & k[1]) == (k[0] & k[1])]
                if not matches:
                    return None
                return min(matches,
                           key=lambda ks: (bin(ks[0][1]).count("1"),
                                           ks[1]["order"]))[1]
            dev._governing = least_specific
        if "tie_break_latest" in self.faults:
            # SPEC.md §9.2 -- specificity still wins, but ties go to the
            # LATEST installed subscription instead of the earliest.
            dev = self.device
            def latest_wins(cid, _dev=dev):
                matches = [(k, sub) for k, sub in _dev._subscriptions.items()
                           if (cid & k[1]) == (k[0] & k[1])]
                if not matches:
                    return None
                return min(matches,
                           key=lambda ks: (-bin(ks[0][1]).count("1"),
                                           -ks[1]["order"]))[1]
            dev._governing = latest_wins
        if "can_duplicate_across_batches" in self.faults:
            # SPEC.md §9.2 -- a forwarded frame is emitted again in the NEXT
            # batch, bus-arrival timestamp and all: cross-batch by
            # construction, which is the shape a per-notification dedup set
            # cannot see, and passed for exactly that reason.
            #
            # Seeded at the RECORD level, after dt quantisation, not by
            # replaying the frame through the batching path. dt is 10 us
            # ticks, so a mid-batch original loses (t - t_base) mod 10 us in
            # encoding while a replayed copy opening its own batch decodes at
            # exactly t -- the two copies then agree on t_device_us only when
            # that remainder happens to be zero, and the fault was caught or
            # missed on a coin toss. Seeding the copy as the next batch's
            # t_base, at the original's DECODED time with dt 0, makes the
            # duplicate byte-provable on every flush.
            dev = self.device
            orig_flush = dev._flush_can
            def duplicating_flush(now, _dev=dev, _orig=orig_flush):
                t0 = _dev._can_batch_t0
                last = (dict(_dev._can_pending[-1])
                        if _dev._can_pending else None)
                payload = _orig(now)
                if payload is not None and last is not None and t0 is not None:
                    _dev._can_batch_t0 = t0 + last["dt"] * 10
                    last["dt"] = 0
                    _dev._can_pending.append(last)
                return payload
            dev._flush_can = duplicating_flush
        self._connected = True
        self._owed = False
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
        """SPEC.md §4.1 -- the attribute table is fixed.

        Every characteristic, whatever the capabilities say. One whose bit is
        clear is inert, not absent: a table that changed with the capability set
        would hand a central that cached it a stale handle to the wrong
        attribute.
        """
        out = {}
        for name, spec in refdec.PROFILE_CHARS.items():
            if "missing_characteristic" in self.faults and name == "imu":
                continue
            out[refdec.CHAR[name]] = Characteristic(
                refdec.CHAR[name], spec["properties"], refdec.SERVICE_UUID)
        if "extra_characteristic" in self.faults:
            uuid = "56544309-" + refdec.SERVICE_UUID.split("-", 1)[1]
            out[uuid] = Characteristic(uuid, ("read",), refdec.SERVICE_UUID)
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
            if {"caps_reserved_bits", "implication_broken"} & self.faults:
                struct.pack_into("<I", info, refdec.offset("info", "capabilities"),
                                 self._capabilities())
            if "obd_capacity_zero" in self.faults:
                # SPEC.md §15 -- the bit is set and the slot count is zero: a
                # poll set nothing fits in, on a role whose whole claim is
                # about what may be asked of it. The generic capacity fault
                # zeroes a capacity's LAST field, so this one takes the
                # first, and the two checks stay separately breakable.
                info[refdec.offset("info", "obd_poll_slots")] = 0
            if "info_major_wrong" in self.faults:
                info[refdec.offset("info", "protocol_major")] = \
                    refdec.PROTOCOL_MAJOR + 1
            if "capacity_zero" in self.faults:
                # SPEC.md §4.1 — a capacity of zero means none, not
                # unspecified, so this is a device that declares a role and
                # then publishes that it can do nothing in it. Which field is
                # left to the schema's capability/capacity pairing rather than
                # named here, so it follows a later minor that adds one.
                declared = {name for name, b in refdec.CAPABILITIES.items()
                            if self._capabilities() & (1 << b)}
                for capability, fields in refdec.CAPACITY_FIELDS.items():
                    if capability in declared:
                        struct.pack_into(
                            "<H", info, refdec.offset("info", fields[-1]), 0)
                        break
            if "info_rate_above_ceiling" in self.faults:
                # SPEC.md §4 — a ceiling below the rate the same record says is
                # running. Both numbers are the device's own, so one of them is
                # false and a client sizing a buffer from either may be wrong.
                struct.pack_into(
                    "<H", info, refdec.offset("info", "gps_max_rate_hz"),
                    max(0, struct.unpack_from(
                        "<H", info, refdec.offset("info", "gps_rate_hz"))[0] - 1))
            if "info_truncated" in self.faults:
                # Last, so it truncates whatever the faults above wrote rather
                # than being overwritten by an offset past its own end.
                return bytes(info[:-1])
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
        if uuid == refdec.CHAR["aiding"]:
            # SPEC.md §14.3 -- a chunk whose token the device never reads: it
            # is rewritten to whatever transfer is open, so a stale chunk
            # from a superseded transfer lands instead of being ignored.
            if "aid_token_ignored" in self.faults and len(data) >= 1 and \
                    getattr(self.device, "_aid", None):
                data = bytes([self.device._aid["token"]]) + bytes(data[1:])
            # SPEC.md §14.3 -- a Write Command. Nothing comes back, including
            # when the device discards it, so the reason is dropped here
            # exactly as a real peripheral drops it.
            self.device.handle_aiding_write(data)
            return
        raise DeviceRefused(f"not writable: {uuid}")

    def _monitor_write(self, data):
        if "monitor_rejects_unknown_slot" in self.faults and \
                self._carries_undeclared_slot(data):
            # SPEC.md §13.1 — the client that is one minor ahead of the device.
            # Refusing its write is the failure mode that clause exists to
            # prevent: the device loses every value in the batch, not just the
            # one it did not recognise.
            raise DeviceRefused("unknown-slot")
        reason = self.device.handle_monitor_write(data)
        if reason == "duplicate-slot" and "monitor_accepts_duplicate_slot" in self.faults:
            return
        if reason == "length" and "monitor_accepts_bad_length" in self.faults:
            return
        if reason and reason.startswith("incomplete") and "monitor_accepts_partial" in self.faults:
            return
        if reason:
            # SPEC.md §13.4 — the write length must be exactly right and a
            # device MUST reject any other. At the GATT layer, rejecting is an
            # error response and nothing else.
            raise DeviceRefused(reason)

    def _carries_undeclared_slot(self, data):
        """True when a well-formed write names a slot the device never asked for."""
        hsize, vsize = refdec.size("monitor_header"), refdec.size("monitor_value")
        if len(data) < hsize or (len(data) - hsize) % vsize:
            return False
        declared = {slot for slot, _ in self.device._monitor_channels}
        return any(data[hsize + i * vsize] not in declared
                   for i in range((len(data) - hsize) // vsize))

    async def _control_write(self, request):
        # SPEC.md §4.1 -- an inert Control rejects every write with an ATT error
        # and parses no opcode: answering would need the response path a device
        # without the capability does not have.
        if not self._capabilities() & (1 << refdec.CAPABILITIES["control"]):
            if "inert_control_accepts_writes" not in self.faults:
                raise DeviceRefused("control capability not declared")
            # The fault: the write is taken rather than refused. Answering is
            # not required to break the rule -- §4.1 is about the ATT error a
            # device without the capability owes every write -- and a device
            # that accepts silently is the harder half to notice.
            return
        # SPEC.md §9.4 -- deliverability is decided BEFORE dispatch. With
        # indications disabled the answer has nowhere to go, so the request MUST
        # NOT take effect and MUST NOT be counted as received.
        if refdec.CHAR["control"] not in self._subs:
            return
        t_rx = self.device.now_us()
        self._rates_before = (self.device.gps_hz, self.device.imu_hz)
        if "params_ignored" in self.faults:
            request = self._parse_leniently(request)
        request = self._indulge_aiding(request)
        if "obd_accepts_unsupported_pid" in self.faults:
            request = self._indulge_obd(request)
        # SPEC.md §15.7 -- a device that answers ok to the stop and keeps
        # transmitting. The poll set is captured before dispatch and put back
        # after it, so the defect is the transmitter's state, not a cosmetic
        # rewrite of the reply.
        obd_poll_before = (list(getattr(self.device, "_obd_poll", ())),
                           getattr(self.device, "_obd_interval_ms", 0))

        # SPEC.md §14.3 -- a BEGIN over an open transfer MUST discard it; this
        # device answers ok and keeps the old one, chunks and all. Seeded
        # ahead of dispatch because the defect is that the device never acts
        # on the request, which a corrupted RESPONSE cannot model. The reply
        # echoes the OLD transfer's chunk_bytes, exactly as such a device
        # would. Gated on the old transfer actually HOLDING a chunk, so only
        # the check that leaves one there -- aiding.begin_supersedes -- meets
        # it, rather than every check that opens a transfer.
        if "aid_begin_keeps_transfer" in self.faults and len(request) >= 2 and \
                request[0] == refdec.OPCODE["GNSS_AID_BEGIN"] and \
                getattr(self.device, "_aid", None) and \
                self.device._aid.get("chunks"):
            import vtp1_encode as _enc
            detail = _enc.encode_aid_begin_result(
                {"token": self.device._aid["token"],
                 "chunk_bytes": self.device._aid["chunk_bytes"]})
            self._deliver_control(bytes([request[0], request[1],
                                         refdec.STATUS_VALUE["ok"]]) + detail)
            return

        # SPEC.md §14.3 -- the superseding BEGIN takes the SAME token the
        # discarded transfer had, so a stale chunk still queued on another
        # EATT bearer is accepted into the new transfer. Gated on the old
        # transfer holding a chunk, like the fault above, so only the check
        # that stages that situation meets it. The device state is rewritten
        # after dispatch, below, so the reuse is real and not cosmetic.
        if "aid_token_reused" in self.faults and len(request) >= 2 and \
                request[0] == refdec.OPCODE["GNSS_AID_BEGIN"] and \
                getattr(self.device, "_aid", None) and \
                self.device._aid.get("chunks"):
            self._aid_reuse_token = self.device._aid["token"]
        else:
            self._aid_reuse_token = None

        # SPEC.md §9.1 -- the duplicate-install family. Each takes effect only
        # on a well-formed subscribe naming an (id, mask) the table already
        # holds, so the checks that stage that situation are the only ones
        # that meet them.
        sub_key = self._parse_subscribe(request)
        if sub_key is not None and sub_key in getattr(
                self.device, "_subscriptions", {}):
            if "duplicate_consumes_slot" in self.faults:
                # The old entry is shunted to a name nothing matches and
                # nothing can remove, so the re-install lands in a fresh
                # entry: one slot silently gone, visible only to arithmetic
                # against Info's declared capacity.
                sub = self.device._subscriptions.pop(sub_key)
                self._dup_shunt += 1
                tomb = (0x3FF00000 + self._dup_shunt, refdec.MASK_EXACT)
                self.device._subscriptions[tomb] = sub
            if "duplicate_double_entry" in self.faults:
                # The re-install will create a second REMOVABLE entry: the
                # device updates in place, but this transport then honours
                # one extra removal, which is what such a table looks like
                # from outside.
                self._dup_entries[sub_key] = self._dup_entries.get(sub_key, 0) + 1
        if "table_full_early" in self.faults and sub_key is not None and \
                sub_key not in getattr(self.device, "_subscriptions", {}):
            slots = getattr(self.device, "CAN_SUBSCRIPTION_SLOTS", None) or \
                _load_peripheral().CAN_SUBSCRIPTION_SLOTS
            if len(self.device._subscriptions) == slots - 1:
                # Refused one short of the capacity Info declares: the
                # classic off-by-one, and exactly the answer a device with a
                # leaked slot gives.
                self._deliver_control(bytes(
                    [request[0], request[1],
                     refdec.STATUS_VALUE["table_full"]]))
                return
        if "duplicate_double_entry" in self.faults and len(request) >= 10 and \
                request[0] == refdec.OPCODE["CAN_UNSUBSCRIBE"]:
            cid, mask = struct.unpack_from("<II", request, 2)
            self._pending_dup_unsub = (cid, mask)
        else:
            self._pending_dup_unsub = None

        # SPEC.md §9 -- a client has at most ONE request outstanding, and a
        # device meeting one that pipelines anyway answers `busy` and MUST NOT
        # apply the request. Modelled here rather than left to the device
        # model, for the same reason serve.py holds it in the transport: it is
        # a property of the response path, not of what the opcode does.
        if self._owed:
            if "pipelines_silently" not in self.faults:
                if "busy_but_applied" in self.faults:
                    self.device.handle_control(request, t_rx=t_rx)
                if len(request) >= 2:
                    self._deliver_control(bytes([request[0], request[1],
                                                 refdec.STATUS_VALUE["busy"]]))
                return

        response = self.device.handle_control(request, t_rx=t_rx)
        if response is None:
            return
        if "obd_ignores_stop" in self.faults and len(request) >= 5 and \
                request[0] == refdec.OPCODE["OBD_POLL_SET"] and \
                request[4] == 0 and response[2] == 0 and obd_poll_before[0]:
            self.device._obd_poll = obd_poll_before[0]
            self.device._obd_interval_ms = obd_poll_before[1]
        if "drops_a_response" in self.faults and len(request) == 2 and \
                request[0] == refdec.OPCODE["TIME_SYNC"]:
            # Only the well-formed one, so exactly one check meets it. A device
            # that drops responses drops them for every request, and any check
            # making that request would catch it -- which would make WHICH check
            # reports it an accident of ordering rather than a property worth
            # asserting.
            return                                  # answered by nobody
        response = self._corrupt_response(bytearray(response), request)
        self._owed = True
        asyncio.create_task(self._answer(bytes(response)))

    async def _answer(self, response):
        """Deliver on the device's own schedule, not the caller's.

        The delay is what makes a request genuinely outstanding. Without it
        every write is answered before the next one is written, and a check
        about what a device does with two at once passes by never creating the
        condition it is testing -- which is exactly how this harness came to
        assert the opposite rule for a while without noticing.
        """
        await asyncio.sleep(self._control_latency)
        self._deliver_control(response)
        self._owed = False

    def _deliver_control(self, response):
        cb = self._subs.get(refdec.CHAR["control"])
        if cb is not None:
            cb(response, asyncio.get_running_loop().time())

    def _parse_subscribe(self, request):
        """(id, mask) of a well-formed subscribe request, else None."""
        if len(request) < 2:
            return None
        if request[0] == refdec.OPCODE["CAN_SUBSCRIBE"] and \
                len(request) == 2 + refdec.OPCODE_PARAM_SIZE["CAN_SUBSCRIBE"]:
            (cid,) = struct.unpack_from("<I", request, 2)
            return (cid, refdec.MASK_EXACT)
        if request[0] == refdec.OPCODE["CAN_SUBSCRIBE_MASK"] and \
                len(request) == 2 + refdec.OPCODE_PARAM_SIZE["CAN_SUBSCRIBE_MASK"]:
            cid, mask = struct.unpack_from("<II", request, 2)
            return (cid, mask)
        return None

    def _parse_leniently(self, request):
        """SPEC.md §9 — the device that takes what it was given and copes.

        Trailing bytes dropped, a short block padded, and every request then
        answered ok. It is the most natural way to write a parser and the
        reason `control.malformed_params` exists: a client that sends the
        wrong thing is told it sent the right thing, and finds out from the
        behaviour rather than from the answer.
        """
        if len(request) < 2:
            # Not addressable: there is no tag to echo, so the device model
            # answers nothing at all and this must not get there first.
            return request
        name = refdec.OPCODE_NAME.get(request[0])
        wanted = refdec.OPCODE_PARAM_SIZE.get(name)
        if wanted is None:
            return request
        params = request[2:2 + wanted].ljust(wanted, b"\x00")
        return bytes(request[:2]) + params

    def _indulge_aiding(self, request):
        """SPEC.md §14 — a device that takes an aiding request it should refuse.

        Seeded on the REQUEST rather than the response, because every defect
        here is the device agreeing to something: a response-side rewrite would
        report `ok` while the device held no session, and the checks would then
        pass or fail on the follow-up rather than on the refusal.
        """
        if len(request) < 2:
            return request
        if request[0] == refdec.OPCODE["GNSS_AID_BEGIN"]:
            return self._indulge_begin(request)
        return request

    def _indulge_begin(self, request):
        if len(request) != 2 + refdec.OPCODE_PARAM_SIZE["GNSS_AID_BEGIN"]:
            return request
        fmt, total = struct.unpack_from("<BI", request, 2)
        declared = getattr(self.device, "AID_FORMAT", 1)
        if "aid_accepts_undeclared_format" in self.faults and fmt != declared:
            # The wrong AssistNow product, taken without complaint. The device
            # then writes it to a receiver that discards it, and the only
            # symptom is a time to first fix that never improves.
            fmt = declared
        if "aid_accepts_oversized" in self.faults:
            ceiling = getattr(self.device, "AID_MAX_BYTES_DECLARED", None)
            if ceiling is not None and total > ceiling:
                total = ceiling
        return bytes(request[:2]) + struct.pack("<BI", fmt, total)

    def _indulge_obd(self, request):
        """SPEC.md §15.4 — a device that polls whatever it is asked.

        Every PID the device would refuse is rewritten into one its own car
        supports before dispatch, so the request is answered ok and the poll
        loop genuinely runs -- the defect a client meets is a device
        transmitting for a PID it never verified."""
        if len(request) < 5 or request[0] != refdec.OPCODE["OBD_POLL_SET"]:
            return request
        pids = bytearray(request[5:])
        for i, pid in enumerate(pids):
            if not self.device._obd_pid_supported(pid):
                pids[i] = 0x0C
        return bytes(request[:5]) + bytes(pids)

    def _corrupt_response(self, response, request):
        opcode, status = response[0], response[2]
        if "timesync_unsupported" in self.faults and \
                opcode == refdec.OPCODE["TIME_SYNC"]:
            # A device that never implemented §9.5 at all, which is what a
            # client meets on firmware predating it. The detail goes with it:
            # §9 allows one only on ok.
            return bytearray(response[:2]
                             + bytes([refdec.STATUS_VALUE["unsupported_opcode"]]))
        if "monitor_paged_declaration" in self.faults and \
                opcode == refdec.OPCODE["MONITOR_LIST"] and status == 0:
            # The superseded shape: a six-byte page header where §13.3 now
            # defines a two-byte declaration. Every byte after it is unchanged,
            # which is exactly what makes it worth seeding -- the entries still
            # decode, and only the length arithmetic gives it away.
            entries = bytes(response[3 + refdec.size("monitor_declaration"):])
            count = response[3]
            return bytearray(response[:3]
                             + struct.pack("<HHBB", count, 0, count, 0)
                             + entries)
        if "rate_ceiling_ignored" in self.faults and opcode in (
                refdec.OPCODE["GPS_SET_RATE"], refdec.OPCODE["IMU_SET_RATE"]) \
                and status == refdec.STATUS_VALUE["rate_exceeded"]:
            # §9.6 — the ceiling Info publishes, accepted past. The device then
            # runs at a rate it told the client it could not reach, and every
            # buffer the client sized from that ceiling is too small.
            response[2] = refdec.STATUS_VALUE["ok"]
        if "unallocated_opcode_ok" in self.faults and \
                request[0] not in refdec.OPCODE_NAME and \
                status == refdec.STATUS_VALUE["unsupported_opcode"]:
            # §9 — an opcode this version does not define, answered ok. A
            # client cannot then tell a device that implements a later minor's
            # command from one that ignored it.
            response[2] = refdec.STATUS_VALUE["ok"]
        if "obd_probe_unsupported" in self.faults and \
                opcode == refdec.OPCODE["OBD_INFO"]:
            # SPEC.md §15.2 -- the device's own Info says bit 10; refusing the
            # opcode leaves a client no way to tell which statement is true.
            return bytearray(
                response[:2]
                + bytes([refdec.STATUS_VALUE["unsupported_opcode"]]))
        if opcode == refdec.OPCODE["OBD_INFO"] and status == 0:
            base = 3
            if "obd_responded_without_ecus" in self.faults:
                # `responded` kept, the list emptied: the probe says something
                # answered and names nothing that did.
                response[base + refdec.offset("obd_probe", "count")] = 0
                del response[base + refdec.size("obd_probe"):]
            if "obd_entries_descending" in self.faults:
                esz = refdec.size("obd_ecu")
                start = base + refdec.size("obd_probe")
                entries = [bytes(response[start + i * esz:
                                          start + (i + 1) * esz])
                           for i in range(len(response[start:]) // esz)]
                if len(entries) >= 2:
                    response[start:] = b"".join(reversed(entries))
            if "obd_stale_behind_bit" in self.faults:
                # The previous car's masks and request id, behind a cleared
                # bit -- §1.1's stale-value shape, on the record that decides
                # what a client polls.
                response[base + refdec.offset("obd_probe", "validity")] = 0
                response[base + refdec.offset("obd_probe", "count")] = 0
                del response[base + refdec.size("obd_probe"):]
            if "obd_reserved_nonzero" in self.faults:
                struct.pack_into(
                    "<H", response,
                    base + refdec.offset("obd_probe", "reserved_18"), 0xBEEF)
        if "aid_stale_held_until" in self.faults and status == 0 and \
                opcode == refdec.OPCODE["GNSS_AID_INFO"]:
            # SPEC.md §1.1 applied to the one field that decides whether a
            # client sends anything: a device holding nothing, with yesterday's
            # window still in the bytes behind a cleared bit. The client reads
            # a valid window and sends no aiding at all.
            base = 3
            v = refdec.offset("gnss_aid_caps", "validity")
            response[base + v] &= ~(1 << refdec.bit("aid_validity", "held_until")) & 0xFF
            struct.pack_into("<q", response,
                             base + refdec.offset("gnss_aid_caps", "held_until"),
                             1_766_000_000_000)
        if "aid_chunk_exceeds_mtu" in self.faults and status == 0 and \
                opcode == refdec.OPCODE["GNSS_AID_BEGIN"]:
            # A chunk size no client can write. Every chunk is refused by the
            # host stack, and the transfer fails at commit with everything
            # missing rather than at the number that was wrong.
            base = 3 + refdec.offset("aid_begin_result", "chunk_bytes")
            struct.pack_into("<H", response, base, self.mtu + 1)
        if getattr(self, "_aid_reuse_token", None) is not None and \
                status == 0 and opcode == refdec.OPCODE["GNSS_AID_BEGIN"]:
            # The second half of aid_token_reused: the response carries the
            # discarded transfer's token, and the device's own state is set
            # to match, so a stale chunk with that token genuinely lands.
            response[3 + refdec.offset("aid_begin_result", "token")] = \
                self._aid_reuse_token
            if getattr(self.device, "_aid", None):
                self.device._aid["token"] = self._aid_reuse_token
            self._aid_reuse_token = None
        if opcode == refdec.OPCODE["GNSS_AID_COMMIT"] and status == 0 and \
                len(response) >= 3 + refdec.size("aid_commit_result"):
            base = 3
            v_off = base + refdec.offset("aid_commit_result", "validity")
            r_off = base + refdec.offset("aid_commit_result", "result")
            m_off = base + refdec.offset("aid_commit_result", "first_missing")
            missing_bit = 1 << refdec.bit("commit_validity", "first_missing")
            applied = refdec.AID_RESULT_VALUE["applied"]
            incomplete = refdec.AID_RESULT_VALUE["incomplete"]
            bad_crc = refdec.AID_RESULT_VALUE["bad_crc"]
            if "aid_applied_with_missing_index" in self.faults and \
                    response[r_off] == applied:
                # Chunk 0 is a real index, so this tells a client a chunk was
                # lost from a transfer that succeeded.
                response[v_off] |= missing_bit
                struct.pack_into("<H", response, m_off, 0)
            if "aid_reports_first_chunk_missing" in self.faults and \
                    response[r_off] == incomplete:
                # The device knows something is missing and not what. A client
                # resends from 0 every time, so a single lost chunk costs the
                # whole transfer -- which is the cost §14.4 exists to avoid.
                struct.pack_into("<H", response, m_off, 0)
            if "aid_ignores_crc" in self.faults and response[r_off] == bad_crc:
                # The bytes reach the receiver corrupted. Nothing downstream
                # can find this: the receiver either refuses them or takes them
                # and computes from them.
                response[r_off] = applied
                response[v_off] &= ~missing_bit & 0xFF
                struct.pack_into("<H", response, m_off, 0)
        if "no_tag_echo" in self.faults:
            response[1] = (response[1] + 1) & 0xFF
        if "detail_on_error" in self.faults and status != 0:
            response += b"\x00\x00"
        if "duplicate_double_entry" in self.faults and \
                self._pending_dup_unsub is not None and \
                opcode == refdec.OPCODE["CAN_UNSUBSCRIBE"] and \
                status == refdec.STATUS_VALUE["unknown_subscription"] and \
                self._dup_entries.get(self._pending_dup_unsub, 0) > 0:
            # The second entry the duplicate install created, coming out of
            # the table: the device already removed the first, and this
            # removal finds the copy.
            self._dup_entries[self._pending_dup_unsub] -= 1
            response[2] = refdec.STATUS_VALUE["ok"]
        if "unknown_subscription_ok" in self.faults and \
                opcode == refdec.OPCODE["CAN_UNSUBSCRIBE"] and \
                status == refdec.STATUS_VALUE["unknown_subscription"]:
            response[2] = 0
        if "timesync_single_reading" in self.faults and \
                opcode == refdec.OPCODE["TIME_SYNC"] and status == 0:
            struct.pack_into("<Q", response, 3 + 8, *struct.unpack_from("<Q", response, 3))
        if "power_unsupported" in self.faults and \
                opcode == refdec.OPCODE["GET_POWER"]:
            # A device whose Info declares bit 8 and whose control plane has
            # never heard of the opcode that bit owns. The client is left with
            # two statements about the same device and no way to tell which is
            # the true one; the detail goes with the refusal, because §9 allows
            # one only on ok.
            return bytearray(response[:2]
                             + bytes([refdec.STATUS_VALUE["unsupported_opcode"]]))
        if status == 0 and opcode == refdec.OPCODE["GET_POWER"] and \
                len(response) >= 3 + refdec.size("power_state"):
            base = 3
            pbit = refdec.bit("power_validity", "percent")
            if "power_percent_impossible" in self.faults:
                # §9.7 -- 200%. The record is well formed, so it DECODES; the
                # defect surfaces as a value the harness must flag, and a
                # client that clamps it to 100 shows a full battery on a
                # device that has lost track of its own pack.
                response[base + refdec.offset("power_state", "validity")] |= \
                    1 << refdec.bit("power_validity", "percent")
                response[base + refdec.offset("power_state", "percent")] = 200
            if "power_stale_behind_bit" in self.faults:
                # The last good reading left in the bytes behind a cleared bit:
                # §1.1's plausible wrong value, one firmware change away from a
                # client that reads it.
                response[base + refdec.offset("power_state", "validity")] &= \
                    ~(1 << pbit) & 0xFF
                response[base + refdec.offset("power_state", "percent")] = 63
            if "power_declared_but_empty" in self.faults:
                # Well formed, honestly zeroed, and useless: a device that
                # declares the capability and reports nothing valid.
                for name in ("validity", "source", "percent"):
                    response[base + refdec.offset("power_state", name)] = 0
            if "power_reserved_nonzero" in self.faults:
                response[base + refdec.offset("power_state", "reserved")] = 1
        if "opcode_capability_late" in self.faults:
            response = self._corrupt_capability_refusal(response, request)
        if "rate_not_applied" in self.faults and status == 0 and opcode in (
                refdec.OPCODE["GPS_SET_RATE"], refdec.OPCODE["IMU_SET_RATE"]):
            # Answers ok and quietly keeps the rate it had: SPEC.md §9.6's
            # plausible wrong value, where the client believes it asked for
            # something the timestamps then contradict.
            self.device.gps_hz, self.device.imu_hz = self._rates_before
        if "list_reserved_nonzero" in self.faults and status == 0 and \
                opcode == refdec.OPCODE["MONITOR_LIST"]:
            response[3 + refdec.offset("monitor_declaration", "reserved")] = 1
        return response

    async def subscribe(self, uuid, callback):
        uuid = uuid.lower()
        if "inert_cccd_rejected" in self.faults:
            name = refdec.CHAR_NAME.get(uuid)
            spec = refdec.PROFILE_CHARS.get(name)
            if spec and spec["capability"] is not None and not (
                    self._capabilities() & (1 << refdec.CAPABILITIES[spec["capability"]])):
                raise DeviceRefused(f"capability {spec['capability']} not declared")
        self._subs[uuid] = callback

    async def unsubscribe(self, uuid):
        self._subs.pop(uuid.lower(), None)

    # -- the notification pump -------------------------------------------

    _STREAM_CHAR = {"gps": "gps", "can": "can", "imu": "imu"}

    def _corrupt_capability_refusal(self, response, request):
        """SPEC.md §9 -- availability before parameters. This gets it backwards."""
        name = refdec.OPCODE_NAME.get(request[0])
        capability = refdec.OPCODE_CAPABILITY.get(name)
        if capability is None:
            return response
        if self._capabilities() & (1 << refdec.CAPABILITIES[capability]):
            return response
        response[2] = refdec.STATUS_VALUE["bad_params"]
        return response

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
        if "obd_flag_never_set" in self.faults and stream == "can":
            # SPEC.md §15.6 -- the one bit that makes the transmitter
            # observable from the stream, never set.
            off = refdec.offset("can_header", "flags")
            payload[off] &= ~(1 << refdec.bit("can_flags", "polling")) & 0xFF
        if "seq_starts_at_one" in self.faults or "seq_repeats" in self.faults:
            off = refdec.offset(self._SEQ_RECORD[stream], "seq")
            seq = struct.unpack_from("<H", payload, off)[0]
            if "seq_repeats" in self.faults:
                seq = 0
            else:
                seq = (seq + 1) & 0xFFFF
            struct.pack_into("<H", payload, off, seq)
        if "clock_steps_backwards" in self.faults and stream == "gps":
            # SPEC.md §8.1 — a device disciplining to GNSS by stepping its
            # clock rather than adjusting its frequency. Every notification is
            # individually plausible; only the pair reveals it, and a client
            # aligning CAN to GPS across the step gets a wrong answer rather
            # than no answer.
            self._steps = getattr(self, "_steps", 0) + 1
            if self._steps % 2 == 0:
                off = refdec.offset("gps_fix", "t_device")
                t = struct.unpack_from("<Q", payload, off)[0]
                struct.pack_into("<Q", payload, off, max(0, t - 500_000))
        if "clock_per_stream" in self.faults and stream == "imu":
            # One clock per sensor is the mistake §8.1 exists to forbid: each
            # stream looks perfectly self-consistent and no two agree.
            off = refdec.offset("imu_header", "t_base")
            t = struct.unpack_from("<Q", payload, off)[0]
            struct.pack_into("<Q", payload, off, t + 3_600_000_000)
        if "stream_truncated" in self.faults and stream == "gps":
            # SPEC.md §5 — a record is its size. Every other notification, so
            # the run still has well-formed ones for everything downstream of
            # the decode check to judge.
            self._truncations = getattr(self, "_truncations", 0) + 1
            if self._truncations % 2 == 0:
                del payload[-1:]
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
