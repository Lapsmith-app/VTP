#!/usr/bin/env python3
"""A fake GATT link, so the transport's state machine is testable.

serve.py's pump is where the peripheral's hardest bugs have lived: sequence
numbers consumed by notifications nobody was subscribed to, a returned number
that a later notification had already taken, and a connection edge handled
after the tick had already sent something for the previous link. None of those
are visible in the device model, none are reachable by a conformance vector,
and reproducing them needed a phone.

This stands in for `bless`'s server across the small surface serve.py actually
uses. It is deliberately not a Bluetooth simulator: it models the four things
that decide the pump's behaviour and nothing else.

  connection      is_connected(), flipped by connect()/disconnect()
  CCCD state      which characteristics a central has subscribed to
  backpressure    update_value() refusing, as CoreBluetooth does when its
                  transmit queue is full
  the wire        every payload the stack accepted, in order

Time is deterministic: the device clock advances one step per tick, driven from
is_connected(), which the pump calls exactly once per tick. Tests therefore
control device time and loop time with one knob and no sleeping.
"""


class FakeCharacteristic:
    def __init__(self, uuid):
        self.uuid = uuid
        self.value = None


class FakeDelegate:
    def __init__(self):
        self._central_subscriptions = {}


class FakeServer:
    """The subset of bless's BlessServer that serve.py touches."""

    def __init__(self, clock, step_us=5_000):
        self._chars = {}
        self._clock = clock
        self._step_us = step_us
        self.peripheral_manager_delegate = FakeDelegate()

        self.connected = False
        # Characteristic name -> list of payloads the stack accepted.
        self.wire = {}
        # When False, update_value refuses, exactly as a full transmit queue
        # does. The pump is expected to hold the payload and retry.
        self.accepting = True
        self.refusals = 0
        # CoreBluetooth calls peripheralManagerIsReadyToUpdateSubscribers when
        # its queue drains, and serve.py hooks it. Without modelling that, a
        # test recovers only via the pump's 250 ms safety timeout -- which
        # makes the result depend on how long the loop happened to take in
        # wall-clock, and a test that depends on wall-clock is a test that
        # reports something other than what it claims to.
        self.on_ready = None

    # -- what serve.py calls ---------------------------------------------

    async def is_connected(self):
        # One tick, one step. The pump calls this once per iteration, which
        # makes device time a function of loop iterations rather than of how
        # fast the test machine happens to be.
        self._clock[0] += self._step_us
        return self.connected

    def get_characteristic(self, uuid):
        return self._chars.setdefault(uuid.lower(), FakeCharacteristic(uuid))

    def update_value(self, _service, uuid):
        if not (self.connected and self.accepting):
            self.refusals += 1
            return False
        char = self.get_characteristic(uuid)
        self.wire.setdefault(self._name(uuid), []).append(bytes(char.value))
        return True

    # -- what a test drives ----------------------------------------------

    def stall(self):
        """The transmit queue is full: every update_value refuses."""
        self.accepting = False

    def drain(self):
        """The queue emptied, and the stack says so."""
        self.accepting = True
        if self.on_ready:
            self.on_ready()

    def connect(self, subscribe=()):
        self.connected = True
        self.peripheral_manager_delegate._central_subscriptions = {}
        for name in subscribe:
            self.subscribe(name)

    def disconnect(self):
        self.connected = False
        self.peripheral_manager_delegate._central_subscriptions = {}

    def subscribe(self, name):
        subs = self.peripheral_manager_delegate._central_subscriptions
        subs.setdefault("central", []).append(self._uuid(name).lower())

    def sent(self, name):
        return self.wire.get(name, [])

    def clear_wire(self):
        self.wire = {}

    # -- naming ------------------------------------------------------------

    _NAMES = {}

    @classmethod
    def bind(cls, char_map):
        """Give the fake the UUID table serve.py is using."""
        cls._NAMES = {k: v for k, v in char_map.items()}

    def _uuid(self, name):
        return self._NAMES[name]

    def _name(self, uuid):
        for k, v in self._NAMES.items():
            if v.lower() == uuid.lower():
                return k
        return uuid
