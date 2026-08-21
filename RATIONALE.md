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

## 4. Coexistence with other devices on the same radio

A VTP/1 device is rarely the only thing a phone is talking to. A realistic
motorsport roster adds a heart-rate strap, an OBD adapter and one or more
action cameras whose control channel is latency-sensitive exactly while
recording. "Higher throughput" is only an improvement if it does not buy itself
that headroom out of everyone else's.

### 4.1 The shared resource is airtime, not bytes

A central serves every connection from one radio, time-division. What a
connection consumes is **occupied radio time**, and that is a function of packet
count as much as payload size, because every packet carries fixed overhead that
does not shrink with its contents.

On the 1M PHY a packet occupies `10 + payload` bytes on air at 8 µs per byte,
followed by an inter-frame space, the peer's acknowledgement and a second
inter-frame space — about 380 µs of fixed cost per packet regardless of size.
A full 251-octet packet therefore costs ≈ 2.5 ms; a 27-octet one costs ≈ 0.7 ms
while carrying a ninth of the data. This is the arithmetic behind SPEC §2.1, and
it is why an ATT MTU negotiated without a matching link-layer payload is a
throughput figure that does not exist.

Taking the roles at plausible rates, encrypted, 1M PHY, link-layer payload
extended:

| Stream | Rate | Notifications/s | Share of radio time |
| --- | --- | --- | --- |
| GPS | 25 Hz | 25 | ~3% |
| IMU | 200 Hz, batched | ~11 | ~3% |
| CAN | ~400 frames/s | ~29 | ~7% |
| CAN | 4 000 frames/s | ~286 | ~67% |

The 2M PHY roughly halves the payload component of each figure, though not the
per-packet overhead.

Two things fall out of that table. GPS and IMU are irrelevant to congestion —
together they are a rounding error, which is what §2.9 meant by calling IMU
"effectively free". And **CAN is the entire story**, with a range spanning an
order of magnitude between a sane subscription set and the ceiling.

### 4.2 The protocol did not raise the risk; it removed a cap

The previous generation was self-limiting. One frame per notification against a
per-connection-event notification budget capped it near 270 frames per second
(§2.4) — not by design, but as a side effect of the framing. A client could not
congest a radio with it because it could not go fast enough to try.

VTP/1 removes that accident. It does not *cause* the top row of that table; it
*permits* it. Whether a device sits at 7% or 67% is decided by the subscription
set the client installs, not by the wire format. That is the correct place for
the decision to live, and it is a decision the previous generation gave nobody
the means to make deliberately.

### 4.3 What the protocol does to help

Three properties make VTP/1 a better citizen than what it replaces, at equal
data rates:

**Fewer, fuller packets.** Batching is a coexistence feature before it is a
throughput feature. The previous generation carried one frame per notification;
the same frames delivered in a fourteenth as many notifications pay a
fourteenth as much per-packet overhead, so batching *reduces* occupied airtime
for a given frame rate.

**Fewer links.** GPS, CAN and IMU on one connection replaces what previously
took a BLE link for CAN and a separate transport for motion data (§2.9). Every
concurrent link costs connection events, scheduling slots and a share of the
central's attention whether or not it carries traffic, so removing links is a
larger and more certain win than any per-byte efficiency. This holds regardless
of how many connections a given central can actually sustain — a number that is
controller-dependent, undocumented on at least one major platform, and which
this specification therefore declines to quote.

**Congestion is observable.** `dropped`, `seq` and the load-shedding flag (§2.6)
mean a saturated link reports itself instead of quietly losing frames. A client
that watches them can unwind subscriptions in response to measured congestion
rather than guessing in advance.

### 4.4 What the protocol deliberately does not do

VTP/1 has no concept of a link budget, and `can_max_frames_per_s` is not one. It
is a statement about what the *device* can produce; `rate_exceeded` protects the
device's own pipeline, not the radio's schedule. The device cannot know what
else the central is serving, so it cannot be the component that decides.

That leaves the client owning the budget, with the per-id subscription modes,
`GPS_SET_RATE` and `IMU_SET_RATE` as the instruments. Those are sufficient to
throttle — the loop from `dropped` and the shedding flag back to a lower rate is
closed without adding anything. What is missing is only ergonomics: there is no
single aggregate throttle, so a client reacting to congestion unwinds
subscriptions individually.

An opcode expressing a total rate ceiling would close that, and is deliberately
absent from 1.0 for two reasons. The right shape is not yet known, and inventing
capacity semantics before any hardware exists is the mistake §2.8 is about. More
importantly, a device that sheds to fit a budget has to decide *what* to shed,
and that is policy — the client knows whether the user is watching a lap timer
or logging a session, and the device cannot. A protocol that moves that decision
into the device stops being a dumb pipe (§3).

`GET_LINK_PARAMS` (SPEC §9.1) is the half that *was* worth adding, and it points
the other way: not another actuator, but the one measurement a client cannot
take for itself. Negotiated link-layer payload, PHY and connection interval are
not exposed to applications on at least one major mobile platform, so without
the device reporting them a client cannot tell a well-configured device from one
quietly costing three times the airtime for the same data. Sensing was the gap;
actuation already existed.

It has no `capabilities` bit, which is a deliberate asymmetry with
`masked_subscriptions` and `on_change_subscriptions`. Those bits earn their
place because a client must know before it composes a subscription plan;
learning the answer by being rejected means unwinding work already done.
`GET_LINK_PARAMS` has no plan behind it. A bit could not carry the values, so a
client that cares issues the opcode either way — and the response already says
whether it is supported. The bit would save no round trip, change no behaviour,
and introduce a second source of truth needing a precedence rule for the case
where it disagrees with the response. For a feature whose whole purpose is
verifying rather than assuming, an advertised claim is the wrong shape; §2.8's
answer to the same problem was `CAN_LIST` reading real state back, not another
bit. `control` already tells a client whether there is a control channel at all,
which is the part worth knowing before connecting.

## 5. Why most records are closed

Only `gps_fix` carries an extension trailer. Every other record is fixed for the
life of major version 1, and SPEC.md §11.3 says so rather than promising otherwise.

### 5.1 The decision could not be deferred

A conforming receiver rejects a payload whose length it does not expect. That
rule is what makes "malformed is rejected whole" mean anything, and it has a
consequence that is easy to miss: a trailer introduced in a later minor is
rejected by every client already deployed. The first device to send one simply
stops working with the installed base.

So extensibility is not a thing a protocol can add when it turns out to need it.
It is decided before the first release or it is decided against. Recognising
that is most of the answer, because it converts an open-ended "should we allow
for the future" into a concrete question with a price attached.

### 5.2 The price is paid per appearance

A trailer costs one byte on the record that carries it, and records appear at
wildly different rates. On `info`, read once per connection, a byte is free. On
`can_record` at 4000 frames per second it is 4 kB/s — on the one stream §4.1
identifies as able to saturate a link, and the identical arithmetic that kept
BRS and ESI out of the specification in the first place.

There is no consistent answer that is also cheap. Extending everything
contradicts §4.1; extending nothing is uniform and costs nothing; extending only
the cheap records is defensible but buys a narrower kind of optionality than it
first appears, for reasons that follow.

### 5.3 Three mechanisms already exist

The argument for trailers assumes there is otherwise no way to add anything.
That is not the case:

- `gps_fix` — the record most likely to want new fields, and the one where
  RTK detail and further accuracy metrics would land — already has a trailer.
- Reserved bits and bytes absorb flags and small values: 24 spare capability
  bits, 20 spare GPS validity bits, two spare bytes in each batch header.
- **Control opcodes are not fixed-size records.** A minor version may add as
  many as it likes with any payload it likes. Anything a client can *ask* for
  is already extensible without limit.

That last one does more work than it looks. Multi-bus CAN, a sensor mounting
rotation, further device capacities — each is something a client requests
rather than something the device pushes, and each can arrive as a new opcode.
What none of the three covers is a new field *pushed alongside streaming data*
on a record other than `gps_fix`, and it is genuinely hard to name one that
matters and is not per-frame.

### 5.4 What this costs

Stated plainly, because §5.1 makes it irreversible:

- A magnetometer triple, a remote frame's requested length, and CAN FD's BRS
  and ESI are VTP/2 changes. All three are per-item, so no affordable trailer
  scheme would have rescued them anyway.
- A pushed batch-level field beyond the two reserved bytes in each header is a
  VTP/2 change. This is the real exposure, and it is a bet that two bytes and a
  control channel are enough.
- A VTP/2 means a new service UUID, so a device speaking it is invisible to
  VTP/1 clients that do not also scan for it (SPEC.md §11.1). That is the mechanism
  working as designed, but it is not free.

The alternative was four new extension mechanisms to specify, test and
implement twice over, against a need nobody has yet articulated, in a
repository whose own list of costs already names "more surface to get wrong".

## 6. What VTP/1 costs

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
