# Software peripheral

A synthetic VTP/1 device. It presents the GATT service over a host Bluetooth
adapter and streams GPS, CAN and IMU from one monotonic clock, so a client can
be developed and demonstrated before any firmware exists.

```sh
python3 selftest.py               # verify the device; needs no Bluetooth at all

./make_macos_app.sh               # macOS: build a bundle that can hold the
open "$PWD/VTPPeripheral.app" \   #        Bluetooth permission (see below)
     --args "$PWD/serve.py"
tail -f /tmp/vtp-peripheral.log

pip install bless && python3 serve.py     # Linux
```

Confirmed working: it advertises the VTP/1 service UUID as `VTP Logger` and
streams GPS, CAN and IMU notifications.

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

macOS also **terminates** any process that creates a `CBPeripheralManager`
without an `NSBluetoothAlwaysUsageDescription` in its `Info.plist` — killed
outright, no exception, no stderr, indistinguishable from a hang. This is *not*
a permission you can grant: the process dies before it can ask, so it never
appears in System Settings → Privacy & Security → Bluetooth, and that pane has
no way to add one by hand.

`make_macos_app.sh` builds a bundle that works. Four things have to be true and
each one fails with the **same misleading message**, so the script documents
each at the point that causes it:

1. A **non-framework** Python. Homebrew's and Apple's are framework builds, so
   the process identifies as `org.python.python` and the wrapper's `Info.plist`
   is ignored entirely. `uv`'s standalone interpreters adopt their bundle.
2. A **self-contained** bundle — interpreter, stdlib and dependencies inside it.
3. Launched with **`open`**. A binary exec'd directly reads the Mach-O's
   embedded `__info_plist` section rather than the file, so a correct bundle
   still dies. (`open <path>`, not `open -a <path>`; the `-a` form wants an
   application name.)
4. A **plain foreground app** — neither `LSBackgroundOnly` nor `LSUIElement`. An
   app that cannot put a window on screen cannot show the prompt either, and
   macOS reports that as "no usage description" rather than "cannot be
   prompted". It costs a Dock icon while running.

**Linux.** BlueZ advertises arbitrary Service Data, so §3.3 is reachable, and
`btmgmt` can set the connection parameters and PHY that §2.1–§2.3 ask for and
which no desktop API exposes to an application. A Linux VM with a USB Bluetooth
adapter is a better rig than a Pi and considerably cheaper.

**Windows.** WinRT's `GattServiceProvider` is workable but the least tested of
the three here.

## What running it revealed

Two bugs the selftest could not reach, which is the argument for doing this at
all rather than trusting a device model:

- CoreBluetooth rejects a notify or write characteristic created with an
  initial value: *"Characteristics with cached values must be read-only"*.
  Only Info may carry one.
- `serve.py` swallowed its own exceptions. Under `open` there is no stderr, so
  every failure looked like a silent exit. It logs tracebacks to the file now,
  which is how the above was diagnosed.

bless also warns that the local name may be truncated because the service UUID
fills the advertisement. Harmless — a client matches on the service UUID — but
it is a live demonstration of why §3.3's Service Data does not fit here.

## The Control plane

Building this device is what produced SPEC.md §9.2-§9.5. Three opcodes were
named with no response payload defined, and the device could not implement
them without inventing wire format — which a reference implementation must
never do, because it creates a de facto standard by accident that no decoder
here can check.

All three are now specified, and this device implements them:

- **`CAN_LIST`** returns a paged `can_list_page` record. Paging is not
  decoration: at the minimum ATT MTU a response carries 97 bytes, of which 3
  are opcode/tag/status and 6 the page header, leaving **six entries** against
  a `can_subscription_slots` that may be far larger.
- **Subscription handles.** An identifier stopped being a unique name for a
  subscription the moment masks existed, so `CAN_UNSUBSCRIBE` takes a handle
  and installs return one. Re-installing the same `(id, mask)` updates in
  place and keeps its handle, so a client reprogramming on every connect
  cannot exhaust the table.
- **Overlap has a rule** (§9.3): most specific mask, then lowest handle, and a
  frame is forwarded at most once. Both terms are visible through `CAN_LIST`,
  so a client can determine which subscription governs rather than discover it.
- **`rate_exceeded` is only claimed where it is decidable** (§9.4). For
  `every_frame` and `on_change` the device cannot know future bus traffic, so
  it admits and sheds, reporting loss in `dropped`. A prediction the device
  cannot make is not a promise the protocol should ask for.

**`LIST_CHANNELS` was removed** rather than specified. It belonged to the
Monitor role, which had a UUID and a capability bit but no characteristic
format and no state machine anywhere in the specification. Whether Monitor
returns is a product question — it lets a client feed its own channels *into*
the device — and it should return as a designed feature or not at all.
