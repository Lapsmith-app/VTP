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

### The panel is not free

Measured, because it was guessed at wrongly three times first:

| | loop blocked | notifications accepted |
| --- | --- | --- |
| `--no-display` | 0 | **~23.5/s** |
| panel open | 140–320 ms/s | **~15–18/s** |

`root.update()` costs 15–30 ms per call and the peripheral cannot send while it
runs, so the panel costs roughly what it blocks — **20–35% of throughput**. The
drawing itself is not the problem: repainting every value measures 0.6 ms, and
lowering the refresh rate makes it *worse*, because Tk's work is per unit time
rather than per paint and a slower rate merely batches it into fewer, longer
stalls.

Fixing it properly means Tk and asyncio on separate threads, and on macOS both
Tk and CoreBluetooth want the main one. Not worth it for a debug tool, so the
cost is stated instead: **use `--no-display` when measuring throughput**, and
keep the panel for everything else, where 15/s is ample to watch a client
work. The status line reports its own cost every ten seconds so the trade is
visible rather than folklore.

The row that mattered most in practice is **notify subscriptions**. A VTP
device can have CAN ids installed *and* no subscriber on the CAN
characteristic, and it then produces batches that go nowhere — which from the
client side looks exactly like a decode bug. The panel turns that red and says
so. Those are two different subscriptions: the `CAN_SUBSCRIBE` control opcode
says which arbitration ids to forward, and a GATT subscribe says whether the
notifications are carried at all.

**`no-sub`** counts notifications produced for a characteristic nobody has
subscribed to. That is not loss and is not reported in `dropped` — nobody asked
for it. **`refused`** is loss: a subscriber exists and the host stack still
rejected the notification, so the items go into `dropped` per §8.3.

Rates matter more than totals: a total cannot tell a stalled stream from a slow
one, and every stall in this repository's history looked like a large number
that had stopped growing.

A Monitor device exists to display values it cannot compute, so the only way to
tell whether the role works end to end is to look at one. `serve.py` opens a
window showing the channels the device asked for and the values the client
supplied:

```
LAP          LAST         BEST
42.318       1:27.340     —·—

DELTA        LAP No.      SPEED  km/h
+1.250       3            136.8
```

The thing to watch is `—·—`. A slot the client has not supplied, or has
explicitly marked absent, renders as absence and in a dimmer colour — never as
`0.000`. Before the first lap of a session there is no last lap time, and a
display showing `0.000` for it has been told something false. That distinction
is the whole reason `monitor_value` carries a `present` bit (SPEC.md §13.4), and
it is invisible in a log of numbers.

Formatting is where the channel enum earns itself. Each channel has exactly one
unit fixed by §13.2, so the device renders a lap time as `1:27.340` and a speed
as `136.8 km/h` without asking the client anything — no unit negotiation, no
scale factor, no configuration.

```sh
python3 display.py          # the screen alone, no Bluetooth, for a look at it
serve.py --no-display       # headless
```

The window is created **after** the server starts advertising, not before. Tk
takes over the main run loop when it initialises, and CoreBluetooth needs that
run loop to deliver its power-on callback — creating the window first leaves
the server waiting for an event that can no longer arrive, with a window up and
nothing behind it.

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
