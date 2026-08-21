# Why VTP/1 is shaped this way

This document is **non-normative**. [SPEC.md](SPEC.md) is the specification;
this explains the reasoning behind it, so that anyone proposing a change can see
which constraint each decision is answering.

---

## 1. The constraint that shaped the previous generation

When [RaceChrono's DIY BLE API](https://github.com/aollin/racechrono-ble-diy-device)
was designed, a BLE notification carried **20 bytes**. That was the default ATT
MTU of 23 minus three bytes of header, and on the hardware of the day it was not
a default you could negotiate away — early Android stacks, early iOS versions and
the peripheral silicon of the era all sat at or near it.

Twenty bytes for a complete position solution is a severe budget. Every
awkwardness described below follows from it, and each was a reasonable trade at
the time. That API has served its ecosystem for the better part of a decade,
which is a longer run than most protocols manage and is worth more than any
individual improvement listed here.

**What changed is the budget, not the judgement.** A negotiated ATT MTU of 185
is universal on mainstream phones today and 247 is common. VTP/1 is what the
same problem looks like when a notification carries roughly twelve times as much.

## 2. What the budget forced, and what changes when it lifts

### 2.1 Absence had to be encoded inside the value

With 20 bytes there is no room for a validity mask, so "no data" has to be a
reserved value of the field itself: latitude `0x7FFFFFFF`, altitude `0xFFFF`,
HDOP `0xFF`, satellite count `0x3F`.

Every one of those is a value the field could otherwise hold — `0x7FFFFFFF`
decodes to 214.7°, `0xFFFF` altitude to 6053.5 m, `0xFF` HDOP to 25.5. A missed
check therefore produces a plausible number rather than an error, and a
plausible number survives review.

There is a second-order effect that is easy to miss: a bit-packed layout has no
room to say what an *unpopulated* field must contain either. Firmware that
zero-fills is as conforming as firmware that writes the sentinel, and both exist
in the field. A robust client ends up treating HDOP `0` as absent too — a
dilution of precision is a geometric ratio that cannot fall below 1.0, so `0.0`
is not an extraordinarily good fix, it is a field nobody wrote. A fix-quality
grader that trusts it will grade a sample carrying no satellite count and no
accuracy figure as usable, on the strength of a byte nobody wrote.

**VTP/1:** a 32-bit validity bitmask, and a hard rule that a field whose bit is
clear MUST be zero and MUST be reported absent. No value anywhere means "no
data", so there is no check to forget. This costs four bytes and removes an
entire category of silent failure, which is the trade the whole protocol is
built on.

### 2.2 A fix did not fit, so it was split in two

Position plus a full timestamp does not fit in 20 bytes. The previous generation
splits it: one characteristic carries position and the minute/second/millisecond
within an hour, another carries the year, month, day and hour, and a 3-bit
counter stamped into both matches them up.

That works, and it costs a read of the second characteristic at connect and
again whenever the counter disagrees, one dropped fix per hour boundary, and a
rule that the cached hour must be discarded on disconnect — the counter restarts
wherever the firmware left it, so equality across a reboot is coincidence rather
than a match. All of that machinery buys five bytes.

The timestamp itself is packed into 21 bits as 2 ms ticks, which is a genuinely
clever fit and also a trap: read as milliseconds it yields a clock running at
exactly half speed, and half speed is plausible.

**VTP/1:** one 74-byte record with an absolute microsecond device timestamp and
an absolute UTC timestamp. No second characteristic, no counter, no rollover
case, no tick unit to misread.

### 2.3 Range and resolution had to be traded against each other

Sixteen bits cannot hold altitude at 0.1 m resolution over the range a car might
see, so the previous generation defines two equations per field and puts a mode
flag in bit 15 — one equation fine and narrow, the other coarse and wide, for
both altitude and speed.

This is the compression that best illustrates the cost. The published fine
ranges are `[-500, +6053.5]` m and `[0, 655.35]` km/h. Both are `0xFFFF`-derived,
but each equation masks to `0x7FFF`, so a fine value can never exceed 15 bits:
the actual fine ceilings are **2776.7 m** and **327.67 km/h**. An implementation
that trusts the published figures builds a decoder that never selects the coarse
branch when it should, and a device stays conforming right up to the altitude
where its fine values silently wrap.

An encoding compressed enough to produce a factor-of-two error in its own
normative text is an encoding that is costing more than it saves.

**VTP/1:** one linear encoding per field, sized to the range. Altitude is a
signed 32-bit millimetre count. No mode flags, no dual equations, nothing to
select between.

### 2.4 One frame per notification was the only option

A CAN frame is 4 bytes of identifier and up to 8 of payload. In a 20-byte
notification, one frame fits and a second does not.

BLE throughput is bounded by **notifications per connection event**, not by
bytes. iOS typically grants about four per 15 ms event, which puts a hard ceiling
near 270 frames per second regardless of MTU. A 500 kbit/s bus running 8-byte
frames carries roughly 4,000 — so the framing forwards about 7% of a busy
chassis bus, and the per-id notify intervals that look like a feature are really
a rationing scheme the framing made necessary.

**VTP/1:** batched frames. At a 247-byte MTU a notification carries a 16-byte
header and fifteen classic frames, moving the constraint from notifications to
bytes and the ceiling to roughly 4,000 frames per second. The per-id rate control
is kept, because it is genuinely useful — it is just no longer load-bearing.

### 2.5 There was no room for a timestamp on a CAN frame

So the client stamps arrival, which folds in BLE stack latency and
connection-event jitter — tens of milliseconds, unbounded at the top end, and
systematically worse under load, which is exactly when the data matters.

The deeper consequence is that GPS time and CAN time have no defined
relationship at all. One carries a UTC-derived timestamp, the other carries
nothing. For an application whose entire question is *where was the car when the
driver did that*, there is no answer better than arrival order.

**VTP/1:** every frame carries a 10 µs offset from a batch base timestamp,
measured by the device at bus arrival, on the same monotonic clock as the GPS
fix and the IMU sample. Cross-channel alignment becomes arithmetic. This is
arguably the single most valuable change in the protocol, and it is the one that
has nothing to do with speed.

### 2.6 Loss had nowhere to be reported

Notifications get dropped — peripheral buffer overrun, missed connection events.
With no spare bytes there is no sequence number and no drop counter, so a client
cannot distinguish "the bus went quiet" from "I lost four hundred frames", and
there is no backpressure signal in either direction.

**VTP/1:** every stream carries a sequence number and a count of what the device
discarded since the last notification, plus a flag for active load shedding.
Loss becomes a number a user interface can show.

### 2.7 Capability had to be discovered by connecting

The service UUID is advertised identically by a CAN-only board, a GPS-only board
and one doing both, and reference firmware registers each characteristic pair
only when its feature is compiled in. So answering "what is this device?"
requires connecting and enumerating the GATT table.

Worse, the answer cannot be cached: a DIY board is reflashed by its owner, and a
build that drops GPS changes the answer without changing the device's Bluetooth
identity. Every pairing pays a connect, an MTU negotiation and a service
discovery to learn three bits.

**VTP/1:** capability bits and the minor version go in the advertisement, and a
24-byte Info characteristic carries the full picture. The Info read is still
per-connection and still uncacheable — that part is inherent to reflashable
hardware, not a protocol defect — but scanning, labelling and ranking no longer
require a connection.

### 2.8 Capacity was never expressed, so clients guessed

Nothing in the previous generation lets a client ask how many filter
subscriptions the device will hold before silently discarding one, what
aggregate frame rate it will sustain, what GPS rate it is producing, or whether
it speaks CAN FD. Filters are write-only: there is no read-back, no per-command
result, and no error when the table is full. An ATT acknowledgement confirms
that bytes arrived, not that a subscription exists.

The practical consequences are that clients import capacity limits from
unrelated hardware — a well-known client caps enabled CAN messages at twelve, a
number inherited from the filter-slot count of an OBD adapter that has nothing
to do with a BLE module — and that the only safe strategy is to reprogram
everything unconditionally on every connect, because a module that browned out
mid-session comes back promiscuous or deaf and both are silent.

**VTP/1:** the Info characteristic declares slots, rate ceilings and payload
support; the control channel is tagged request/response with typed failures
(`table_full`, `rate_exceeded`); and `CAN_LIST` returns the installed table so a
client can verify state rather than assume it.

### 2.9 Motion data went somewhere else entirely

The previous generation's BLE API has no accelerometer or gyroscope channel, so
a board with an IMU on it — which is most of them — reaches for a second,
unrelated protocol: an ASCII, NMEA-shaped stream over Bluetooth RFCOMM or
TCP/IP. Different transport, different data model, no CAN, and on iOS the RFCOMM
lane is closed to non-MFi hardware, which pushes home-built boards to TCP/IP.

This is not really a design decision; it is two protocols that grew up
separately. But the effect is that the three things a DIY logger has cannot
share a link or a clock.

**VTP/1:** IMU is a first-class role on the same service, batched, timestamped
against the same clock. At 200 Hz it costs about 2.4 kB/s and eleven
notifications per second — effectively free on a link that can carry 60.

### 2.10 Versioning had no room either

There is no version field anywhere. When a Monitor API was added in a later app
release, the only way for a client to detect it was to look for the
characteristics. Forward compatibility was left to convention.

**VTP/1:** major versions get separate service UUIDs, so an unsupported major is
something a client never discovers rather than something it half-parses. Minor
versions are additive by construction — frozen record sizes, length-prefixed
extension records, reserved bits — rather than by asking implementers to be
careful. SPEC.md §11 carries the rules and the prohibitions.

## 3. What was already right, and is kept

Four decisions carried over unchanged, because they are correct and not obvious:

**Equations, not DBC.** A CAN channel is a `(packet id, equation over raw
bytes)` pair. DBC files for road cars are proprietary and mostly unobtainable,
whereas an equation is a line of shareable text that a user can reverse-engineer
one signal at a time and post on a forum. This is the decision that made a
community possible, and VTP/1 does not touch it.

**The device is a dumb pipe.** Bus bit rate, 11- versus 29-bit addressing and
transceiver configuration never appear in the protocol; the arbitration id
arrives already resolved. This is why one client entry can serve modules from
several vendors.

**Per-id rate limiting in the device.** Bandwidth bounded by an interval the
device enforces, per identifier, expressed in milliseconds. Strictly better than
hardware filter slots. VTP/1 generalises it with on-change and every-Nth modes
rather than replacing it.

**Publishing it and then leaving it alone.** Stability is the feature that
created the ecosystem. Any successor inherits that obligation, which is why
SPEC.md §11 spends more words on what may never change than on what may.

## 4. What VTP/1 costs

Stating these plainly, because a rationale that only lists benefits is marketing:

- **A fix is 74 bytes instead of 23.** At 25 Hz that is 1.85 kB/s, under 3% of an
  achievable link — but it is not nothing on a congested one.
- **A minimum ATT MTU of 100.** Hardware that genuinely cannot negotiate past 23
  cannot implement VTP/1 at all. This is a deliberate exclusion of pre-2014
  silicon.
- **Batching adds latency.** A frame waits for its batch to fill or for the flush
  timer. Flushing once per connection interval makes this a wash, but for a
  single slow signal on an idle bus, VTP/1 delivers marginally later. The win is
  throughput and timestamp accuracy, not first-byte latency.
- **More surface to get wrong.** Six characteristics, three batch formats and an
  extension mechanism is more specification than a single 20-byte struct. The
  conformance corpus exists because that is a real cost and it needs paying down
  with tests rather than with prose.
- **Zero deployed devices.** Which is the largest cost by a wide margin, and the
  one nothing in this document addresses.
