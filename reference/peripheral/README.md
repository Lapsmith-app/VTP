# Software peripheral

A synthetic VTP/1 device: it presents the GATT service over a host Bluetooth
adapter and streams GPS, CAN, IMU and Monitor from one monotonic clock, so a
client can be built and demonstrated before any firmware exists.

It advertises as **`VTP`** on service `56545001-5f05-5b56-af87-dcab2baf2522`.

---

## Running it

### macOS

macOS will not let an ordinary process advertise, so the interpreter has to be
wrapped in an app bundle. Build it **once**:

```sh
./make_macos_app.sh
```

Then run it. Note `open -n` and not plain `open`: without `-n`, macOS activates
an already-running copy and **silently discards your arguments**.

```sh
# with the debug panel — costs ~30% of notification throughput, see below
open -n "$PWD/VTPPeripheral.app" --args "$PWD/serve.py"

# headless — full throughput, use this when measuring anything
open -n "$PWD/VTPPeripheral.app" --args "$PWD/serve.py" --no-display

tail -f /tmp/vtp-peripheral.log
```

Stop it with `pkill -f VTPPeripheral`, or by closing the window.

**Do not rebuild the bundle unless you have to.** It contains only the
interpreter — `serve.py`, `vtp_device.py` and `display.py` are read from the
repository at run time, so editing them needs no rebuild. Re-signing changes the
bundle's code signature, macOS treats it as a different app, and the Bluetooth
permission has to be granted again.

### Linux

```sh
pip install bless
python3 serve.py                  # add --no-display for headless
```

### Without any Bluetooth at all

```sh
python3 selftest.py               # verifies the device against the reference decoder
python3 display.py                # the panel alone, with fake data
```

---

## The panel costs about 30% of throughput

Measured, on a connected client:

| | loop blocked | notifications delivered |
| --- | --- | --- |
| `--no-display` | 0 | **~24/s, 96% of what the device produces** |
| panel open | 140–320 ms/s | **~15/s, around 70%** |

`root.update()` takes 15–30 ms per call and the peripheral cannot send while it
runs, so the panel costs roughly what it blocks. The drawing is not the
expensive part — repainting every value measures 0.6 ms — and lowering the
refresh rate makes it *worse*, because Tk's work is per unit time rather than
per paint and a slower rate batches it into fewer, longer stalls.

**Use `--no-display` for anything where throughput or loss matters.** Keep the
panel for watching a client work, where 15/s is ample. The status line reports
the panel's own cost every ten seconds so the trade stays visible.

---

## Checking it works

The log prints a status line every ten seconds:

```
sent gps=686 can=692 imu=380 | refused gps=21 can=11 imu=25 |
no-subscriber gps=165 can=0 imu=95 | CAN ids=3 |
notify-subscribed: can, control, gps, imu
```

- **`notify-subscribed`** — which characteristics the client has enabled
  notifications on. The single most useful field: a client can install CAN ids
  through the control channel and never subscribe to the CAN characteristic,
  and the device then produces batches that go nowhere. That looks exactly like
  a decode bug from the client side. The log warns when it sees that state.
- **`refused`** — a subscriber exists and the transport still rejected the
  notification. Real loss, counted into `dropped` per SPEC.md §8.3.
- **`no-subscriber`** — produced for a characteristic nobody subscribed to.
  Not loss; nobody asked for it.
- **`CAN ids`** — subscriptions installed by `CAN_SUBSCRIBE`.

Control requests are logged individually by name with their parameters and the
status returned, so a client's whole conversation is visible.

---

## Configuring a client against the test bus

The bus carries three identifiers, 11-bit standard addressing, little-endian.
There is no bus bit rate: VTP/1 does not carry one, and this device has no
transceiver. Full layouts are under
[The synthetic CAN bus](#the-synthetic-can-bus) below.

A client must send `CAN_SUBSCRIBE` for each id before any CAN arrives — the
table is empty on every connection (SPEC.md §9.2).

As a worked example, in LapSmith's pasted-channel format:

```
# name, unit, canId, equation
Engine Speed, rpm, 0x0C0, bytesToUIntLe(raw, 0, 2)
Vehicle Speed, km/h, 0x0C0, bytesToUIntLe(raw, 2, 2)
Gear, , 0x0C0, bytesToUIntLe(raw, 4, 1)
Coolant Temp, degC, 0x0C0, bytesToUIntLe(raw, 5, 1)
Throttle, %, 0x1A0, bytesToUIntLe(raw, 0, 1)
Brake, %, 0x1A0, bytesToUIntLe(raw, 1, 1)
Heading, deg, 0x1A0, bytesToIntLe(raw, 2, 2) * 0.1
Lateral G, g, 0x2E0, bytesToIntLe(raw, 0, 2) * 0.01
Longitudinal G, g, 0x2E0, bytesToIntLe(raw, 2, 2) * 0.01
Yaw Rate, deg/s, 0x2E0, bytesToIntLe(raw, 4, 2) * 0.1
```

Engine speed is the one to check first: it sawtooths 1200–7000 rpm across four
gear changes every 20 seconds, and a wrongly decoded channel does not sawtooth.
Vehicle speed should track the GPS speed exactly, because the CAN value **is**
the derivative of the GPS track. Coolant is a constant 90 °C on purpose; a
frozen value there is not a fault.

---

## Two layers, deliberately

| | |
| --- | --- |
| `vtp_device.py` | The device. One clock, three roles, MTU-aware batching, the control plane. **No Bluetooth dependency.** |
| `serve.py` | The BLE transport, on [bless](https://github.com/kevincar/bless). Thin by design. |
| `display.py` | The device's screen. Pure formatting plus a Tk window, split so CI checks the formatting without a display. |
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

**Do not rebuild the bundle unless you have to.** It contains only the
interpreter; `serve.py`, `vtp_device.py` and `display.py` are read from the
repository at run time, so editing them needs no rebuild. Re-signing changes the
bundle's code signature, macOS treats it as a different app, and the Bluetooth
permission must be granted again. A peripheral that hangs with nothing but
`logging to` in the log is waiting for that prompt. `make_macos_app.sh` refuses
to rebuild over an existing bundle for this reason; `FORCE=1` overrides.

**Linux.** BlueZ advertises arbitrary Service Data, so §3.3 is reachable, and
`btmgmt` can set the connection parameters and PHY that §2.1–§2.3 ask for and
which no desktop API exposes to an application. A Linux VM with a USB Bluetooth
adapter is a better rig than a Pi and considerably cheaper.

**Windows.** WinRT's `GattServiceProvider` is workable but the least tested of
the three here.

## The synthetic CAN bus

Three identifiers, little-endian throughout, and **nothing else exists** — a
subscription to any other id is accepted and yields no frames, because no such
frame is on this bus.

The device streams **no CAN at all until a client subscribes** (SPEC.md §9.2:
the table is empty on every connection). Configuring a channel in a client
should install a `CAN_SUBSCRIBE` for its id; if nothing arrives, check that
first.

### `0x0C0` — engine, 50 Hz

| Bytes | Field | Range |
| --- | --- | --- |
| 0–1 | Engine speed, rpm, `u16` | 1200 – 7000, sawtooths on each gear change |
| 2–3 | Road speed, km/h, `u16` | 67 – 149 |
| 4 | Gear, `u8` | 2 – 4 |
| 5 | Coolant, °C, `u8` | 90, constant |
| 6–7 | padding | zero |

### `0x1A0` — driver inputs, 20 Hz

| Bytes | Field | Range |
| --- | --- | --- |
| 0 | Throttle, %, `u8` | 0 – 100 |
| 1 | Brake, %, `u8` | 0 – 100 |
| 2–3 | Heading, deg × 10, `i16` | 0 – 3600 |
| 4–7 | padding | zero |

### `0x2E0` — chassis, 10 Hz

| Bytes | Field | Range |
| --- | --- | --- |
| 0–1 | Lateral acceleration, g × 100, `i16` | 20 – 97 |
| 2–3 | Longitudinal acceleration, g × 100, `i16` | −38 – +38 |
| 4–5 | Yaw rate, deg/s × 10, `i16` | 59 – 132 |
| 6–7 | padding | zero |

### The values are consistent across channels, deliberately

Speed is `v(t) = 30 + 12·sin(2πt/20)` m/s. Position is its exact integral and
longitudinal acceleration its exact derivative, so the road speed on the CAN
bus is the derivative of the GPS track and the IMU's X axis is the derivative
of that. Lateral acceleration is `v²/r`, so it rises and falls with speed.

A client that cross-checks the three channels finds them agreeing. That is the
property VTP/1 exists to provide, so a test device that faked it would be
testing the wrong thing.

The first version of this circuit ran at constant speed, which made every CAN
value a constant — and a client decoding a channel correctly looked exactly
like one reading a fixed byte offset wrongly. Nothing moving is nothing tested.

## The screen

The window is a debug panel as well as the device's display, laid out around
the questions that actually came up bringing a client onto this protocol:

```
CLIENT CONNECTED        up 4m12s     MTU 247
notify subscriptions:  -gps  +can  -imu  +control

STREAMS
              sent      /s  refused   no-sub  pending drop
gps              0     0.0        0     1126             0
can            634    27.0        0        4             0
imu              0     0.0        0      668             0
configured: gps 10 Hz   imu 100 Hz

CAN SUBSCRIPTIONS
handle  id              mode           arg
1       0x0C0           periodic        40
2       0x1A0           periodic        40
3       0x2E0           periodic        40

CONTROL
19:14:19  CAN_SUBSCRIBE tag=10 id=0x2E0      ok
19:14:19  CAN_SUBSCRIBE tag=9 id=0x1A0       ok

MONITOR
LAP          LAST         BEST
42.318       1:27.340     —·—
```

The thing to watch is **`—·—`**. A Monitor slot the client has not supplied,
or has explicitly marked absent, renders as absence and in a dimmer colour —
never as `0.000`. Before the first lap of a session there is no last lap time,
and a display showing zero for it has been told something false. That
distinction is the whole reason `monitor_value` carries a `present` bit
(SPEC.md §13.4), and it is invisible in a log of numbers.

Formatting is where the channel enum earns itself: each channel has exactly one
unit fixed by §13.2, so the device renders a lap time as `1:27.340` and a speed
as `136.8` km/h without asking the client anything.

### A control response is owed; a notification is only offered

The two are handled differently on purpose, and conflating them cost a client
its connection.

SPEC.md §8.3 says a device discards what it cannot deliver and reports the
count. That is right for a notification: every record carries the time it was
taken, so a late batch misrepresents nothing except by being late, and a
backlog delivered at speed is worse than loss.

SPEC.md §9 says a device MUST respond to every request. There is no discard
option. Dropping a control response is worse than losing data, because the
request has already been **applied** — the device had installed a CAN
subscription and answered `ok`, the answer was refused by the transport and
discarded, and the client sat waiting on its tag until it timed out and dropped
the link. The two ends then disagreed about the subscription table.

Control responses are therefore queued, retried until they land, and sent
**before** notifications each iteration. On a dropped link the queue is cleared
and the count logged: nothing is owed to a client that has gone.

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
