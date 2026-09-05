"""One connection to one device, and everything the checks read from it.

The session owns the parts of a conformance run that outlive any single check:
the notification logs that accumulate in the background while the control plane
is being interrogated, and the request/response correlation that SPEC.md §9
puts on the tag.
"""
import asyncio
import re
import dataclasses
import struct
import time

from . import refdec
from .transport import DeviceRefused, TransportError


class ControlTimeout(Exception):
    """A request went unanswered. SPEC.md §9 -- a device MUST respond to every
    request it applies, so this is a finding rather than an error."""

    def __init__(self, opcode, tag, waited, orphans=()):
        super().__init__(
            f"no response to {refdec.opcode_name(opcode)} "
            f"tag {tag} within {waited:.1f}s")
        self.opcode, self.tag, self.waited = opcode, tag, waited
        self.orphans = list(orphans)


#: How a host reports that the device wanted an encrypted or authenticated
#: link and this client had not paired -- three platforms, three wordings.
#: SPEC.md §10 puts pairing on the client and says a client meeting this MUST
#: NOT report the device as faulty, so it is the one ATT refusal on Control
#: that is not a finding about the device.
_NEEDS_PAIRING = re.compile(
    r"insufficient (authentication|encryption)|authentication is insufficient"
    r"|not ?authori[sz]ed", re.IGNORECASE)


class ControlRefusedAtAtt(Exception):
    """A Control write answered with an ATT error, on a device that declares
    `control`.

    The transport reports it as `DeviceRefused`, a `TransportError`, and the
    runner read the first outside device to do this as the link dying and
    ended the run four seconds in (issue #61). The link is up -- the
    transport only says "refused" when it is -- and SPEC.md §9.4 leaves a
    device that declares control no ATT-layer refusal to give, so this is a
    MUST finding about the device, on whichever check wrote the request.

    The message states what was observed and nothing about why: the host
    keeps no ATT error code, so the harness cannot tell a full transmit
    pool from anything else. `needs_pairing` is the one cause it can read,
    from the reason's own words, and it is SPEC.md §10's rather than §9.4's.
    """

    def __init__(self, opcode, tag, reason):
        self.opcode, self.tag, self.reason = opcode, tag, str(reason)
        super().__init__(
            f"{refdec.opcode_name(opcode)} tag {tag} was refused at the ATT "
            f"layer ({self.reason}). SPEC.md §9.4: a device that declares "
            f"control answers every request it reads with a response, held "
            f"until the transport takes it, and never with an ATT error")
        self.evidence = {"att_error": self.reason}

    @property
    def needs_pairing(self):
        """SPEC.md §10 -- the device wants a link this host has not paired."""
        return bool(_NEEDS_PAIRING.search(self.reason))


class ControlEchoMismatch(Exception):
    """A response carried the right tag and the wrong opcode.

    SPEC.md §9 makes the device echo both. Correlation only needs the tag, so
    nothing downstream would notice a mangled opcode -- every check reads
    `status` and `detail` and none of them re-reads the echo. Raised here so
    the one guard covers every opcode rather than each check testing its own.
    """

    def __init__(self, sent, response):
        super().__init__(
            f"wrote {refdec.opcode_name(sent)} and the response carrying "
            f"that tag echoed {refdec.opcode_name(response.opcode)}. "
            f"§9 requires the opcode to be echoed from the request")
        self.evidence = {"sent": refdec.opcode_name(sent),
                         "response": response.raw.hex()}


@dataclasses.dataclass
class Response:
    raw: bytes
    opcode: int
    tag: int
    status: int
    detail: bytes
    t_write: float
    t_recv: float
    decoded: dict = None
    reject_reason: str = None

    @property
    def ok(self):
        return self.status == 0

    @property
    def status_name(self):
        return refdec.STATUS.get(self.status, f"unknown({self.status})")

    @property
    def opcode_name(self):
        return refdec.opcode_name(self.opcode)

    @property
    def round_trip_s(self):
        return self.t_recv - self.t_write

    def detail_as(self, record):
        """Decode the detail with the reference decoder, or raise Reject."""
        return refdec.decode(record, self.detail)

    def describe(self):
        return (f"{self.opcode_name} tag {self.tag} -> {self.status_name}"
                + (f" +{len(self.detail)}B detail" if self.detail else ""))


@dataclasses.dataclass
class Notification:
    payload: bytes
    t_host: float
    index: int


class StreamLog:
    """Every notification seen on one characteristic, in arrival order."""

    def __init__(self, name):
        self.name = name
        self.items = []
        self.subscribed_at = None

    def append(self, payload, t_host):
        self.items.append(Notification(payload, t_host, len(self.items)))

    def __len__(self):
        return len(self.items)

    @property
    def duration_s(self):
        if len(self.items) < 2:
            return 0.0
        return self.items[-1].t_host - self.items[0].t_host

    def since(self, t_host):
        return [n for n in self.items if n.t_host >= t_host]


class ControlClient:
    """SPEC.md §9 -- write requests, correlate the indications that answer them.

    Correlation reads the tag straight out of the response bytes rather than
    from a decoded record, because a response the reference decoder rejects is
    exactly the finding worth reporting and it still has to be attributed to
    the request that provoked it.
    """

    def __init__(self, transport, uuid, timeout=3.0):
        self._transport = transport
        self._uuid = uuid
        self.timeout = timeout
        self.enabled = False
        self._pending = {}
        self._tag = 0
        self.orphans = []
        self.history = []

    async def enable(self):
        """SPEC.md §9.4 -- indications before the first write, always."""
        await self._transport.subscribe(self._uuid, self._on_indication)
        self.enabled = True

    async def disable(self):
        await self._transport.unsubscribe(self._uuid)
        self.enabled = False

    def _on_indication(self, payload, t_host):
        tag = payload[1] if len(payload) >= 2 else None
        entry = self._pending.pop(tag, None)
        response = self._build(payload, entry[1] if entry else t_host, t_host)
        self.history.append(response)
        if entry is None:
            self.orphans.append(response)
            return
        future = entry[0]
        if not future.done():
            future.set_result(response)

    def _build(self, payload, t_write, t_recv):
        base = refdec.size("control_response")
        opcode = payload[0] if payload else -1
        tag = payload[1] if len(payload) > 1 else -1
        status = payload[2] if len(payload) > 2 else -1
        response = Response(raw=bytes(payload), opcode=opcode, tag=tag,
                            status=status, detail=bytes(payload[base:]),
                            t_write=t_write, t_recv=t_recv)
        try:
            response.decoded = refdec.decode("control_response", payload)
        except refdec.Reject as exc:
            response.reject_reason = str(exc)
        return response

    def _next_tag(self):
        for _ in range(256):
            self._tag = (self._tag + 1) & 0xFF
            if self._tag not in self._pending:
                return self._tag
        raise RuntimeError("no free control tag")

    async def send(self, opcode, params=b"", tag=None):
        """Write a request and return the future its response will land in.

        Separate from `request` so a check can hold two requests outstanding
        at once, which is the only way to test what a device does with a
        client that pipelines despite SPEC.md §9's one-outstanding rule.
        """
        tag = self._next_tag() if tag is None else tag
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        t_write = loop.time()
        self._pending[tag] = (future, t_write)
        try:
            await self._transport.write(
                self._uuid, bytes([opcode, tag]) + bytes(params), response=True)
        except TransportError as exc:
            self._pending.pop(tag, None)
            if isinstance(exc, DeviceRefused):
                # The link is up and the device answered: a verdict on the
                # device (SPEC.md §9.4), and the run goes on to the next
                # check. Let through as the TransportError it subclasses, it
                # was the link dying (issue #61).
                raise ControlRefusedAtAtt(opcode, tag, exc) from exc
            raise
        return tag, future

    async def await_response(self, opcode, tag, future, timeout=None):
        timeout = self.timeout if timeout is None else timeout
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            self._pending.pop(tag, None)
            raise ControlTimeout(opcode, tag, timeout, self.orphans[-4:])

    async def request(self, opcode, params=b"", tag=None, timeout=None):
        tag, future = await self.send(opcode, params, tag)
        response = await self.await_response(opcode, tag, future, timeout)
        if response.opcode != opcode:
            raise ControlEchoMismatch(opcode, response)
        return response

    # -- typed helpers ----------------------------------------------------

    async def subscribe_can(self, can_id, mode=0, arg=0, mask=None):
        if mask is None:
            return await self.request(
                refdec.OPCODE["CAN_SUBSCRIBE"],
                struct.pack("<IBH", can_id, mode, arg))
        return await self.request(
            refdec.OPCODE["CAN_SUBSCRIBE_MASK"],
            struct.pack("<IIBH", can_id, mask, mode, arg))

    async def unsubscribe_can(self, can_id, mask=refdec.MASK_EXACT):
        """SPEC.md §9.1 -- removal names the same (id, mask) that installed."""
        return await self.request(
            refdec.OPCODE["CAN_UNSUBSCRIBE"], struct.pack("<II", can_id, mask))


class Session:
    """Everything one connection knows about one device."""

    STREAMS = ("gps", "can", "imu")

    def __init__(self, transport, *, adversarial=True):
        self.transport = transport
        self.adversarial = adversarial
        self.advert = None
        self.info_raw = None
        self.info = None
        self.info_reject = None
        self.capabilities = frozenset()
        self.chars = {}
        self.services = set()
        self.dis = {}
        self.control = None
        self.streams = {name: StreamLog(name) for name in self.STREAMS}
        self.mtu = None
        self.state = {}
        self.notes = []
        self.connect_started = None

    # -- capability helpers ----------------------------------------------

    def has(self, capability):
        return capability in self.capabilities

    def char(self, name):
        return self.chars.get(refdec.CHAR[name])

    def note(self, text):
        self.notes.append(text)

    # -- connection -------------------------------------------------------

    async def open(self, target):
        self.advert = target if hasattr(target, "service_uuids") else None
        self.connect_started = time.monotonic()
        await self.transport.connect(target)
        self.services = set(self.transport.services())
        self.chars = self.transport.characteristics()
        self.mtu = self.transport.mtu

    async def read_info(self):
        """SPEC.md §4 -- read on every connection, never cached across one."""
        self.info_raw = await self.transport.read(refdec.CHAR["info"])
        try:
            self.info = refdec.decode("info", self.info_raw)
        except refdec.Reject as exc:
            # Kept rather than reduced to None: the reason IS the finding. A
            # wrong length is the only way a well-formed read fails to decode;
            # everything else about Info -- the §4.1 matrix included -- decodes
            # and is judged by the info checks.
            self.info = None
            self.info_reject = str(exc)
            self.capabilities = frozenset()
            return
        self.info_reject = None
        caps = self.info["capabilities"]
        self.capabilities = frozenset(
            name for name, bit in refdec.CAPABILITIES.items()
            if caps & (1 << bit))

    async def read_dis(self):
        for name, uuid in refdec.DIS_CHARS.items():
            if uuid not in self.chars:
                continue
            try:
                self.dis[name] = (await self.transport.read(uuid)).decode(
                    "utf-8", "replace").strip("\x00")
            except (DeviceRefused, TransportError) as exc:
                self.dis[name] = f"<unreadable: {exc}>"

    async def start_streams(self):
        """Subscribe to every stream this device declares, as early as
        possible: SPEC.md §8.2 puts seq 0 on the first notification sent after
        the connection, and a harness that subscribes late has thrown away the
        only chance to see it."""
        loop = asyncio.get_running_loop()
        for name in self.STREAMS:
            if not self.has(name) or refdec.CHAR[name] not in self.chars:
                continue
            log = self.streams[name]

            def make(log):
                return lambda payload, t_host: log.append(payload, t_host)

            await self.transport.subscribe(refdec.CHAR[name], make(log))
            log.subscribed_at = loop.time()

    async def start_control(self):
        if not self.has("control") or refdec.CHAR["control"] not in self.chars:
            return
        self.control = ControlClient(self.transport, refdec.CHAR["control"])
        await self.control.enable()

    async def close(self):
        await self.transport.disconnect()
