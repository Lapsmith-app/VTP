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
support, and the control channel is tagged request/response with typed failures
(`table_full`, `rate_exceeded`). Reprogramming everything on every connect —
the only safe strategy above — is made cheap instead of merely necessary: the
table clears on disconnect, and re-installing the same subscription updates in
place, so the client always knows the table because it installed it, this
connection.

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
device enforces, per identifier, expressed in milliseconds. Strictly better
than hardware filter slots. VTP/1 keeps it as the `periodic` subscription mode
rather than replacing it — and pre-1.0 drafts that generalised it with
on-change and every-Nth modes walked that back (§8.4): the decade of evidence
is for per-id intervals, and each extra mode bought real firmware complexity
for a need nobody had demonstrated.

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

There is one genuine sensing gap here: negotiated link-layer payload, PHY and
connection interval are not exposed to applications on at least one major
mobile platform, so a client cannot tell a well-configured device from one
quietly costing three times the airtime for the same data. Pre-1.0 drafts
closed it with a `GET_LINK_PARAMS` opcode reporting the device's own view of
the link, and the opcode was removed before anything shipped (§8.7): it was
bench diagnostics dressed as a core feature — a record, a validity scheme and
a set of consistency rules, in service of a question that a sniffer on a bench
answers better and that no lap ever depends on. SPEC §12.1 now says plainly
that §2.1–2.3 are verified on a bench; opcode `0x31` stays unassigned so a
later minor can revive the report if hardware experience shows the runtime
check earning its keep.

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

## 6. Why subscriptions are addressed by mask

A reasonable objection: masks belong in hardware. A CAN controller has a
handful of acceptance filters and never enough of them, so `(id, mask)` is how
you make four filter slots cover the traffic you want. A VTP/1 device is not
in that position — it is unpacking every frame the controller gives it and
repacking it into a notification, and at that point matching an exact
identifier is a dictionary lookup. So what is the mask for?

Three things, none of which is hardware filtering.

**Slots are the scarce resource, and the protocol says so.**
`can_subscription_slots` (SPEC.md §4) is a declared capacity a client must plan
against, and a mask lets one slot stand for a family of identifiers. Buses
allocate identifiers in blocks — a diagnostic range, an ECU whose low bits are
a source address, an extended-id block from one supplier — and a client wanting
such a block spends one slot on it instead of sixteen or two hundred and
fifty-six. That is the difference between a table a modest device can hold and
one it cannot.

**A mask of zero is the whole bus, which nothing else can express.** Per-id
subscription requires knowing every identifier in advance, which is exactly
what a user reverse-engineering an unfamiliar car does not. One catch-all entry
at a `periodic` rate is a survey of everything the bus carries, and the
specificity ordering of SPEC.md §9.2 makes it compose: the catch-all governs
whatever the specific entries do not, so a client can log the whole bus slowly
and take four identifiers at full rate at the same time, with no overlap and no
duplicate frames. The precedence rule exists for this case, and this case is
why masks are worth their cost.

**A device that does have filters can push the subscription down into them.**
The mask arrives in the shape the controller wants. Going the other way — a
device deriving hardware filters from a list of exact identifiers — means
reconstructing masks the client already knew.

The cost is real and is paid per frame. Matching stops being a lookup and
becomes a scan of the table with a precedence rule to break ties
(SPEC.md §9.2), on the one stream that can run at four thousand frames a
second. It also reshapes naming: an identifier stops being a unique name for
an entry once masks exist, which is why a subscription's identity is its
`(id, mask)` pair (SPEC.md §9.1) — pre-1.0 drafts solved the same problem
with device-assigned handles, and the pair turned out to already be the name,
so the handles went (§8.7).

A device unwilling to pay any of that pays none of it. `masked_subscriptions`
is `capabilities` bit 6, so a client knows before it composes a plan rather
than by being refused, and a device that offers plain `CAN_SUBSCRIBE` and
matches exact identifiers is fully conforming. The generality is available to
the devices that want it and free to the ones that do not, which is the
property that made it worth having in major version 1 rather than deferring.

## 7. What VTP/1 costs

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
- **More surface to get wrong.** Seven characteristics, three batch formats and an
  extension mechanism is more specification than a single 20-byte struct. The
  conformance corpus exists because that is a real cost and it needs paying down
  with tests rather than with prose.
- **Zero deployed devices.** Which is the largest cost by a wide margin, and the
  one nothing in this document addresses.

---

## 8. Notes to the specification, section by section

SPEC.md states the rules and stops; this section carries the reasoning each
rule used to carry inline. It exists because the specification's own claim to
be "deliberately terse" had stopped being true — at one point it ran to more
than two thousand lines, most of them explanation, and the barrier to
implementing a GPS beacon was reading all of it. The rules did not change when
the explanations moved here; where a rule *did* change in the same revision,
the note says so.

### 8.1 Transport (SPEC §2)

**Why the link-layer payload matters as much as the MTU (§2.1).** A large ATT
MTU is not by itself a throughput figure. Sent over the 27-byte default
link-layer payload, a 247-byte notification is fragmented across ten or more
packets, each carrying its own header, inter-frame spacing and
acknowledgement — roughly three times the radio airtime for the same bytes,
taken from every other peripheral sharing the central's radio. §4.1 above has
the airtime arithmetic.

**Why flush timing follows the device's own clock (§2.3).** The connection
interval is granted by the central, not chosen by the device, and a central
serving several peripherals commonly grants 30 ms or more whatever was
requested. A device that times batch flushes from the interval it *asked for*
misbehaves precisely on the crowded radios where behaving matters.

### 8.2 Discovery and Info (SPEC §3, §4)

**Why the Device Information Service is a SHOULD (SPEC §3.4).** Nothing in VTP/1
reads it, which is the point: it is where every generic Bluetooth tool already
looks, so it is what answers "which firmware is on the logger that is
misbehaving" without the asker needing to know anything about this protocol.
It carries no protocol meaning, so requiring it would add a conformance
surface no client behaviour depends on.

**Why the attribute table is fixed (§4.1).** Central stacks cache the
attribute table across connections, and several cache it across reboots of the
phone. A device whose table changes between connections — because a capability
was switched off in firmware, or a build shipped without a role — hands the
client a stale handle. The client then reads or writes the wrong attribute
rather than discovering a missing one, which is precisely the
plausible-wrong-value failure SPEC §1.1 exists to prevent. An inert characteristic
costs a handful of lines; a mutable table costs a caching bug on somebody
else's phone.

**Why an Info that breaks the capability matrix decodes (§4.1).** Earlier
drafts had the receiver reject it, exactly as it rejects a wrong length. But
the record is well-formed — every field is where it says it is — and the
violation is a *device* defect, almost certainly a firmware bug rather than
wire corruption (the BLE link layer already checksums every packet). Rejecting
turned "your firmware set one bit wrong" into "the app says no device found",
with no diagnostic path for exactly the person able to fix it. The client now
decodes, refuses to use the contradicted role, and surfaces the contradiction;
the reference encoder still refuses to produce one, so a conforming device
cannot ship it.

**Why `max_notify_bytes` was removed.** Info once published the largest
notification the device would send. Its first definition — the negotiated ATT
payload — was unimplementable: a client reads Info before a peripheral learns
the negotiated maximum. Its second — a fixed device ceiling — was
implementable and useless: a notification can never exceed the negotiated ATT
payload, the client's own stack knows that number, and a receive buffer sized
to it is always sufficient. A field that restates a bound the reader already
has is a field two implementations can disagree about for no gain, which is
the same argument that removed `can_max_payload`.

### 8.3 GPS (SPEC §5)

**Why the solution-epoch flag exists (SPEC §5.6).** A GNSS receiver computes a
solution for a specific instant and delivers it over a serial link tens to
hundreds of milliseconds later, varying with the receiver, its output rate and
how busy the link is. A device that stamps delivery therefore reports a
position that was true at one time with a timestamp naming another, and every
GPS sample runs late against CAN and IMU by that latency — removing exactly
the cross-channel alignment the shared clock exists to provide, while every
number stays plausible. Whether the receiver exposes the epoch — a timing
message, a PPS edge — is a property of the hardware, so the requirement cannot
be unconditional; the flag is the honest answer to "when was this true?", and
its absence is an admission rather than a guess.

**Why out-of-range values decode (§5).** Earlier drafts had the receiver
reject a latitude of 91°, on the argument that every other field in the record
came from the same bytes. But the link layer already rules out wire
corruption, so an out-of-range coordinate is a firmware bug — and rejecting
the fix hides the evidence from the person best placed to notice, while a
strict client meeting a slightly-buggy device shows a blank screen with no
path forward. The bounds still bind the device absolutely, the reference
encoder still refuses to emit a violation, and clamping is still forbidden —
91° is not a place a clamp could move closer to, and clamping to 90° puts the
vehicle at the pole and lets the client draw it there. The same reasoning
moved the RTK-flag consistency rules (§5.3) from receiver-reject to
device-must-not-emit: a receiver MUST NOT resolve both-RTK-bits as "fixed
wins", because that upgrades a device's accuracy claim on the strength of a
bug, but it decodes the fix and says what it saw.

### 8.4 CAN (SPEC §6)

**Why timestamps are end-of-frame (SPEC §6.7).** A device cannot generally know a
frame's time on air without knowing the bit timing and the number of stuffed
bits, so a start-of-frame timestamp would be back-computed rather than
measured, and this specification prefers a measurement it can defend to an
estimate it cannot. The cost: a long frame is stamped later than a short one
relative to the moment it began — at 500 kbit/s a 64-byte FD frame is roughly
a millisecond of that.

**Why there are two subscription modes.** Pre-1.0 drafts had four:
`every_frame`, `periodic`, `on_change` and `every_nth`. The last two were
removed, and the argument is worth keeping because each looked free on paper.
`every_nth` decimates by frame count, so its output rate scales with bus load
— the wrong property for budgeting a radio — and `periodic` answers the same
need with a rate the client actually chose; it also produced a genuine
specification bug, because `every_nth` with N of 1 selects exactly what
`every_frame` selects and the two were treated differently by the (since
removed) rate-admission rule. `on_change` was the single largest RAM demand in
the CAN role: SPEC §6.8 requires mode state per (subscription, identifier)
pair, and `on_change` alone needs a copy of the last forwarded payload — 8 to 64 bytes —
per identifier, on a mask that may cover the whole bus. Both modes are
revivable in a later minor from the reserved enum values, against demonstrated
need rather than symmetry.

**Why the schedule state may be bounded (SPEC §6.8).** A mask of zero matches
every identifier on the bus, and a device cannot know at install time how many
that is — so `periodic` state per (subscription, identifier) pair is an
unbounded demand on a bounded MCU. Earlier drafts left exhaustion unspecified, which is the worst
available answer. Shedding is the mechanism the device already has: frames it
can no longer schedule are counted in `dropped` with the shedding flag set,
observable and degrading rather than silently wrong. Substituting unscheduled
forwarding is forbidden because a client that asked for one frame in ten and
silently gets all of them cannot tell a configuration bug from a flood.

**Why the bound has an eviction rule (SPEC §6.8).** Permitting a bound and
saying nothing about what to sacrifice at it leaves a device with two costs and
no way to choose between them, and the first implementation found the bad half
on a vehicle: a broad slow subscription filled the pool, an exact subscription
installed afterwards could allocate nothing, and it was shed permanently —
never receiving even the first frame SPEC §6.8 promises it. The entries
blocking it belonged to a subscription that could not use them, because
SPEC §9.2 had displaced it on exactly those identifiers.

The two costs are not symmetric, which is what makes the rule statable. Evict a
displaced entry and its owner over-delivers *once*, and only if governance
returns to it, which requires the client to remove the subscription that took
it. Refuse to evict and a subscription the client installed is silent for as
long as the bound holds, which is indefinitely — and silent in the way
SPEC §6.8 exists to prevent, since a quiet bus and a starved subscription look identical
from the client. So displaced state goes first, and the specification says the
early frame that may cost is conformant rather than leaving an implementer to
discover the choice was theirs. Only when every entry is a governing one does
the device shed, and then no rule can help it: every candidate is in use.

### 8.5 IMU (SPEC §7)

**Why `t_base` is acquisition time, with no flag and no exception.** Samples
are commonly drained from a sensor FIFO in bursts, so the read happens well
after the earliest sample was taken — at 833 Hz a sixteen-deep FIFO is nearly
twenty milliseconds. A device stamping the drain reports the batch late by the
depth of its own buffer, and the error changes with occupancy, so a client
cannot even calibrate it away. Unlike a GNSS solution epoch (§8.3 above) the
device sets the sampling schedule itself: sample 0's time is the drain time
less the samples behind it, and a device that cannot work that out is not
measuring what it claims to measure.

**Why a batch ends at a discontinuity.** Even spacing is what lets a client
derive per-sample times arithmetically; a gap inside a batch makes every
derived timestamp after it wrong by the size of the gap, silently and
increasingly. Splitting costs one extra notification at the moment the device
is already in trouble, which is the cheapest possible price for not shipping a
timeline that is quietly wrong from that point on.

**Why `period` may be approximate, and by how much.** Real sensor output data
rates are not integer hertz, so `period` is the true interval rounded to the
nearest microsecond — up to 0.5 µs per sample of error, accumulating only
within a batch because each notification re-anchors at a measured `t_base`.
At 833 Hz over nineteen samples the worst case is about 9 µs, below the 10 µs
resolution of a CAN timestamp, so it can never be the limiting term in
cross-channel alignment.

### 8.6 Clock, sequence and loss (SPEC §8)

**Why the seq rule is stated as a property of the notification (§8.2).** The
counter phrasing — "restarts at 0" — can be read as the counter being zeroed
and the first notification then taking the *next* value, which puts 1 on the
wire. A device did exactly that, and its own conformance check was written to
match, so the test agreed with the bug it existed to catch. "The first
notification carries 0" admits one reading.

**Why `dropped` is best-effort and `seq` exact (§8.3).** Attributing every
lost item to exactly one notification means owning the counter transactionally
across encoding, transmit-queue refusal and supersession. The question the
field answers — "is my link bad, or is the device overrun?" — survives a count
landing one notification late, so the exactness is spent on `seq` instead,
where it is cheap: `seq` counts notifications actually sent, committed when
the transport accepts one.

### 8.7 Control (SPEC §9)

**Why one outstanding request (SPEC §9).** An earlier draft let a client pipeline
and required a device to accept at least four outstanding requests. That
bought one thing — installing a subscription table without a round trip per
connection interval — and cost every implementer a queue, a depth, an ordering
guarantee and a refusal to hold them together. Nothing on the control plane is
latency-critical: subscriptions are installed once at connect, rates change
when a user changes them, and `TIME_SYNC` measures the round trip it is
already waiting for.

**Why subscriptions have no handles.** Pre-1.0 drafts had installs return a
device-assigned handle, and `CAN_UNSUBSCRIBE` took one. But install-in-place
already made `(id, mask)` a unique name — the same pair updates the same entry
— so the handle was a second name for a thing that had one, and it dragged
allocation rules, a reuse prohibition, an `unknown_handle` status and
client-side bookkeeping behind it. Removal now names the pair directly; the
overlap tie-break (SPEC §9.2) uses installation order, which the client knows
because it installed the table, this connection.

**Why there is no `CAN_LIST`.** The specification *mandates* that
subscriptions clear on disconnect and that a client reprograms on every
connection — so a conforming client always already knows the table: it
installed it, this connection, with install-in-place semantics. A read-back
opcode could only ever reveal device bugs, which makes it bench tooling, and
it was the single largest piece of the control plane: a paged response, two
record types, and rules for `total`, `index` and overshoot. The harness
verifies table behaviour behaviourally — install, send, observe, remove — and
opcode `0x05` stays unassigned for a later minor if implementation experience
argues the debug window back in. `GET_LINK_PARAMS` went the same way for the
same reason (§4.4 above).

**Why a subscription is never refused on rate grounds (SPEC §9.3).** An earlier
draft asked a device to predict the load a subscription would add and refuse
beyond a budget. The prediction cannot be made: not for `every_frame`, which
depends on what the bus will carry, and not for a mask, which keeps its
schedule per matching identifier and so produces one rate per identifier
rather than the one its `arg` names. Shedding is the honest mechanism and the
device has it already — observable, degrading rather than failing, and needing
no forecast. `rate_exceeded` remains for the two rate setters, where the
ceiling is a fact the device knows.

**Why TIME_SYNC carries two timestamps (SPEC §9.5).** One timestamp cannot bound
its own error: a client that knows only when it asked, when it heard back, and
one device reading cannot separate the outbound delay from the inbound one, so
its estimate is uncertain by the whole round trip — tens of milliseconds on a
30 ms connection interval, in an exchange whose purpose is aligning a
microsecond clock. Reporting the device's own processing time lets the client
subtract it; what remains unmeasurable is the asymmetry between the two
queuing delays, which is why `delay` is a floor and not a total. This is NTP's
exchange, for NTP's reason. The request carries no parameters because an
earlier draft had it carry the host's UTC milliseconds, which the equations
could not use and the device could only discard.

**Why owing ends at the send, not the confirmation (SPEC §9).** SPEC §9 used
three words for one idea — a device "owes" a response, a tag is reusable once
its response has been "sent", a request is refused when one is already
"outstanding" — and for a single response in flight they agree. They come
apart the moment a device holds two, which SPEC §9 creates itself: the `busy`
refusal is a response, so a device answering one request and refusing another
is holding both. The first implementer outside this repository found it by
review, on a stack whose queued-request pump runs before the completed
request's callback, and asked which word governed.

The answer is the send, and the reason is that the client's boundary is the
arrival. SPEC §9 tells a client to write again as soon as the response reaches
it, and ATT permits that write before the client's confirmation has gone out —
request/response and indication/confirmation are independent flows on the
bearer. Send, arrival and confirmation are three points in that order, so a
device whose obligation ran to the confirmation would refuse, with `busy`, a
client that had waited exactly as long as SPEC §9 told it to; the retry would
meet the same window, and whether it ever cleared would depend on how the
client's stack ordered its own confirmation against its own writes. **A
device's completion point has to fall no later than the client's.** The send
does. The confirmation does not.

This was got wrong first. The draft that answered the report chose the
confirmation, on the argument that the delivery slot is physically singular —
one outstanding indication per bearer — so a response awaiting confirmation
still occupies the only means the device has of answering anything else. That
premise is true and the conclusion does not follow from it. One outstanding
indication is a reason to **hold** a response, not a reason to **refuse** a
request: a response composed while an earlier one is unconfirmed simply waits
its turn, and waiting is bounded and safe where refusing is neither. The
supporting argument — that the confirmation is the one moment both ends
observe — was true and irrelevant, because only the device ever evaluates this
boundary. The client evaluates the arrival.

So SPEC §9's tag-reuse sentence was right before the report and is right now.
What the report changed is everything around it: the word is stated once, the
same way, in all three places, and the two things the old text left unsaid are
said.

The report was right about the structure even though it picked the later
boundary. One flag does not do, whichever boundary it is cleared at: the
refusal is a response too, so a device that has sent the answer to one request
and not yet sent the refusal to the next still owes something, and a flag
cleared on that first send reads as free. A count is the fix, and the report's
device had one. What was conservative was only where it stopped counting.

The first is that a `busy` refusal is a response and is owed like every other,
so it too waits for the slot. The second is what a device has to hold. It is
one response beyond the indication in flight, and that is not a spare: it is
where a **conforming** client's next request lands, arriving after the
previous response reached it but before the confirmation did. A device with
nowhere to put that response would have to refuse a client doing everything
right — the same defect as the confirmation reading, reached from the other
side. Past that a device has no room and discards, which is not a queue depth
to negotiate: it is the point where a client writing faster than a bounded
device can answer would otherwise size the device's memory.

Two held is therefore not the four-deep queue this section opens by rejecting.
That draft cost a depth to agree on, an ordering guarantee and a refusal to
hold them together, all so a client could pipeline by design. Nothing here is
by design: the order is the order they were composed in, the second slot
exists for a client that is obeying the rule, and no client benefits from
asking for more.

**Why deliverability is decided before dispatch (SPEC §9.4).** Applying first and
answering second is the natural order to write the code in, and it strands the
client: a device that applies a request whose response is then lost leaves the
client to retry, and for any non-idempotent request the retry applies it
twice. The failure was observed in practice before it was specified.

### 8.8 Security (SPEC §10)

**Why the obligation is one-sided.** Requiring encryption costs the device
author real work — bond storage, a bond table that fills, and a mismatch after
reflashing that presents as a broken device — and that cost lands hardest on
exactly the small implementations this protocol needs. Supporting encryption
costs a client almost nothing: every major central stack turns `Insufficient
Encryption` into a pairing attempt on its own. Putting the requirement on the
side that can bear it leaves each device free to choose its posture without
fragmenting what clients can talk to.

**Why enforcement is the GATT permission, not an application check (SPEC §10.1).**
The two are not interchangeable: a characteristic carrying the permission is
enforced by the ATT layer, so an unencrypted write never reaches application
code and there is nothing there to generate a `needs_encryption` reply from.
An earlier draft required both, which cannot be implemented; the status stays
allocated because a status, once allocated, is never reused (SPEC §11.4).
## 9. Why the supply reading is an opcode, and why it is two fields

Bluetooth has had a Battery Service since 2011. `0x180F` is one characteristic
carrying one byte, every generic tool reads it without being told how, and
SPEC §3.4 already takes exactly that argument for the Device Information
Service: a thing every tool looks at is worth exposing precisely because nothing
in this protocol has to know about it. The first draft of what became SPEC §9.7
rejected it on the grounds that a percentage is a lie on
hand-built hardware and that the record should carry millivolts instead. That
was wrong twice over, and the correction is worth recording because the mistake
is a recurring one.

It was wrong about the client. Volts are not actionable without the cell
chemistry, the cell count and the load, so a client handed 7.42 V either renders
a number nobody reads or converts it to a percentage itself — which is the same
guess the device was being spared, made one layer further from the hardware that
could inform it. And it was wrong about the device: a builder who has only a
divider will map it to a percentage anyway, and doing that on the board is where
the conversion belongs.

What survives the correction is smaller and is not about accuracy at all. It is
that a logger wired to the car's ignition feed has **no charge to report**, and
in a percentage-only encoding it must answer 100 forever. That is a reserved
value meaning "not applicable" in the one field a client draws as a gauge — the
same shape as latitude `0x7FFFFFFF` decoding to 214.7°, which SPEC §5.4 is about, and
the same shape as the "absence is a magic value" row in README.md's comparison
table. One byte of `source` closes it, and a client that does not care reads
`percent` and ignores the rest.

So the record is a source and a percentage, each behind a validity bit, and
`0x180F` is a thing a device MAY also expose rather than the thing this
specification points at: it can say neither "external power" nor "unknown", and
a device with either state to report has to invent a number in the only field it
has.

**It is polled, not pushed.** A supply reading changes over minutes. A
notification stream for it would cost a characteristic, a CCCD, a place in the
fixed attribute table and a conformance role, for a value no client watches
continuously — and SPEC §11.3 names new opcodes as the extension point for
exactly this shape of thing, something a client asks for rather than something
the device sends. A client that wants a low-battery warning polls every
half-minute and pays one round trip for it.

**It has a capability bit rather than a `bad_params`.** Bit 8 costs a client one
test against a word it already read, and it means an app can decide whether to
draw a battery indicator at all before asking a question the device might refuse.
The alternative — ask everyone, treat `unsupported_opcode` as "no battery" — is
the same information one round trip later and reads identically to a device that
is broken.

The costs are real. Bit 8 is the first capability past the eight the
advertisement carries (SPEC §3.3), so this is the first role a scanner cannot
see before connecting; the record has one reserved byte and six reserved
validity bits and nothing else, so anything richer — a voltage after all, a
current draw, a time-to-empty estimate — is a later opcode rather than a later
field; and there is deliberately no way to ask "tell me when it gets low",
because that is a threshold, a hysteresis and a subscription for a question
`GET_POWER` answers in one round trip.

---

## 10. Why aiding is a transfer, and why it is not on Control

A GNSS receiver with no current orbit data reads it from the satellites, at
50 bits per second, and takes tens of seconds to finish. Under a grandstand or
between two transporters it may never finish. The phone in the driver's pocket
can fetch the same data over the network in a moment, so the only question was
how it reaches the device.

### 10.1 Control could carry it, and should not

SPEC §11.3 names new control opcodes as this protocol's general-purpose extension
point, and by that reading aiding is three opcodes and nothing else. The
arithmetic is what rules it out. A predicted-orbit product runs to tens of
kilobytes; SPEC §9 allows a client one outstanding request; so a 40 kB transfer is
about 164 write-and-wait round trips, several seconds of wall clock, and a
control plane that can answer nothing else while it runs — no `TIME_SYNC`, no
subscription change, no rate change.

Written without a response on its own characteristic, the same transfer is a
few hundred milliseconds and blocks nothing.

That is a new attribute in a table SPEC §4.1 calls fixed, which would be a VTP/2
change after 1.0 and is an ordinary one before it. It is being made now
because it cannot be made later.

### 10.2 Dropping the per-write response costs nothing

A Write Command has no reply, so the obvious objection is that the device
cannot say "that chunk was malformed". It never needed to. A chunk is
legitimate only after `GNSS_AID_BEGIN` answered `ok`, which fixed the shape of
the whole transfer, so the device can tell a chunk it cannot place from one it
can without being asked — and every outcome a client acts on arrives at
`GNSS_AID_COMMIT`, on the plane that already has tags, typed failures and a
lifecycle.

The division is the one SPEC §14.5 states: the fast path carries bytes, and
the control plane carries meaning.

### 10.3 Loss becomes a number, as it does everywhere else

Without acknowledgement per chunk, a lost write is invisible until the whole
transfer is wrong — the failure mode SPEC §8.3 exists to prevent on the outbound
streams. So commit reports the lowest index the device did not receive, the
client resends from there, and the exchange terminates because that index
strictly advances.

It only works because `chunk_bytes` is fixed for the transfer. With
variable-length chunks a device cannot place chunk 7 without having received 0
through 6, and there is no gap to resend — only a transfer to start again.

### 10.4 The bytes are opaque, and that is a real cost

SPEC §1.1 says no receiver may produce a plausible wrong value, and this
specification has otherwise refused every opportunity to carry something it
does not understand: equations rather than DBC, enumerated channels rather than
an expression language, no vendor namespace anywhere. An aiding payload is a
vendor blob, and pretending otherwise would be worse than admitting it.

Three things bound the damage. The bytes travel client to device, so they enter
no recording. A wrong payload costs time to first fix, not a corrupted lap. And
the one thing a client could get wrong without noticing — which format the
receiver speaks — is the one thing the protocol does carry, as an enumeration
the device declares rather than a string the client guesses.

What is not bounded by any of that is a device treating aiding as measurement,
which is why SPEC §14.6 forbids it in those words. Aiding is a plausible position
arriving from something that is not a sensor; a fix built from one would be
wrong, plausible, and indistinguishable from a real one.

### 10.5 Nothing is reserved for corrections

Differential corrections were the obvious neighbour: same direction, same
opacity, same reason the phone has the data and the logger does not. They are
out of scope, and no slot is held for them.

Holding one would have bought nothing. SPEC §11.4 lets a minor version add an
`aid_format` member whenever it likes, so there is no collision to get ahead of
and no compatibility cliff — an allocated value for a format nobody implements
is a line in a table that means nothing, and a device could declare it and be
conforming while doing anything at all. The lifecycle is the actual work:
corrections are continuous for a session rather than one transfer at connect,
which needs an inbound rate ceiling and rules about airtime against CAN. That
work is what a future minor version would do, and reserving a number now does
none of it.

### 10.6 What earlier drafts carried, and this one does not

Aiding went through the same review as the rest of the protocol
(CHANGELOG.md), and three pieces of its first draft did not survive it. They
are recorded here because each is the kind of thing a reviewer will propose
adding back — and so is the one thing the review tried to remove and could
not, because that failed attempt is the best illustration of where the
review's own method breaks.

**The transfer token stayed, and a draft of the review removed it.** Every
chunk echoes a byte from `GNSS_AID_BEGIN`, so that a chunk still in flight
for a discarded transfer cannot land in the one that superseded it. The
draft argued no such chunk can exist: ATT runs a client's writes down one
ordered L2CAP bearer, so everything written before the new BEGIN arrives
before it — the token distinguished transfers the transport already keeps
apart, the subscription-handle mistake (§8.7) wearing a new name. That
argument is true of a bearer and false of a client. EATT (Bluetooth 5.2)
lets a client open several ATT bearers to the same server, each its own
L2CAP channel with its own MTU, ordered only within itself — so a chunk
queued on one bearer genuinely can arrive after a superseding BEGIN sent on
another, and without the token it lands at whatever offset its index names.
The CRC would catch the corruption, but a valid transfer would fail, and
"redundant with transport ordering" was simply not true of the transport as
specified. One byte per chunk is what the guarantee costs; SPEC §14.3 now
also says which bearer's MTU governs `chunk_bytes`, which the one-bearer
draft never had to ask.

**An abort opcode.** `GNSS_AID_ABORT` discarded an open transfer. So does
opening the next transfer, and so does disconnecting — and a client abandoning
a transfer is always about to do one or the other, because an open transfer it
does not intend to finish costs it nothing to leave. An opcode whose whole
effect the next BEGIN already has is a second name for the same act, and every
name is conformance surface: with it gone, so are its parameter checks, its
session validation, and the checks that tested them. `0x14` stays unassigned.

**A `persists` flag.** `gnss_aid_caps` said whether `held_until` survives a
power cycle, so a client could predict what the device would hold on the
*next* connection. But a client MUST read `GNSS_AID_INFO` before every
transfer, and that read answers for the connection it happens on — the only
one in which the answer is actionable. A prediction about the next boot is a
value nothing can act on, carried so it could go stale.

**A chunk count at commit.** The commit carried `chunks` so a disagreement
about the transfer's shape would be caught rather than acted on. The device
computes the same number from its own `GNSS_AID_BEGIN`, so the parameter could
only ever agree or be refused — and the CRC already backstops every
disagreement that matters, including the ones the count cannot see. Carrying
it also required a paragraph about commits whose count contradicts the
transfer, a `bad_params` rule for it, and a proof that the exchange still
terminates. Four bytes of parameter bought a page of failure modes.

What survived is the part that does the work: one transfer open at a time,
named by a token, a fixed `chunk_bytes` so index-to-offset is arithmetic,
`first_missing` so loss is a number, and a CRC-32 stated exactly. The
transfer protocol is the minimum that makes write-without-response
recoverable on the transport Bluetooth actually provides, and nothing else.


## 11. Why OBD polling is a role, and why the safety bit is the point

VTP/1 grew up as a listening protocol. Every role before SPEC §15 shares one
property so completely that nothing ever needed to state it: the device
observes. GPS listens to satellites, CAN listens to the bus, the IMU listens
to the device itself. The CAN role's opcode set — subscribe, unsubscribe,
reset — configures a receiver, and nothing in the protocol could cause the
device to put a frame on the vehicle's bus. Devices of this class are sold on
exactly that property: listen-only controller mode, TXD physically lifted, a
dongle that cannot disturb a moving car however wrong its firmware is.

OBD-II breaks the property, necessarily. J1979 is request/response — nothing
appears on `0x7E8` until somebody puts `02 01 0C` on `0x7DF` — so a device
that can read the legally mandated PIDs is a device that transmits. The
question was never whether polling is useful (it is, §11.1); it was whether a
listening protocol should carry a transmitting role at all, and the answer
turned on an argument about honesty rather than about features.

### 11.1 What it buys: a universal floor

Raw CAN sniffing yields usable channels only on a car whose OBD port is not
gatewayed **and** whose broadcast frames somebody has reverse-engineered.
Both conditions fail often: gateways that isolate the diagnostic port from
the body buses have shipped in volume since the late 2010s, and the
reverse-engineering exists for popular track cars and almost nothing else. A
pure sniffer on an unknown car reports nothing, and a product built on one
either finds a car it knows or fails.

The J1979 Mode 01 PIDs are the opposite case. They are legally mandated on
essentially every petrol car since 2001 and diesel since 2004, they need no
per-car knowledge, and — the part that earns the role its place in this
protocol — the standard itself carries capability negotiation: PIDs `0x00`,
`0x20` and `0x40` are bitmasks of what each ECU actually implements. One
probe at connect tells a client exactly which channels this specific car
offers before anything is polled. Declare, verify, use: the same shape as
every other role here, running on a negotiation mechanism a 25-year-old
standard already provides. Without the role, a VTP device has no floor;
with it, engine speed, coolant temperature, throttle and a dozen others
work on nearly every car made this century.

### 11.2 The bit is the load-bearing part

Suppose the role had been added the obvious way instead: no capability bit,
a vendor opcode or an out-of-band convention, responses simply appearing on
`0x7E8` for clients that know to subscribe. Everything would work — and the
protocol would have silently lost the ability to express whether a given
device transmits. "VTP devices do not transmit" would have quietly become
false as a category statement, with nothing at any layer able to say which
devices it is false of. A user plugging a dongle into their own car could
not find out. A review of a device's safety claims could not cite anything.

That is the failure SPEC §1.1 exists to prevent, at the scale of a device rather
than a field. A protocol whose devices may transmit without declaring it
gives every client a plausible wrong value for the one question — "does
this thing talk to my car?" — whose wrong answer is a trust failure rather
than a display bug. So bit 10 is not a feature flag that happens to gate two
opcodes; it is the declaration that keeps the category statement meaningful.
After SPEC §15, "this device does not transmit" is still expressible, still
checkable, and now per-device: bit clear means the old property holds, bit
set means it does not and says so.

Three consequences follow, and each is in the specification because the
declaration would otherwise be weaker than it looks:

- **The bit describes the connection, not the model** (SPEC §15). Info is
  re-read every connection precisely because a DIY device is reflashed by
  its owner (§8.2); a device with a physical listen-only switch clears the
  bit while the switch is set, so the declaration tracks the hardware state
  it claims to describe.
- **The flag makes it observable** (SPEC §15.6). A capability bit is a
  statement of what a device may do; `can_flags` bit 1 states what it is
  doing, on every batch, to anyone reading the stream — including a client
  that never sent `OBD_POLL_SET` and a tool inspecting a log after the
  fact. The stop rules of SPEC §15.7 are auditable because the flag's
  falling edge is on the wire.
- **What may be transmitted is enumerable** (SPEC §15.1). A declaration
  that a device transmits is only as strong as the bound on what. SPEC §15.1 is
  a complete enumeration — single-frame Mode 01 requests, one PID each,
  spaced, never retried, no flow control — so the worst case on the bus is
  one short frame per `obd_min_interval_ms`, computable from Info before
  anything is sent. The alternative, an opcode that transmits a
  client-supplied frame, would have made bit 10 mean "this device transmits
  whatever an app tells it to", which bounds nothing and declares nothing.

### 11.3 The device transacts; the client computes

The poll loop lives on the device and the arithmetic lives in the client,
and both placements were forced rather than chosen.

The loop cannot live in the client. SPEC §9 allows one outstanding control
request, so client-driven polling would be a write-and-indication round
trip per sample — 30 to 60 ms on realistic connection intervals — occupying
the control plane completely at any useful rate: no `TIME_SYNC`, no rate
change, no subscription change while logging. That is the same arithmetic
that kept aiding transfers off Control (§10.1). It would also make sample
spacing a function of radio conditions, on a protocol whose central design
investment is one shared device clock (§2.5): device-side, the interval is
the device's own microsecond clock and every response carries a true
bus-arrival time.

The decode cannot live on the device. A PID formula table is large, grows
with every SAE revision, and is exactly the kind of thing that is trivial
to update in an app and painful to update in fielded firmware — the client
this role was designed against already carries an 1,100-line PID engine.
Mode 01 responses echo their service and PID, so frames are
self-describing and the client needs no per-request state; the device's
contribution is the part only it can do — request framing, spacing, and
honest timestamps. A device that shipped scaled engineering values instead
would duplicate the formula table on the end that is hardest to fix, and
disagreements between the two copies would be plausible wrong values by
construction.

Delivering responses as ordinary `can_record`s follows from the same
split, and it kept the wire format untouched: no new record type, no new
characteristic, no new stream — and a VTP/1.0 client that has never heard
of bit 10 is unaffected, because nothing below is reachable without an
`OBD_POLL_SET` it cannot send.

How the responses reach the client was the last question settled, and it
was settled by reversing a draft. The draft required an explicit
subscription: poll responses would arrive only through the table, like
every other frame, on the argument that one delivery path is cleaner than
two. What that design actually permitted was a device **transmitting
requests on a moving car and discarding the answers as unsubscribed** —
every cost of the role and none of its benefit, reachable as the default
consequence of forgetting one call. A protocol whose worst state is its
most likely mistake fails this repository's own standard, and the
double-instruction bought nothing the safety story needed: the
safety-relevant act is transmitting, and `OBD_POLL_SET` is already its
explicit consent. Requiring a second instruction before the device may
*hand over* answers it already extracted was ceremony wearing safety's
clothes.

SPEC §15.5's rule is the repair, shaped so the draft's one real virtue —
a stream fully determined by declared state, with SPEC §9.2 total over
it —
survives: while the poll set is non-empty, frames on the probe's reported
response identifiers that match **no installed subscription** are forwarded
`every_frame`. The table still governs everything it matches, so SPEC §9.1
and SPEC §9.2 are untouched and a client that wants tighter control installs
ordinary `periodic` subscription, which wins; the fallback exists only
underneath, is not table state, and dies with the poll set. The cost is
recorded where it is paid (SPEC §15.5): the device cannot tell its own
answers from another tester's on the same identifiers, so a polling client
is delivered what the bus says there, including frames it never asked
after — which for a logger is closer to a duty than a defect.

### 11.4 Why the probe is an opcode, and Info stays about the device

The supported-PID masks could have gone in Info — it is where capability
lives, after all. They do not belong there, because Info describes the
device and the masks describe the car, and the two have different
lifetimes: Info is constant for a connection (§4 reads it once), while the
car changes every time the dongle moves. A probe result cached in a record
that is explicitly never re-read mid-connection would be a stale answer
with a normative excuse. `OBD_INFO` is therefore `GET_POWER`'s shape —
measured when asked, no timestamp, ask again for fresher — and Info carries
only the two numbers that really are the device's: how many PIDs fit a poll
set, and how fast it will transmit. Those live in the two bytes the
withdrawn `can_max_payload` and `max_notify_bytes` fields freed, which is
what reserved space is for (SPEC §11.2).

The probe also resolves the addressing question without an enum. Whether
the car answered 11-bit or 29-bit addressing is carried by bit 29 of
`request_id` — the same identifier layout as `can_record` and
`CAN_SUBSCRIBE` — so the format is derived from a value the client was
going to use anyway, rather than stated in a second field that could
disagree with it.

### 11.5 One PID per request, and the ISO-TP question that dissolves

Multi-PID Mode 01 requests exist — up to six PIDs in one frame — and were
rejected, because their responses routinely exceed seven bytes and arrive
as ISO-TP multi-frame transfers, which need the device to send flow
control frames and reassemble. One PID per request makes every response a
single frame *by arithmetic*: within `0x01`–`0x60` no Mode 01 response
exceeds four data bytes (J1979's own sizes), so `41 pid data` fits seven
bytes always. The device then needs no reassembly and — the half that
matters for §11.2 — **no flow control transmission**, so SPEC §15.1 can
forbid the primitive outright, and a conforming device is structurally
incapable of being drawn into a multi-frame exchange, its own or another
tester's. The window and the mask PIDs are the same boundary: `0x60` is
exactly where the three supported-PID masks stop, so "what can be
negotiated" and "what stays single-frame" are one line, not two.

The cost is request count: a client polling six PIDs sends six frames
where multi-PID packing would send one. At the floors involved — one short
frame per few tens of milliseconds, on a bus whose ECUs answer this
traffic for a living — the bus cost is negligible, and the complexity it
buys off the firmware (ISO-TP state, flow-control timing, per-ECU
reassembly buffers) was the single largest item in the role's original
scope estimate.

**This paragraph priced the cost wrongly, and SPEC §15.4.1 is the
correction.** Request count is bus load; what a client feels is cycle
latency, which against a fixed per-request floor is *k* × `interval_ms` of
sample period — a quantity this paragraph never names. And the bus cost of
grouping is not negligible but *zero*: SPEC §15.1 already pads every request
frame to eight bytes, so `[0x07, 0x01, p1…p6]` occupies exactly what
`[0x02, 0x01, pid]` occupies, and the response side gets strictly smaller.
Measured against the reference peripheral, twelve PIDs at a 20 ms floor go
from 4.1 Hz each to 9.9 Hz.

The rejection above stands **for the case it examined** — unbounded
multi-PID packing, whose answers do need ISO-TP. What it never separated out
is the bounded case: six response bytes fit a single frame by the same
arithmetic this section already relies on, the client owns the sizing
because SPEC §15.5 already puts the tables there, and a group sized wrong is
answered with a first frame that SPEC §15.5 already disposes of. So SPEC §15.4.1 adds
grouping under capability bit 11 without giving the device ISO-TP, flow
control, reassembly, or a PID length table — none of the complexity this
paragraph correctly refused to buy.

### 11.5a Why the poll clock became response-paced

The fixed clock made `interval_ms` do two unrelated jobs — how fast do I
sample, and how long will I wait before abandoning a request — and gave the
client nothing to choose between them with, because the protocol never told
it the car's latency. Every value is wrong for one of the two, and LapSmith,
the only client, picked a constant blind.

SPEC §15.4 now waits for the answer, floors nothing, and treats `interval_ms`
as a minimum the client may set to zero. Zero is admissible here where a
`periodic` subscription's `arg` (SPEC §6.8) could not be, and the difference is
exactly the pacing: a device that waits for an answer before transmitting
cannot generate traffic faster than the car produces it, so "no client
throttle" is bounded where "no limit" would not have been.

**`obd_min_interval_ms` was withdrawn with the clock it governed.** A device
is plugged into a car it has never met, so a rate it publishes as *safe* is a
guess about a vehicle it cannot see — the same guess a client makes when it
hard-codes an interval, relocated to the party with strictly less
information. Under pacing it also governs nothing the car is not already
governing. What replaces it is a discipline rather than a number: at most one
request outstanding, waits for its answer, never retries, transmits nothing a
client did not ask for. That is checkable by inspection and true on every
car, which a published interval never was.

The cost is stated where it is paid: SPEC §15.1's worst case is no longer a rate
a client can read out of Info before anything is transmitted. That property
was real, and it is gone. It bought a number no vendor could fill in
honestly, and this repository's own standard is that a field whose correct
answer nobody knows is worse than no field.

### 11.6 Why the rate is aggregate

Per-PID *rates* were considered for consistency with `periodic`
subscriptions and rejected on a difference that matters more than the
symmetry: a subscription's interval *filters traffic that exists anyway*,
so N of them cannot add a frame to the bus, while a poll interval
*generates traffic*, so N independent rates would make the device's bus
load the sum of a list only the client knows, with collisions the device
would have to arbitrate. One interval keeps the load-bearing sentence
speakable — at most one request per `interval_ms`, ever — and relative
rates survive as list composition: entries are ordered and may repeat, so
`[0C, 0D, 0C, 05]` samples engine speed twice per cycle. A schedule
expressed as data, not as a scheduler expressed as parameters.

**This argument holds for rates and not for divisors, and SPEC §15.4.2 is
the line between them.** N independent intervals make the bus load a sum
only the client knows; N divisors can only ever subtract from a load already
bounded, because a divisor causes a request to be *skipped* and never adds
one. The load-bearing sentence — at most one request per pass of the
schedule, ever — survives a divisor verbatim, which is the same test SPEC §15.4.1
had to pass. Refusing the mechanism that only reduces traffic while
permitting the one that increases it was backwards, and repetition alone left
a client no way to make a channel slower than the cycle: the only currency
for buying a ratio was `obd_poll_slots`, so a client wanting one channel
slower paid in channels it could no longer read at all.

`interval_ms` zero was refused here in the same breath, on the same
reasoning, and §11.5a is why that no longer holds: under response pacing zero
is not unbounded.

### 11.7 What it costs, and what was declined

The costs are real and stated. The role reports the *union* of the ECUs'
supported sets, not per-ECU masks — eight ECUs of per-ECU masks do not fit
a control response at the minimum MTU, so which ECU implements a PID is
learned by polling and watching response identifiers (SPEC §15.3). Mode 22
manufacturer DIDs — where the interesting signals live on gatewayed cars —
are out, with the reasoning in SPEC §15.9: they need the ISO-TP machinery
SPEC §15.1 forbids, and their DID space has no supported-mask to negotiate
against, so the declare-verify-use shape that justifies this role cannot
cover them. DTC reading and clearing are out; Mode 04 *writes to the
vehicle*, a categorically different act. Every one of these is a later
minor's opcode behind its own declaration if it ever comes, which is
SPEC §11.3 doing its job.

Declined outright: transmit-arbitrary-frame opcodes (§11.2 above),
device-side decoding (§11.3), a probe cached in Info (§11.4), automatic
tester-detection heuristics (SPEC §15.8 — a device whose transmit
behaviour varies with unmodelled traffic is a device whose behaviour
cannot be stated), and polling that survives disconnection (SPEC §15.7 —
transmit must not outlive the client that asked for it; the CAN role's
subscriptions already die with the link, and the transmitter holds to the
stricter version of the same rule).

### 11.8 Where "never retries" lands, and which instant is "transmitted"

Reported from the first device firmware before it had a transport, as a
question rather than a defect: a CAN controller retransmits a frame that was
not acknowledged or lost arbitration, below anything the firmware sees, and
SPEC §15.1 says a device MUST NOT retry an unanswered request and offers
inspection as the check. On a car the two never meet — any awake node
acknowledges a well-formed frame, so an unanswered request is on the wire
exactly once — but a dongle in a car with the ignition off is a node alone on
its bus, and a capture there shows the same Mode 01 request repeating at the
link layer's pace, thousands of times a second, none of them acknowledged. Three readings were on the table: the rule binds the
application, and the link layer is CAN's business; it binds the wire, so a
device MUST use single-shot transmission where its controller offers it and
MUST NOT declare bit 10 where it does not; or it binds only on a bus with a
car on it. The first won, and the question exposed two sentences the section
had needed all along.

**The rule binds what the device asks.** Its reason — recorded in the proposal
the pacing came from, `proposals/obd-response-paced-polling.md` — is that a
tester re-asking a question
an ECU declined to answer is how a tester becomes a fault: it adds load to a
bus that is already not behaving, at exactly the wrong moment. That reason
needs a request the ECU received. Link-layer retransmission is ISO 11898-1's
error recovery, which every node on the bus performs and every ECU relies
on; a device that switched it off would be the one node on the bus behaving
unlike the others, and less reliable for it rather than safer. Single-shot
also drops a frame that lost arbitration, which is not an error at all:
`0x7DF` is near the bottom of the priority order, and on a loaded bus a
request on it loses arbitration routinely. The request would then be
abandoned as unanswered when the car was never asked, and the client reads a
gap SPEC §15.4 promises is the truth off a PID the car answers — the plausible
wrong value SPEC §1.1 forbids, produced by the rule meant to prevent harm. So
the wire reading costs every car something to change what happens on a bus
with no car on it.

**And on that bus it changes less than it looks.** Under single-shot the frame
goes out once and is abandoned; the loop comes round; on a one-group
schedule the next request is the same request. The capture still shows the
same frame repeating, at 10 Hz instead of the controller's pace. What
distinguishes a retry from the loop coming round was never how many times a
frame appears — it is whether the frame is a second *acknowledged* request
for what an acknowledged, unanswered one already asked, and a capture shows
the ACK slot. The slot, and not an error flag: a frame that lost arbitration
leaves no flag because it was never on the wire, and once the lone node is
error-passive its flags are six recessive bits that a capture cannot tell
from idle. SPEC §15.1 now says so, and the inspection claim holds with that
reading rather than in spite of it. ISO 11898-1 is also kinder to the
lone node than the report assumed: its transmit error counter stops
climbing at error-passive when the only fault is a missing acknowledgement,
so a node alone on a bus repeats indefinitely rather than going bus-off, and
SPEC §15.7 already covers a controller that does.

**"Transmitted" had no instant.** SPEC §15.1 measured `OBD_RESPONSE_TIMEOUT_MS`
from when the request "was transmitted" and never said when that was. With
automatic retransmission there is no single instant the firmware can see
except the controller's completion, which fires when the frame is
acknowledged — and on a bus that never acknowledges, never. A device that
timed from the hand-over instead would abandon the pending frame at 100 ms
and hand the controller the next one, behind it: a queue that empties as a
burst the moment a node wakes, several requests outstanding and none of them
spaced, on the one bus where the firmware could least see it happening. The
instant is now the acknowledgement, which is also the instant ISO 15765-4
measures P2 from, so the 100 ms window measures what it was sized against. A
pending frame is the outstanding request, and the poll loop waits for it —
there is nothing to poll on a bus that will not carry a frame, and the loop
stalling there is the truth about that bus. The probe cannot wait, because it
owes a control response, so SPEC §15.2 withdraws a probe frame still pending
after the same 100 ms and counts it as unanswered; a probe of a sleeping car
concludes `responded` clear in a few hundred milliseconds, which is also the
truth. The probe also begins by clearing the poll set and withdrawing the
loop's pending frame, where an earlier draft cleared the set only when the
probe completed: a probe that had to wait behind a pending poll frame could
never start on the one bus that needs it, and the control plane would be
`busy` until link loss. And SPEC §15.7 withdraws whatever is pending when the
poll set clears: a
frame left in the controller after link loss goes out when the driver turns
the key, minutes after the client went away — the transmission that section
exists to make impossible, reachable only through a state the section had
not named.

Declined: a bound on how long the poll loop lets a frame pend. On a live bus
the frame goes out in milliseconds and the bound never trips; on a bus with
no node it withdraws one frame so the loop can hand over the next, which
pends identically — a controller abort every 100 ms that changes nothing on
the wire. Also declined: requiring single-shot where the controller offers
it. The reporter's controller offers it in silicon and its driver refuses
it, and a rule met only by working behind a driver's back, on a reading of
what a section wants, is not one this specification should write.

---

## Contradictions found by review, and how each was closed

SPEC.md states rules; this is why these particular rules are the ones it
states. Every entry below was a place where two parts of the specification, or
the specification and its own conformance suite, said different things — the
class of defect that produces two implementations which each pass every test
and cannot talk to each other. The full history is in CHANGELOG.md.

**"Per matching identifier" read as naming the key, and it does not.**
SPEC §6.8 says `periodic` state is kept per matching identifier, which exists
to forbid one interval shared across a masked subscription's identifiers. The
first implementation read it as the whole key — state keyed by identifier
alone — and a subscription's rate limit was then destroyed the moment a second
subscription matched one of its identifiers: removing the narrower one let the
broader one forward immediately, though it had been installed throughout and
its interval had not elapsed. A once-a-minute subscription delivered three
frames in twenty milliseconds, every one of them well-formed. The key is the
`(subscription, identifier)` pair, and SPEC §9.2 decides which subscription
forwards a frame rather than which subscriptions still exist. No byte vector can reach
this: every frame involved decodes perfectly, and the defect is in when they
arrive.

**A byte-identical retry and the first-frame promise disagreed.** SPEC §9.1
makes a re-install update `mode` and `arg` in place, unconditionally, as an
operation; SPEC §6.8 promises the first matching frame after an install;
SPEC §9.4 tells
a client it MAY retry any request whose response it did not receive and that
doing so is harmless. For a retry that changes nothing, those three do not say
whether the first frame re-arms — and the observable difference is an extra
frame inside the client's own rate limit, with nothing on the wire to explain
it, because a lost response and a delivered one are identical at the client.
SPEC §9.4's guarantee is the one that has to hold: a request changing neither `mode`
nor `arg` now changes nothing at all, the schedule included. A request that
changes either is not a retry, and re-arms the first frame — which is also what
a client changing its mind wants, since it is asking to see the signal on new
terms.

**A control response had three completion points and one of them was the
client's.** SPEC §9 said a device "owes" a response, that a tag frees when its
response is "sent", and that a request is refused while one is "outstanding" —
three words agreeing only while a single response is in flight, which the
`busy` refusal breaks by being a response itself. Reported by the first
implementation outside this repository. The first fix chose the confirmation
as the boundary and introduced a worse contradiction than it closed: SPEC §9
also tells a *client* to write again as soon as the response arrives, which
ATT permits before its confirmation goes out, so a device owing until the
confirmation refuses a client obeying the rule and the retry meets the same
window. The boundary is the send, which falls no later than the client's
(RATIONALE §8.7) — and what a device must be able to hold is one response
beyond the one in flight, because that window is where the conforming client's
next request lands.

**Capability implications were nowhere.** SPEC.md defined each capability bit
independently while `conformance/run.py` carried a hard-coded table making
`can` and `monitor` imply `control` — the runner enforcing a rule the
specification did not state. Canonical Info vectors meanwhile described a CAN
device with no Control characteristic, which no client can install a
subscription on. SPEC.md §4.1 is now one generated matrix and the runner reads it.

**Omitting unused characteristics loses to a fixed table.** The choice looks
like a matter of taste and is not. Central stacks cache the attribute table
across connections and some across reboots, so a device whose table changes
between connections hands the client a stale handle, and the symptom is a read
of the wrong characteristic rather than a missing one. A fixed table cannot
produce that. The cost is bounded deliberately: an inert characteristic
rejects writes with an ATT error and implements nothing else, so a GPS-only
build is a service declaration, four inert attributes and one notify path.

**`max_notify_bytes` could not mean the negotiated MTU.** A client reads Info
as its first act after connecting; a CoreBluetooth peripheral does not learn
the negotiated maximum until a central subscribes, which is strictly later.
Defined as the live value it was a field whose correct answer did not exist
yet at the only moment anyone read it. It was redefined as a device ceiling —
and then removed altogether (§8.2): a notification never exceeds the
negotiated ATT payload, which the client's own stack already knows, so even
the ceiling was a second statement of a bound the client has. Bytes 22–23 of
Info were reserved, and SPEC §15 has since assigned them to
`obd_min_interval_ms`.

**Monitor freshness had two rules and a third to reconcile them.** `max_age` of
zero meant "no deadline of its own", and a derived device-wide "liveness bound"
— the largest deadline declared — then expired those channels anyway. Three
concepts for one question, and the canonical four-channel vector satisfied none
of them. Requiring every declared channel to carry a non-zero `max_age` deletes
the other two. A channel that changes rarely takes the 25.5 s ceiling rather
than an exemption.

**`dropped` is a diagnostic, not an audit trail.** Making it exact means owning
a counter transactionally across encoding, transmit-queue refusal and
supersession. The question it answers — "is my link bad, or is the device
overrun?" — survives a count landing one notification late, so SPEC.md §8.3 says
best-effort and spends the exactness on `seq` instead, where it is cheap:
`seq` counts notifications actually sent and is committed when the transport
accepts one.

**Rate setting was undefined in four ways** — zero, unsupported rates,
ceilings, and when the change takes effect. SPEC.md §9.6 states them. There is
deliberately no way to enumerate supported rates: asking and finding out is one
round trip on a link the client already has, and a discovery mechanism would be
a list format, a paging scheme and a second thing to keep in step with Info.

**Both RTK bits could be set at once.** The natural client reading of the pair
is "fixed wins", which upgrades a device's accuracy claim on the strength of a
bug. SPEC.md §5.3 makes them exclusive and makes either imply `differential`.
The rule is the device's: the fix is well-formed, so a receiver decodes it,
MUST NOT read the pair as either solution, and SHOULD flag the contradiction —
the review pass in §8.3 is where the receiver-side reject this paragraph once
described was downgraded, with the rest of the content rules.

**"Satellites used in the solution" named two solutions.** SPEC §5 opens by
scoping the record to a position solution, and `num_sv` one row below `p_dop`
says "the solution", so a `time_only` fix — the ordinary state of a u-blox
receiver through most of the first thirty seconds of a cold start — had two
readings available in the document and they disagreed about whether bit 11
could be set. SPEC §5.1 leaves no third state: the bit is set and the field is
a measurement, or it is clear and the field is absent. Two conforming devices
therefore reported one receiver differently and nothing on the wire told a
client which convention it was reading — an absent `num_sv` meaning "no
satellites contributed" under one and "this device declines to count them
without a position" under the other. Reported by the first firmware
implementation, which had withheld it. The count won. A time solution is
computed from real satellites and their number is a measurement of a real
thing, and this document's own reason for decoding an out-of-range latitude —
hiding the evidence from the person best placed to notice costs more than
surfacing it — applies unchanged to hiding a satellite count for the whole of
an acquisition. SPEC §1.1 forbids a plausible *wrong* value, and this one is
right; what scopes it is the `fix_type` byte beside it, which has no validity
bit and is present on every fix. So SPEC §5.2 now scopes `num_sv` to whatever
solution `fix_type` names and `p_dop` to a position's geometry, and says the
two adjacent bits do not move together. What the adjective in its description
settled for `p_dop` is stated rather than inferred, and `fix_type = none` — no
solution for a satellite to have been used in — leaves bit 11 clear, so the
count of satellites *tracked* cannot borrow the name of the count *used*. The
premise underneath both — that a `fix_type` of `none` or `time_only` reports no
position — is now a rule of its own rather than an assumption two other rules
lean on, and SPEC §5 no longer opens by calling every notification a position
solution, which is the sentence the narrow reading came from.

**The union over every ECU that answered had no record once a ninth
answered.** SPEC §15.3 scoped the three `supported_*` masks to "every ECU that
answered"; SPEC §15.2 capped the entry list at eight and required `count` to
be non-zero whenever anything answered. The three are consistent while at most
eight answer, and on 11-bit functional addressing nothing else can: ISO
15765-4 legislates eight response identifiers, `0x7E8`–`0x7EF`, so the cap
there is arithmetic rather than a rule. The 29-bit fallback the same section
*requires* is not so bounded — responses to `18DB33F1` arrive on `18DAF1xx`,
whose low byte is the responder's source address, which is 256 identifiers —
so a ninth responder is an ordinary bus reached by the prescribed fallback,
and for it no conforming record existed at all: the list could not name nine,
`count` could not say nine, and `responded` could not be clear because
something plainly answered. Reported by the first device firmware, from the
addressing rather than from a car. Two readings were available and neither was
free. A literal union with eight listed makes the record claim a PID whose
only responder it does not name — SPEC §15.4 lets a client poll it,
SPEC §15.5's fallback delivers only on reported identifiers, and the device
then transmits onto a car for a reply the client cannot receive, which is the
shape SPEC §15.1's bounds exist to make unexpressible. A union over the eight
reported is self-consistent and under-reports the car. The self-consistent one
won, with the under-report made visible rather than accepted silently:
SPEC §15.2 now names the selection — the eight numerically *lowest*
identifiers, because arrival order is not reproducible and the
ascending-entry rule exists so that two conforming devices probing one car
produce identical bytes — SPEC §15.3 scopes the union to the ECUs the record
reports, and `obd_validity` bit 1 (`truncated`) says the masks are a floor
rather than a census. That bit is what makes the choice conform to SPEC §1.1
rather than merely satisfy SPEC §15.2: a silently truncated union has a
client read "unsupported" off a car that supports the PID, which is a
plausible wrong value, and no byte vector can catch it because every byte of
the record is well-formed. Raising the cap
was considered and declined — `count` is a `u8` and an entry is four bytes, so
a control response at the minimum ATT MTU holds eighteen entries rather than
eight — but a higher cap moves the cliff instead of closing it, and the
truncation rule would still have to exist at nineteen.

**An IMU batch could carry no samples**, though `t_base` is defined as the
acquisition time of sample 0. The same reasoning was then applied to CAN:
SPEC.md §6.2 forbids an empty CAN batch too, because its `t_base` is defined
as the bus-arrival time of record 0 and a batch with no record 0 timestamps a
frame that does not exist. A quiet bus is reported by sending nothing.

## What the reference peripheral cannot observe

A CoreBluetooth peripheral is never told about a connect or a disconnect: the
delegate has `didSubscribeToCharacteristic` and
`didUnsubscribeFromCharacteristic` and no connect/disconnect pair at all. bless
0.3.0's `is_connected()` therefore reports "at least one central is subscribed
to at least one characteristic".

So the reference peripheral's connection edge is not a link edge, and a client
that unsubscribes from everything while staying connected is indistinguishable
from one that went away. It resets anyway, because the two mistakes are not
equal: resetting on a resubscribe costs the client a CAN table and a `seq`
restart it must already tolerate and can see, while failing to reset on a real
reconnection hands the next connection the previous one's state in a way
SPEC.md §8.2 and SPEC.md §9.2 exist to prevent and no client can detect.

This is a property of that backend, not of VTP/1. A BlueZ peripheral has a real
`InterfacesRemoved` signal and needs none of it.

The synthetic CAN bus has no link layer. Every frame the device hands over is
acknowledged at the instant it is handed over, so the two instants SPEC §15.1
distinguishes — hand-over and acknowledgement — coincide, a frame is never
pending, and nothing there can exercise the rules that follow from the
difference: the poll loop waiting on a pending frame, the probe withdrawing
one at `OBD_RESPONSE_TIMEOUT_MS`, SPEC §15.7 withdrawing one when the poll set
clears. The harness's loopback transport is the same bus, so it cannot
either. Those rules are tested by a controller on a bus with no other node on
it, which only a device can be plugged into.
