# Software peripheral

A synthetic VTP/1 device. It presents the GATT service over a host Bluetooth
adapter and streams GPS, CAN and IMU from one monotonic clock, so a client can
be developed and demonstrated before any firmware exists.

```sh
pip install bless
python3 serve.py                  # present it over Bluetooth
python3 selftest.py               # verify it without any Bluetooth at all
```

## Two layers, deliberately

| | |
| --- | --- |
| `vtp_device.py` | The device. One clock, three roles, MTU-aware batching, the control plane. **No Bluetooth dependency.** |
| `serve.py` | The BLE transport, on [bless](https://github.com/kevincar/bless). Thin by design. |
| `selftest.py` | Drives the device and decodes every notification with the reference decoder. |

The split is the point. Everything worth testing is in the first file, so CI
verifies the device on machines with no Bluetooth adapter — and the peripheral
is checked by the same decoder that checks the conformance corpus. A device
emitting bytes that decoder rejects is not a conforming device, and proving
that needs no radio.

`selftest.py` asserts what no single-payload vector can express: that the three
roles share one monotonic clock and their timestamps interleave, that `seq`
advances by one per fix, that a field with no validity bit reads *absent*
rather than zero, that batches respect the negotiated MTU and the 655.35 ms
`dt` window, and that the control plane changes device behaviour rather than
merely answering. Seven seeded device faults are all caught by it.

## What this proves, and what it does not

**Proves:** the GATT layout is implementable; the record formats round-trip
against an independent decoder; the control plane is usable; a client can be
built and demonstrated against it.

**Does not prove:** anything about hardware. SPEC.md §8's clock discipline,
§6.1's timing bounds and the transport requirements of §2.1–§2.3 are all
properties of an MCU and a radio, not of a host operating system's scheduler.
This makes a client developable. It leaves VTP/1 unproven on hardware, which is
still the largest gap in this repository.

## Platform limits

**macOS.** Works, with one hole: CoreBluetooth's peripheral role accepts only a
local name and service UUIDs in an advertisement, so the three-byte Service
Data of SPEC.md §3.3 **cannot be advertised**. A client discovers the device and
must read Info to learn its capabilities — which §3.3 requires regardless,
Service Data being advisory. It is the one part of the specification a Mac
cannot exercise.

macOS also **terminates** a command-line process that creates a
`CBPeripheralManager` without a Bluetooth permission grant — killed outright,
no exception, no stderr, indistinguishable from a hang. Grant Bluetooth access
to your terminal under System Settings → Privacy & Security → Bluetooth.

**Linux.** BlueZ advertises arbitrary Service Data, so §3.3 is reachable, and
`btmgmt` can set the connection parameters and PHY that §2.1–§2.3 ask for and
which no desktop API exposes to an application. A Linux VM with a USB Bluetooth
adapter is a better rig than a Pi and considerably cheaper.

**Windows.** WinRT's `GattServiceProvider` is workable but the least tested of
the three here.

## What building this revealed about the Control plane

Three opcodes are named in SPEC.md §9 with **no response payload defined**, so
this device answers `unsupported_opcode` for all three rather than inventing a
format. A reference implementation that invents wire format creates a de facto
standard by accident, and no decoder in this repository could check it:

- **`CAN_LIST`** — "Response carries the installed subscription table." The
  table's encoding is unspecified. With 32 slots it also cannot fit in one
  indication at the minimum MTU of 100, so it needs pagination or a cursor,
  which is a design decision rather than an oversight to patch.
- **`CAN_SUBSCRIBE_MASK`** — the parameters are specified; the interaction with
  overlapping exact-id subscriptions is not. Which wins, and does unsubscribing
  an id covered by a mask remove it?
- **`LIST_CHANNELS`** — belongs to the Monitor role, which has a UUID and a
  capability bit but no characteristic format and no state machine anywhere in
  the specification.

Two further gaps this device had to make a local decision about:

- **`TIME_SYNC`** returns `t_device` at receipt as a `u64`, which §9 implies
  but does not state as a layout.
- **`rate_exceeded` is not decidable** for an `every_frame` subscription: the
  device cannot know future bus traffic, so it cannot tell at admission time
  whether the subscription would exceed `can_max_frames_per_s`. This device
  admits and then sheds, reporting the loss in `dropped` — which is defensible
  but is not what §9 describes.
