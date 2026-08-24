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
pip install -r requirements.txt   # pinned; see the note in that file
python3 serve.py                  # add --no-display for headless
```

### Without any Bluetooth at all

```sh
python3 selftest.py               # verifies the device against the reference decoder
python3 transport_selftest.py     # verifies the pump against a fake GATT link
python3 display.py                # the panel alone, with fake data
```

---

## What this backend cannot tell you

**A CoreBluetooth peripheral is never told about a connect or a disconnect.**
The delegate has exactly two central-facing callbacks —
`didSubscribeToCharacteristic` and `didUnsubscribeFromCharacteristic` — and no
connect/disconnect pair at all. bless 0.3.0's `is_connected()` therefore
returns `len(_central_subscriptions) > 0`: *at least one central is subscribed
to at least one characteristic*.

That is the only link-ish signal the platform offers, and it differs from a
real connection in a way you can trip over:

| What happened | What this peripheral sees |
| --- | --- |
| A central connects and subscribes | connected |
| A central unsubscribes from **everything**, link still up | disconnected |
| ...and then resubscribes | a new connection |
| A central disconnects | disconnected |
| The same phone reconnects | a new connection |

Rows two and three are the ones that are not true. The peripheral resets
anyway — sequence numbers restart and the CAN subscription table is cleared —
and that is deliberate rather than an oversight, because the two possible
mistakes are not equal:

- Resetting on a resubscribe costs the client its CAN table and restarts `seq`.
  A client already has to handle both, because that is what every reconnection
  does, and it can see the restart in the next notification.
- *Not* resetting on a real reconnection hands the new connection the old one's
  sequence numbers and subscription table. SPEC.md §8.2 exists so a client
  never has to tell a reconnection from a wrap and §9.1 so it never inherits
  state it did not install; a client cannot detect either failure.

The second is silent and unrecoverable, so the ambiguous case errs towards
resetting. The log says which it probably was: a rising edge from the same
central identity is most likely a resubscribe, a different identity is
certainly a new central. CoreBluetooth keeps `CBCentral.identifier` stable
across connections to the same peer, so identity is a hint for the log and
never a reason to skip a reset.

`reference/peripheral/gattsim.py` can be told to reproduce this
(`bless_semantics=True`), and `transport_selftest.py` pins the behaviour above
so it cannot drift.

**A BlueZ peripheral has a real `InterfacesRemoved` signal** and does not need
any of this. If you port `serve.py` to it, feed that edge straight into
`ConnectionTracker.update()` and the names mean what they say.

---

## The real-radio smoke test

Everything else in this repository — the conformance corpus, `selftest.py`,
`transport_selftest.py` — runs with no Bluetooth adapter. That covers the
protocol thoroughly and covers **the radio not at all**. `smoketest.py` is the
missing half: a real client, over a real link, checking the things a fake link
cannot reach.

It needs **two machines**, because a host adapter cannot usefully scan for a
peripheral it is itself presenting.

Both commands are run from **this directory**, so the install and the script
agree about where they are:

```sh
cd reference/peripheral

# machine A — the device.
#   Linux:
python3 serve.py --no-display
#   macOS: it must run from the app bundle, or the OS kills it before it can
#   ask for Bluetooth. `open -n`, not plain `open` — see "Running it" above.
./make_macos_app.sh          # once
open -n "$PWD/VTPPeripheral.app" --args "$PWD/serve.py" --no-display

# machine B — the client
pip install -r requirements-client.txt      # bleak, the central-role library
python3 smoketest.py
```

**Pair the two machines first on Linux and Windows.** `serve.py` defaults to
requiring an encrypted link on everything except Info (SPEC.md §10). macOS
pairs on demand when an encrypted characteristic is first touched; BlueZ and
WinRT do not — they answer *Insufficient Authentication* and bleak raises. So:

```sh
bluetoothctl pair <address>     # Linux
# Windows: Settings > Bluetooth & devices > Add device
```

`smoketest.py` recognises that error and says this rather than reporting a
protocol fault. For a first bring-up, `serve.py --encrypt none` removes the
question entirely.

Note that `mtu_size` on BlueZ reports the ATT default of 23 rather than the
negotiated value, so the smoke test says the MTU floor went unchecked on Linux
instead of failing a healthy link. The notification-size checks still run, and
they are the ones that matter.

What it checks, and why each one needs hardware:

| Check | Why a fake link cannot reach it |
| --- | --- |
| Discovery by service UUID | The advertisement has to fit in 31 bytes and actually be broadcast (§3.3) |
| Info decodes and satisfies §4.1 | Nothing else reads Info off a real characteristic |
| Negotiated ATT MTU ≥ 100 | §2's floor is a property of the two stacks, not of this code |
| No notification exceeds the negotiated ATT payload | §2 — the link bounds the notification, and only a real link has one |
| An indication arrives on Control | §9's response path is a CCCD write and an ATT indication |
| `TIME_SYNC` returns `t_device_tx ≥ t_device_rx` | §9.5, measured across a real round trip |
| Every stream decodes with the reference decoder | The bytes have crossed a radio |
| Device timestamps advance, and the streams overlap | §8.1's one clock, observed in real time |
| `seq` restarts at 0 on the second connection | §8.2 — needs a link that genuinely dropped |

A sequence *gap* is reported but is not a failure: §8.2 makes a gap the
transport losing what the device sent, which is a fact about the link and the
distance between the two machines rather than a device fault. A *repeat* is a
failure.

`--no-reconnect` skips the second connection, and says so — §8.2's
per-connection restart then goes unchecked.

**This has not been run yet.** The script's decode and inspection half is
exercised by `selftest.py` against the software device's own output, including
a deliberately failing case, so it is known to work on known-good input. Its
BLE half has never met an adapter. That is the single largest gap in this
repository and the README says so on purpose.

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

### What that looks like from the client

The same build, same Mac, same iPhone, four CAN-subscribed streams, differing
only in whether the panel was open:

| | panel open | `--no-display` |
| --- | --- | --- |
| CAN notifications refused by the stack | **40%** of attempts | **2%** |
| batches/s the client received | 9.1–9.4 | 9.7–9.8 |
| frames/s the client received | 58–63 | **71–72** |
| frames per batch | 6.4–6.7 | 7.3–7.4 |

The frame rate is the part worth understanding, because it is not loss:
`dropped` was 0 and there were no sequence gaps in either column. The bus here
is synthetic and `_due_can_frames` emits per poll, so a 74 ms stall in the loop
produces one 50 Hz frame where three were due. Frames never generated are never
accepted, so §8.3 correctly does not count them — the panel does not make the
device lose data, it makes the device *have less data*, and a client cannot
tell those apart from the wire.

**This is also how a panel stall used to reach a client as a fault.** Until the
pump retried a refused notification instead of discarding it, each of those
refusals threw a whole batch away and counted its frames under §8.3 — so the
same run reported 26% of frames dropped and a client stepped down from
`every_frame` to sampling. The panel was the congestion; the discard was what
turned congestion into reported loss. Both are fixed, and the 2% column is what
the link was always capable of.

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
table is empty on every connection (SPEC.md §9.1), and it must **enable
indications on Control before its first write** (SPEC.md §9.4). A write that
arrives before then is discarded *unapplied* and logged as such: the response
would have nowhere to go, and a device that applied it anyway would leave the
two ends disagreeing about the table.

**By default every characteristic except Info requires an encrypted link**, so
the first connection raises a pairing prompt. SPEC.md §10 leaves that choice to
the device and requires every *client* to support all of them, so `--encrypt`
selects which posture this device presents — see [Pairing](#pairing).

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

## Pairing, and the three encryption postures

SPEC.md §10 requires no device to encrypt anything, and requires every client
to cope with one that does. So the interesting thing to test is not whether a
*device* encrypts — it is whether a **client** still works against each posture
a device is allowed to present. This peripheral can present all three:

| `--encrypt` | Protected | What it tests |
| --- | --- | --- |
| `all` *(default)* | Everything but Info | The client pairs and then works on every characteristic |
| `control` | Control only | The common-but-incoherent arrangement §10.2 warns about |
| `none` | Nothing | The client does not *require* encryption either |

### What the posture actually enforces

**The table above is what this peripheral asks for, not everywhere what it
gets.** bless 0.3.0 — the pinned backend, and the newest that exists —
translates the encryption permission only for some characteristics, and the
rest are served in clear with no error anywhere in the sequence. The peripheral
names the gap in a `NOT ENCRYPTED on this backend` warning at startup; these
are the facts behind it, read from the backend sources:

| Backend | Encryption is applied to | So `--encrypt all` leaves in clear |
| --- | --- | --- |
| CoreBluetooth (macOS) | reads and both write forms | `gps`, `can`, `imu` — CB permissions do not govern notification delivery |
| BlueZ (Linux) | `read` and `write` only | `gps`, `can`, `imu`, `aiding` — notify and write-without-response keep their plain flags |
| WinRT (Windows) | nothing | everything — its permission word is shifted past the bit it tests |

`--encrypt control` is fully enforced on macOS and Linux, because Control
carries the one property both translate.

This does not change what SPEC.md §10 permits or what a client must support; it
changes what this peripheral can be used to demonstrate. Treat `--encrypt all`
here as a statement of intent rather than as a control, and do not use it to
conclude anything about a client's behaviour against a genuinely encrypted
device. `reference/peripheral/selftest.py` asserts the table above, so if a
future bless closes the gap the assertions fail rather than the documentation
going quietly stale.

A client that passes all three supports encryption without requiring it, which
is what §10 asks of it. Info stays readable in every posture (§10.2) so a
client that cannot pair can still identify the device rather than reporting it
as broken.

Run each in turn:

```bash
open -n "$PWD/VTPPeripheral.app" --args "$PWD/serve.py" --no-display --encrypt all
open -n "$PWD/VTPPeripheral.app" --args "$PWD/serve.py" --no-display --encrypt control
open -n "$PWD/VTPPeripheral.app" --args "$PWD/serve.py" --no-display --encrypt none
```

The log names the posture at startup, and `notify-subscribed:` names the
characteristics the client actually got onto — which is how you tell "the
client paired and proceeded" from "the client was stopped at the gate".

If a client cannot pair at all, forget the device on it first. A stale bond
against a peripheral that has since restarted with a new identity produces
repeated authentication failures that look exactly like a device fault, and it
is the single most common cause. `--encrypt none` then establishes whether
pairing is the problem or something else is.

macOS in the *peripheral* role is a much thinner path than in the central role.
Just Works pairing initiated against a Mac acting as a peripheral does work —
verified against LapSmith on iOS, which raised a prompt, paired, and then wrote
to Control successfully.

## Device Information Service

The peripheral also exposes the standard Device Information Service (`0x180A`)
with manufacturer, model, firmware revision and serial number, which SPEC.md
§3.4 recommends. Nothing in VTP/1 reads it — it is there because it is where
every generic BLE tool already looks when someone asks what a device is.

## A reconnect faster than one poll is invisible

Connection state is discovered by polling `is_connected()` once per tick, and
`ConnectionTracker` compares the current answer with the last one. A central
that drops and reconnects **between two polls** therefore produces no edge at
all: `on_connect()` never runs, so the device does not restart its sequence
numbers or clear its subscription table for what is, to it, a new client.

At the default 200 Hz that window is 5 ms, and no BLE stack completes a
disconnect and a fresh connection inside it — the supervision timeout alone is
orders of magnitude longer. So this is not reachable on hardware. It is
recorded because it is a property of *how* the state is discovered rather than
of the timings that make it safe, and a future change to either would need to
know it is there. A stack that reports connection edges as events rather than
as a level would not have it.

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

Three broadcast identifiers, little-endian throughout, plus the diagnostic
side below — a subscription to any other id is accepted and yields no
frames, because no such frame is on this bus.

The device streams **no CAN at all until a client subscribes** (SPEC.md §9.1:
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

### The diagnostic side (SPEC.md §15)

The synthetic car also answers J1979 Mode 01, through two ECUs on 11-bit
functional addressing:

| ECU | Answers on | Supports |
| --- | --- | --- |
| Engine | `0x7E8` | most of Mode 01, including `0x0C` rpm, `0x0D` speed, `0x05` coolant, `0x11` throttle |
| Transmission | `0x7E9` | a small subset: `0x01`, `0x0C`, `0x0D`, `0x11` and the mask PIDs |

Nothing appears on these identifiers until a client drives the role:
`OBD_INFO` probes (the mask responses `41 00 …` cross the bus, in J1979's own
MSB-first bit order — the probe *detail* carries SPEC.md §15.3's LSB-first
order, and the transcription between them is deliberate), and `OBD_POLL_SET`
starts the loop. While the poll set is non-empty the answers are delivered
with nothing subscribed — SPEC.md §15.5's fallback, on exactly the response
identifiers the probe reported — and any subscription a client does install
on those identifiers governs its frames first, `periodic` included. The
request frames never appear (§15.5), and `can_header.flags` bit 1 is set on
every batch flushed while the poll set is non-empty. The values derive from
the same motion state as everything else: PID `0x0C` decodes to the same rpm
`0x0C0` carries.

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
id              mode           arg
0x0C0           periodic        40
0x1A0           periodic        40
0x2E0           periodic        40

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

Building this device is what produced SPEC.md §9.1-§9.4. Opcodes had been
named with no response payload defined, and the device could not implement
them without inventing wire format — which a reference implementation must
never do, because it creates a de facto standard by accident that no decoder
here can check.

The rules that survived into the slimmed control plane, all implemented here:

- **A subscription is named by its `(id, mask)` pair** (§9.1). An identifier
  stopped being a unique name the moment masks existed, so `CAN_UNSUBSCRIBE`
  takes the pair, and re-installing the same pair updates in place — a client
  reprogramming on every connect cannot exhaust the table. (Pre-1.0 drafts
  solved this with device-assigned handles and a paged `CAN_LIST` read-back;
  both were removed, and RATIONALE §8.7 records why.)
- **Overlap has a rule** (§9.2): most specific mask, then earliest installed,
  and a frame is forwarded at most once. Both terms are known to the client,
  because it installed the table on this connection.
- **CAN subscriptions are never refused on rate grounds** (§9.3). The device
  cannot predict the load one adds — not from bus traffic, not across modes
  that select identically, and not for a mask that schedules per identifier —
  so it admits and sheds, reporting loss in `dropped`. `rate_exceeded` survives
  only for `GPS_SET_RATE` and `IMU_SET_RATE`, where the limit is the device's
  own and the answer is a fact rather than a forecast.

**`LIST_CHANNELS` was removed** rather than specified. It belonged to the
Monitor role, which had a UUID and a capability bit but no characteristic
format and no state machine anywhere in the specification. Whether Monitor
returns is a product question — it lets a client feed its own channels *into*
the device — and it should return as a designed feature or not at all.
