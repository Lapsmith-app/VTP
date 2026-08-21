# VTP/1 — Vehicle Telemetry Protocol over Bluetooth LE

**Normative specification · Protocol major 1 · Specification status: draft**

> **This document is a draft (`v0.x`) and the wire format may change without
> notice.** The compatibility guarantees in §11 take effect at specification
> version 1.0 and not before. Do not ship hardware against this document yet.
> [README.md](README.md) carries current status.

Two version numbers appear in this document and they are independent.
**Protocol major 1** is the wire identity: it fixes the service UUID family
(§3.1) and the value of `protocol_major` (§4), and it is what "VTP/1" names.
The **specification version** is this document's own release, and has not yet
reached 1.0.

The key words MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD, SHOULD NOT,
RECOMMENDED, MAY and OPTIONAL are to be interpreted as described in
[RFC 2119](https://www.rfc-editor.org/rfc/rfc2119) and
[RFC 8174](https://www.rfc-editor.org/rfc/rfc8174), and only when they appear in
all capitals.

Rationale, trade-offs and comparisons belong in [RATIONALE.md](RATIONALE.md).
This document is normative and deliberately terse.

---

## 1. Scope and conformance

VTP/1 carries GNSS position, CAN-bus frames and inertial samples from a
purpose-built device to a host application over Bluetooth LE GATT.

An implementation is **conforming** if it:

1. Implements at least one of the GPS, CAN or IMU roles.
2. Declares exactly what it implements in the Info characteristic (§4).
3. Passes the conformance vectors in `conformance/vectors/` for every role it
   declares (§12).

A device MAY implement any subset of roles. A client MUST NOT assume a role is
present because the service is.

### 1.1 The governing principle

**No VTP/1 receiver may ever produce a plausible wrong value.** Where a
specification choice trades bytes against ambiguity, this specification spends
the bytes. Three consequences run through every section below and a conforming
implementation MUST honour all three:

- Absence is signalled **only** by a validity bit or a presence flag, never by a
  reserved value of the field itself.
- Anything unrecognised — an enum value, a bitmask bit, an extension type — is
  reported as unrecognised. It MUST NOT be coerced to a default.
- Anything malformed — a short payload, a truncated record — is rejected whole.
  A receiver MUST NOT decode the prefix of a malformed payload.

---

## 2. Transport

| Requirement | Value |
| --- | --- |
| GATT role | Device is peripheral, host application is central |
| Byte order | **Little-endian, every field, no exceptions** |
| Minimum ATT MTU | 100 |
| Link-layer payload | Largest the controller supports, up to 251 octets |
| PHY | Device SHOULD request LE 2M; MUST function on LE 1M |
| Connection interval | Device SHOULD request 15 ms, peripheral latency 0, while streaming |

A device MUST function correctly at an ATT MTU of 100 and MUST use up to the
negotiated maximum when batching (§6, §7).

A device MUST implement the Bluetooth SIG Device Information Service
(`0x180A`) and MUST populate Manufacturer Name, Model Number and Firmware
Revision.

Signed integers are two's complement. Reserved fields MUST be written as zero
and MUST be ignored on receive.

The three subsections below are the only requirements in this specification that
the conformance corpus cannot test, because none of them appears in any payload.
§12.1 says what follows from that; `GET_LINK_PARAMS` (§9.1) is how a client
checks them at run time.

### 2.1 Link-layer payload

A device MUST negotiate the largest link-layer payload its controller supports,
up to `max_tx_octets` and `max_rx_octets` of 251.

A large ATT MTU is not by itself a throughput figure. Sent over the 27-byte
default link-layer payload, a 247-byte notification is fragmented across ten or
more packets, each carrying its own header, inter-frame spacing and
acknowledgement — roughly three times the radio airtime for the same bytes. A
device that negotiates a large MTU without extending the link-layer payload has
not gained the throughput the MTU implies, and takes that airtime from every
other peripheral sharing the central's radio.

### 2.2 PHY

A device SHOULD request the LE 2M PHY and MUST function correctly on the LE 1M
PHY. The 2M PHY halves the airtime of a given payload; no other part of this
specification changes with it.

### 2.3 Connection parameters

The connection interval and peripheral latency are granted by the central, not
chosen by the device. A device MUST function correctly at whatever values the
central applies, including values it did not request and values that change
during a connection.

A device MUST NOT assume it received the interval it requested; a central
serving several peripherals commonly grants 30 ms or more. Batch flush timing
(§6.1, §7) MUST therefore follow the device's own clock rather than an assumed
interval, and MUST respect the `dt` bound of §6.1 in every case.

---

## 3. Discovery

### 3.1 UUID allocation

<!-- BEGIN GENERATED: uuids -->
| Role | UUID |
| --- | --- |
| Service (VTP/1) | `56545001-5f05-5b56-af87-dcab2baf2522` |
| Characteristic `info` | `56544301-5f05-5b56-af87-dcab2baf2522` |
| Characteristic `gps` | `56544302-5f05-5b56-af87-dcab2baf2522` |
| Characteristic `can` | `56544303-5f05-5b56-af87-dcab2baf2522` |
| Characteristic `imu` | `56544304-5f05-5b56-af87-dcab2baf2522` |
| Characteristic `control` | `56544305-5f05-5b56-af87-dcab2baf2522` |
| Characteristic `monitor_values` | `56544306-5f05-5b56-af87-dcab2baf2522` |
<!-- END GENERATED: uuids -->

These values are **frozen** for the life of major version 1.

### 3.2 The family prefix

<!-- BEGIN GENERATED: family_prefix -->
Every VTP service UUID begins with the four bytes `56 54 50 MM`, where `56 54 50` is ASCII `"VTP"` and `MM` is the major version. Characteristic UUIDs begin `56 54 43 NN` (ASCII `"VTC"` and an index) and share the service's remaining twelve bytes.
<!-- END GENERATED: family_prefix -->

A client MAY use this structure to recognise a VTP device whose major version it
does not implement, and SHOULD report that condition to the user rather than
letting the device appear absent. A client MUST NOT attempt to parse any
characteristic of a major version it does not implement.

### 3.3 Advertisement

A device MUST advertise its VTP service UUID. It SHOULD additionally include
Service Data for that UUID, three bytes:

| Off | Size | Field |
| --- | --- | --- |
| 0 | 1 | `protocol_minor` |
| 1 | 1 | `capabilities` bits 0–7 (§4) |
| 2 | 1 | Device class — a display hint only; carries no protocol meaning |

Service Data is advisory. A client MUST NOT rely on it in place of reading the
Info characteristic, and MUST re-read Info on every connection regardless of
what the advertisement said.

---

## 4. Info characteristic — READ

<!-- BEGIN GENERATED: info -->
*Device self-description. Read once per connection; never cached across connections.*

Total: **24 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 1 | `u8` | `protocol_major` | MUST equal 1; cross-check against the discovered service UUID |
| 1 | 1 | `u8` | `protocol_minor` | — |
| 2 | 4 | `u32` | `capabilities` | bitmask `capabilities` |
| 6 | 2 | `u16` | `gps_rate_hz` | `Hz`; Current rate; 0 if no GPS |
| 8 | 2 | `u16` | `gps_max_rate_hz` | `Hz` |
| 10 | 2 | `u16` | `can_subscription_slots` | — |
| 12 | 4 | `u32` | `can_max_frames_per_s` | `frames/s` |
| 16 | 2 | `u16` | `imu_rate_hz` | `Hz` |
| 18 | 2 | `u16` | `imu_max_rate_hz` | `Hz` |
| 20 | 1 | `u8` | `can_max_payload` | 8 for classic CAN, 64 for CAN FD |
| 21 | 1 | `u8` | `clock_flags` | bit0 GNSS-disciplined, bit1 clock survives reconnect |
| 22 | 2 | `u16` | `max_notify_bytes` | `bytes` |
<!-- END GENERATED: info -->

`capabilities` bits:

<!-- BEGIN GENERATED: bitmask:capabilities -->
| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `gps` | — |
| 1 | `can` | — |
| 2 | `imu` | — |
| 3 | `monitor` | — |
| 4 | `control` | — |
| 5 | `can_fd` | — |
| 6 | `masked_subscriptions` | — |
| 7 | `on_change_subscriptions` | — |
| 8+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:capabilities -->

A client MUST read this characteristic on every connection and MUST NOT cache it
across connections. A DIY device is reflashed by its owner: its minor version,
capability set and rate ceilings can all change while its Bluetooth address does
not.

If `protocol_major` does not match the major version implied by the discovered
service UUID, the client MUST treat the device as non-conforming and disconnect.

A capacity field of zero means "none", not "unspecified". A client MUST NOT
substitute a default for any capacity it did not read.

---

## 5. GPS characteristic — NOTIFY

One notification carries exactly one position solution. There is no pairing
between characteristics and no reassembly.

<!-- BEGIN GENERATED: gps_fix -->
*One complete position solution. Never split, never paired, never packed.*

Total: **74 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 2 | `u16` | `seq` | Notifications sent on this characteristic; +1 each, wraps, restarts at 0 per connection |
| 2 | 2 | `u16` | `dropped` | Fixes accepted then discarded since the previous notification; saturates, never wraps |
| 4 | 4 | `u32` | `validity` | bitmask `gps_validity` |
| 8 | 8 | `u64` | `t_device` | `us`; Monotonic device clock |
| 16 | 8 | `i64` | `t_utc` | `ms`; valid when `validity` bit 0 (`t_utc`) is set; Unix epoch |
| 24 | 4 | `i32` | `lat` | `deg`; scale 1e-07; valid when `validity` bit 2 (`position`) is set |
| 28 | 4 | `i32` | `lon` | `deg`; scale 1e-07; valid when `validity` bit 2 (`position`) is set |
| 32 | 4 | `i32` | `alt_msl` | `mm`; valid when `validity` bit 3 (`alt_msl`) is set |
| 36 | 4 | `i32` | `alt_ellipsoid` | `mm`; valid when `validity` bit 4 (`alt_ellipsoid`) is set |
| 40 | 4 | `i32` | `vel_n` | `mm/s`; valid when `validity` bit 5 (`velocity`) is set |
| 44 | 4 | `i32` | `vel_e` | `mm/s`; valid when `validity` bit 5 (`velocity`) is set |
| 48 | 4 | `i32` | `vel_d` | `mm/s`; valid when `validity` bit 5 (`velocity`) is set |
| 52 | 4 | `i32` | `head_mot` | `deg`; scale 1e-05; valid when `validity` bit 6 (`head_mot`) is set |
| 56 | 4 | `u32` | `h_acc` | `mm`; valid when `validity` bit 7 (`h_acc`) is set; 1 sigma |
| 60 | 4 | `u32` | `v_acc` | `mm`; valid when `validity` bit 8 (`v_acc`) is set; 1 sigma |
| 64 | 4 | `u32` | `s_acc` | `mm/s`; valid when `validity` bit 9 (`s_acc`) is set; 1 sigma |
| 68 | 2 | `u16` | `p_dop` | scale 0.01; valid when `validity` bit 10 (`p_dop`) is set |
| 70 | 1 | `u8` | `fix_type` | enum `fix_type` |
| 71 | 1 | `u8` | `num_sv` | valid when `validity` bit 11 (`num_sv`) is set |
| 72 | 1 | `u8` | `fix_flags` | bitmask `fix_flags` |
| 73 | 1 | `u8` | `ext_count` | Extension records following the base record |
<!-- END GENERATED: gps_fix -->

### 5.1 Validity

<!-- BEGIN GENERATED: bitmask:gps_validity -->
| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `t_utc` | t_utc carries a GNSS time solution |
| 1 | `t_utc_resolved` | Leap seconds fully resolved |
| 2 | `position` | lat and lon are valid |
| 3 | `alt_msl` | — |
| 4 | `alt_ellipsoid` | — |
| 5 | `velocity` | vel_n, vel_e and vel_d are all valid |
| 6 | `head_mot` | — |
| 7 | `h_acc` | — |
| 8 | `v_acc` | — |
| 9 | `s_acc` | — |
| 10 | `p_dop` | — |
| 11 | `num_sv` | — |
| 12+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:gps_validity -->

For every field governed by a validity bit:

- If the bit is **set**, the field carries a measurement.
- If the bit is **clear**, the device MUST write the field as zero, and the
  receiver MUST report the field as absent.

A receiver MUST NOT treat a zeroed field with a clear validity bit as a
measurement of zero. No field value anywhere in VTP/1 signals absence.

### 5.2 Fix type

<!-- BEGIN GENERATED: enum:fix_type -->
| Value | Name | Meaning |
| --- | --- | --- |
| 0 | `none` | No position solution |
| 1 | `dead_reckon` | Dead-reckoning solution only |
| 2 | `fix_2d` | 2D position solution |
| 3 | `fix_3d` | 3D position solution |
| 4 | `gnss_dr` | Combined GNSS and dead-reckoning |
| 5 | `time_only` | Time solution only, no position |
| *other* | *unknown* | MUST decode as unknown, never as a default |
<!-- END GENERATED: enum:fix_type -->

### 5.3 Fix flags

<!-- BEGIN GENERATED: bitmask:fix_flags -->
| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `differential` | Differential corrections applied |
| 1 | `rtk_float` | — |
| 2 | `rtk_fixed` | — |
| 3 | `clock_disciplined` | t_device was GNSS-disciplined at this sample |
| 4+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:fix_flags -->

### 5.4 Derived quantities

Ground speed is `hypot(vel_n, vel_e)` and is exact. A device MUST NOT report a
separately computed scalar ground speed; the velocity vector is the only
representation.

`head_mot` is the receiver's filtered heading of motion and MAY differ from
`atan2(vel_e, vel_n)`. A client SHOULD prefer `head_mot` when its validity bit
is set.

### 5.5 Extension records

`ext_count` extension records MAY follow the base record. Each is:

| Off | Size | Type | Field |
| --- | --- | --- | --- |
| 0 | 1 | `u8` | Extension type |
| 1 | 1 | `u8` | Payload length in bytes |
| 2 | *len* | — | Payload |

A receiver MUST skip an extension whose type it does not recognise, advancing by
its length. For a recognised type whose length exceeds what the receiver
understands, the receiver MUST parse the prefix it understands and skip the
remainder. A receiver MUST NOT reject an extension because its length differs
from the expected value.

The notification length MUST equal the base record plus exactly the bytes
accounted for by `ext_count` extension records. Any other length MUST be
rejected.

---

## 6. CAN characteristic — NOTIFY

A notification is one batch header followed by `count` frame records.

<!-- BEGIN GENERATED: can_header -->
*Batch header. Followed by `count` can_record entries.*

Total: **16 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 2 | `u16` | `seq` | Notifications sent on this characteristic; +1 each, wraps, restarts at 0 per connection |
| 2 | 2 | `u16` | `dropped` | Frames accepted then discarded since the previous notification; excludes frames no subscription matched; saturates |
| 4 | 8 | `u64` | `t_base` | `us`; Bus-arrival time of record 0 |
| 12 | 1 | `u8` | `count` | — |
| 13 | 1 | `u8` | `flags` | bit0 device is shedding load |
| 14 | 2 | `u16` | `reserved` | **reserved — MUST be zero** |
<!-- END GENERATED: can_header -->

<!-- BEGIN GENERATED: can_record -->
*One CAN frame with a device-measured bus-arrival time.*

Total: **7 bytes + `payload`**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 2 | `u16` | `dt` | `10us`; Ticks since t_base; window is 0..655.35 ms |
| 2 | 4 | `u32` | `id` | bits 0-28 arbitration id; b29 extended; b30 CAN FD; b31 RTR |
| 6 | 1 | `u8` | `len` | Payload length, 0..64 |
<!-- END GENERATED: can_record -->

### 6.1 Timestamps

`dt` counts 10 µs ticks from `t_base`, so a frame's bus-arrival time is
`t_base + dt × 10` microseconds on the device clock (§8).

`dt` spans 0 … 655 350 µs. A device MUST emit a batch before `dt` would exceed
that range, which bounds worst-case batch latency at 655.35 ms. A device SHOULD
flush far more frequently — once per connection interval is RECOMMENDED.

`t_base` MUST be the bus-arrival time of record 0, measured by the device, not
the time the notification was queued or sent.

### 6.2 Batches

`count` MAY be zero. An empty batch means the bus is quiet; it is not an error
and a receiver MUST accept it.

A receiver MUST reject a notification whose length does not exactly match the
header plus `count` complete records.

### 6.3 Loss

`dropped` is defined in §8.3. A device MUST report discards there rather than
silently omitting frames, and MUST NOT count frames that matched no
subscription — those were never accepted. A client SHOULD surface a non-zero
`dropped` to the user.

`flags` bit 0 indicates the device is actively shedding load.

### 6.4 Identifier validity

A standard frame is one whose `id` bit 29 is clear. Its arbitration identifier
is eleven bits, so bits 11–28 MUST be zero. A receiver MUST **reject the whole
batch** if a standard frame carries a larger identifier: an eleven-bit
identifier that does not fit in eleven bits is malformed, and the only
alternative is to truncate it to a different identifier that looks entirely
valid.

Bit 30 (CAN FD) and bit 31 (RTR) MUST NOT both be set. CAN FD has no remote
frames, so the combination describes a frame that cannot exist.

A remote frame carries no data. If bit 31 is set, `len` MUST be zero.

Each of these is rejected rather than repaired, under §1.1: a repaired frame is
a plausible wrong value with a correct-looking identifier.

### 6.5 What a remote frame does not carry

The data length a remote frame *requests* is not represented in major version
1. `len` is the payload length, and the batch's length arithmetic depends on it
being exactly that, so it cannot also carry a requested size.

A logger therefore records that a remote frame for an identifier occurred, and
not how many bytes it asked for. This is a deliberate limitation and not an
oversight: remote frames are rare on the vehicle buses this protocol targets,
and the alternative is a second length field on the highest-volume record in
the specification.

### 6.6 CAN FD flags not carried

Bit Rate Switch and Error State Indicator are not represented. Both are
per-frame, so carrying them costs a byte on `can_record` — at 4000 frames per
second that is 4 kB/s on the one stream that can saturate a link, which is the
finding in RATIONALE §4.1.

They are bus diagnostics rather than vehicle telemetry, and no device
implements this specification yet, so nothing is known to need them. Record
sizes are frozen for the life of a major version (§11.3), which means adding
them later is a VTP/2 change rather than a minor one. That is the cost of this
decision and it is stated plainly rather than discovered.

### 6.7 When a timestamp is taken

`t_base` and every `dt` are bus-arrival times measured at the **end of the
frame** — the point at which the controller signals a completed reception.

Not start-of-frame. A device cannot generally know a frame's time on air
without knowing the bit timing and the number of stuffed bits, so a
start-of-frame timestamp would be back-computed rather than measured, and this
specification prefers a measurement it can defend to an estimate it cannot.

The consequence is that a long frame is stamped later than a short one relative
to the moment it began, by its own transmission time. At 500 kbit/s a 64-byte
CAN FD frame is roughly a millisecond of that; at 1 Mbit/s an 8-byte classic
frame is well under a tenth. A client aligning below a millisecond should know
which end of the frame it is aligning to.

### 6.8 Subscription modes and the first frame

The first matching frame is forwarded in every mode. A client that installs a
subscription and then waits for a value it can display should not have to wait
for a second frame to arrive.

| Mode | `arg` | Behaviour after the first frame |
| --- | --- | --- |
| `every_frame` | ignored | Every matching frame. |
| `periodic` | minimum interval, ms | At most one frame per `arg` ms. `arg` 0 means no limit. |
| `on_change` | debounce, ms | Forwarded when the payload differs from the last one forwarded, and not within `arg` ms of it. `arg` 0 means no debounce. |
| `every_nth` | N | Every Nth frame after the first. N MUST be at least 1; a device MUST answer `bad_params` to N of 0. |

`on_change` compares the payload only. A frame whose payload is unchanged is
not forwarded even though its arrival time differs, which is the point of the
mode; a client that needs arrival times needs `every_frame`.

### 6.9 One bus

Major version 1 addresses a single CAN bus. A device with more than one
transceiver cannot say which bus a frame arrived on, and the subscription
commands have no bus parameter.

The low byte of `can_header.reserved` is earmarked for a bus index so a later
minor version may add one. Until then it MUST be written as zero and MUST be
ignored on receive. Subscribing per bus would need a new opcode, which a minor
version may add (§11.2), so this is a gap that can be closed without a major
version.

---

## 7. IMU characteristic — NOTIFY

<!-- BEGIN GENERATED: imu_header -->
*Batch header. Followed by `count` evenly spaced imu_sample entries.*

Total: **20 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 2 | `u16` | `seq` | Notifications sent on this characteristic; +1 each, wraps, restarts at 0 per connection |
| 2 | 2 | `u16` | `dropped` | Samples accepted then discarded since the previous notification; saturates, never wraps |
| 4 | 8 | `u64` | `t_base` | `us`; Timestamp of sample 0 |
| 12 | 4 | `u32` | `period` | `us`; Interval between samples |
| 16 | 1 | `u8` | `count` | — |
| 17 | 1 | `u8` | `flags` | bit0 accel present, bit1 gyro present |
| 18 | 2 | `u16` | `reserved` | **reserved — MUST be zero** |
<!-- END GENERATED: imu_header -->

<!-- BEGIN GENERATED: imu_sample -->
*Sensor-frame acceleration and rotation. Vehicle alignment is the client's job.*

Total: **12 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 2 | `i16` | `ax` | `mg`; present when `imu_header.flags` bit 0 (`accel`) is set; milli-g |
| 2 | 2 | `i16` | `ay` | `mg`; present when `imu_header.flags` bit 0 (`accel`) is set |
| 4 | 2 | `i16` | `az` | `mg`; present when `imu_header.flags` bit 0 (`accel`) is set |
| 6 | 2 | `i16` | `gx` | `deg/s`; scale 0.05; present when `imu_header.flags` bit 1 (`gyro`) is set |
| 8 | 2 | `i16` | `gy` | `deg/s`; scale 0.05; present when `imu_header.flags` bit 1 (`gyro`) is set |
| 10 | 2 | `i16` | `gz` | `deg/s`; scale 0.05; present when `imu_header.flags` bit 1 (`gyro`) is set |
<!-- END GENERATED: imu_sample -->

Samples are evenly spaced: sample *i* is at `t_base + i × period` microseconds.

`period` is expressed as an interval rather than a rate because real sensor
output data rates are not integer hertz. A device MUST report its actual sample
interval, rounded to the nearest microsecond.

That rounding is the one place in VTP/1 where a value is approximate, so its
bound is stated rather than left to be discovered. An output data rate whose
true interval is not a whole number of microseconds is misrepresented by up to
0.5 µs per sample, and the error accumulates only *within* a batch: sample
timing is derived from that batch's own `t_base`, which the device measures, so
each notification re-anchors and nothing accumulates across a stream. At 833 Hz
in a batch of nineteen samples the worst case is about 9 µs, which is below the
10 µs resolution of a CAN frame timestamp (§6.1) and therefore cannot be the
limiting term in any cross-channel alignment.

`period` is a `u32`, so representable intervals run from 1 µs to about 4295
seconds. A device MUST NOT report a period of zero.

`flags` declares which sensor groups are populated. If a group's flag is clear,
its fields MUST be zero and the receiver MUST report them absent — not as a
measurement of zero.

Samples are in the **sensor frame**. A device MUST NOT attempt to rotate them
into a vehicle frame; it cannot know its mounting. Mounting orientation is
outside the scope of major version 1: there is no extension mechanism on this
characteristic to carry it, and inventing one for a single field would be worse
than leaving the question to the client, which has to solve it for every other
sensor anyway.

---

## 8. Clock, sequence and loss

The three bookkeeping fields every stream carries. All are cross-cutting: a
client uses them the same way whichever characteristic they arrived on.

### 8.1 The clock

A device MUST maintain one monotonic microsecond clock and MUST timestamp GPS
fixes, CAN frames and IMU samples against it. This single shared time base is
what makes cross-channel alignment possible and is REQUIRED even when only one
role is implemented.

The clock MUST NOT jump backwards while connected. A device that disciplines its
clock to GNSS MUST set `clock_flags` bit 0 and MUST apply corrections as a
frequency adjustment, not as a step.

`t_utc` in a GPS fix and `TIME_SYNC` (§9) are the two ways a client maps this
clock to wall time.

Timestamps derived from the clock — `t_base + dt × 10` for a CAN frame (§6.1)
and `t_base + i × period` for an IMU sample (§7) — are computed modulo 2^64,
the width of the field they derive from. A device will not reach that wrap: at
one microsecond per tick it is over half a million years away. The arithmetic is
specified so that two conforming implementations agree bit for bit rather than
by accident, which they otherwise do not — a decoder in a language with
arbitrary-precision integers produces a different answer from one in C for the
same bytes.

### 8.2 Sequence

`seq` counts **notifications sent on its own characteristic**. It increments by
exactly one per notification, on all three streams, and wraps at 65535.

A gap therefore means one thing and one thing only: notifications the device
sent that the client did not receive. A receiver MUST NOT treat a wrap from
65535 to 0 as a gap.

`seq` restarts at 0 on the first notification sent on that characteristic after
a connection is established. A client consequently never has to distinguish a
reconnection from a wrap, and the protocol needs no session or boot identifier
to make that distinction for it.

Counting notifications rather than source items is what makes the field uniform.
A CAN batch header cannot count frames the device never accepted, and an IMU
header cannot count samples without jumping by `count` each time; only the
notification is a thing all three streams have exactly one of.

### 8.3 Loss

`dropped` counts items the device **accepted and then discarded**, since the
previous notification on that characteristic.

Accepted is the load-bearing word. A CAN frame that matched no subscription was
never accepted, and a device MUST NOT count it. `dropped` is a report of
capacity that was exceeded, not of filtering that worked as instructed, and
conflating the two would make the field useless for the only thing it is for.

`dropped` **saturates** at 65535. It MUST NOT wrap.

This is the one counter in VTP/1 that saturates, and the reason is §1.1. A
wrapping drop counter reads 0 after exactly 65536 discards — reporting perfect
health at the precise moment the device is losing data fastest. That is a
plausible wrong value, and the specification spends the ceiling rather than
allow one. A receiver MUST read 65535 as "at least 65535", never as exactly
that many.

Together the two fields separate the two ways data goes missing, and neither can
mask the other: `seq` gaps are the transport losing what the device sent, and
`dropped` is the device losing what the source produced.

---

## 9. Control characteristic — WRITE, response by INDICATE

Requests are `[opcode:u8][tag:u8][params…]`. Responses are
`[opcode:u8][tag:u8][status:u8][detail…]`.

`tag` is chosen by the client and MUST be echoed in the response so that
requests and responses can be correlated. A device MUST respond to every
request.

<!-- BEGIN GENERATED: control -->
| Opcode | Command | Params | Response detail | Notes |
| --- | --- | --- | --- | --- |
| `0x01` | `CAN_RESET` | — | — | Clear all subscriptions and stop the CAN stream |
| `0x02` | `CAN_SUBSCRIBE` | `id:u32, mode:u8, arg:u16` | `handle:u16` | Equivalent to CAN_SUBSCRIBE_MASK with mask 0x1FFFFFFF |
| `0x03` | `CAN_SUBSCRIBE_MASK` | `id:u32, mask:u32, mode:u8, arg:u16` | `handle:u16` | — |
| `0x04` | `CAN_UNSUBSCRIBE` | `handle:u16` | — | Removes one subscription by the handle its install returned |
| `0x05` | `CAN_LIST` | `start:u16` | `can_list_page record` | One page of the table, starting at index `start` |
| `0x10` | `GPS_SET_RATE` | `hz:u16` | — | — |
| `0x20` | `IMU_SET_RATE` | `hz:u16` | — | — |
| `0x30` | `TIME_SYNC` | `host_t_utc_ms:i64` | `t_device:u64` | The device clock at the instant the write was received |
| `0x31` | `GET_LINK_PARAMS` | — | `link_params record` | — |
<!-- END GENERATED: control -->

`status` values:

<!-- BEGIN GENERATED: enum:status -->
| Value | Name | Meaning |
| --- | --- | --- |
| 0 | `ok` | Request accepted |
| 1 | `unsupported_opcode` | Opcode not implemented |
| 2 | `bad_params` | Parameters malformed or out of range |
| 3 | `table_full` | No free subscription slot |
| 4 | `rate_exceeded` | Would exceed can_max_frames_per_s |
| 5 | `busy` | Retry later |
| 6 | `needs_encryption` | Link is not encrypted |
| 7 | `unknown_handle` | No subscription with that handle |
| *other* | *unknown* | MUST decode as unknown, never as a default |
<!-- END GENERATED: enum:status -->

Subscription modes:

<!-- BEGIN GENERATED: enum:sub_mode -->
| Value | Name | Meaning |
| --- | --- | --- |
| 0 | `every_frame` | Forward every frame |
| 1 | `periodic` | arg = minimum interval, ms |
| 2 | `on_change` | arg = debounce interval, ms |
| 3 | `every_nth` | arg = N |
| *other* | *unknown* | MUST decode as unknown, never as a default |
<!-- END GENERATED: enum:sub_mode -->

A device MUST reject a subscription that would exceed `can_subscription_slots`
with `table_full`, rather than accepting it and silently discarding frames.

A client MAY have several requests outstanding. A device MUST process them in
the order received and MUST respond to each. `tag` is opaque to the device and
MUST be echoed unchanged.

### 9.2 CAN subscriptions

Installing a subscription returns a **handle**. The handle identifies that
subscription for as long as it is installed; it is assigned by the device,
opaque to the client, and MUST NOT be reused while the subscription it names
exists. It MAY be reused once that subscription has been removed.

`CAN_SUBSCRIBE` is exactly `CAN_SUBSCRIBE_MASK` with a mask of `0x1FFFFFFF`. A
set bit in `mask` is a bit of `id` that a frame must match; a clear bit is a
bit that may hold anything.

Installing a subscription whose `id` and `mask` equal one already installed
MUST update that subscription's `mode` and `arg` in place and return its
existing handle. It MUST NOT consume a second slot. A client that reprograms
unconditionally on every connection therefore cannot exhaust the table, which
is the strategy §4 already forces on it.

`CAN_UNSUBSCRIBE` takes a handle, not an identifier. An identifier is not a
unique name for a subscription once masks exist, and a client that cannot say
precisely which entry it means cannot verify what it removed. A handle that
names no installed subscription MUST be answered `unknown_handle`.

Subscriptions do **not** survive disconnection. A device MUST clear its
subscription table when the link drops, so that a client always finds a known
state and never inherits one it did not install.

### 9.3 Overlapping subscriptions

A frame matching several subscriptions MUST be forwarded **at most once**, and
the subscription that governs it is chosen as follows:

1. The match whose `mask` has the most bits set — the most specific.
2. Among equally specific matches, the lowest handle.

Both terms are visible to the client through `CAN_LIST`, so which subscription
governs a frame is something a client can determine rather than discover. A
device MUST NOT forward one frame once per matching subscription: duplicate
frames on one bus-arrival timestamp are indistinguishable from a bus fault.

### 9.4 Rate admission

`rate_exceeded` is only decidable where the subscription itself bounds the
rate. For `periodic` and `every_nth` a device MUST refuse at admission if the
subscription would take the installed total beyond `can_max_frames_per_s`.

For `every_frame` and `on_change` it is **not** decidable: the device cannot
know what the bus will carry. A device MUST NOT refuse such a subscription on
rate grounds. It admits, and if the resulting load exceeds what it can forward
it sheds — reporting the loss in `dropped` and setting `flags` bit 0 (§6.3).
A prediction the device cannot make is not a promise the protocol should ask
for.

### 9.5 The subscription table

`CAN_LIST` returns one page of the installed table, beginning at index `start`:

<!-- BEGIN GENERATED: can_list_page -->
*One page of the CAN subscription table. Followed by `count` can_subscription entries.*

Total: **6 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 2 | `u16` | `total` | Subscriptions installed, across all pages |
| 2 | 2 | `u16` | `index` | Table index of the first entry in this page |
| 4 | 1 | `u8` | `count` | Entries in this page |
| 5 | 1 | `u8` | `reserved` | **reserved — MUST be zero** |
<!-- END GENERATED: can_list_page -->

followed by `count` entries:

<!-- BEGIN GENERATED: can_subscription -->
*One installed CAN subscription, as the device holds it.*

Total: **13 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 2 | `u16` | `handle` | Identifies this subscription; assigned by the device |
| 2 | 4 | `u32` | `id` | bits 0-28 arbitration id; b29 extended |
| 6 | 4 | `u32` | `mask` | A set bit is a bit of `id` that must match |
| 10 | 1 | `u8` | `mode` | enum `sub_mode` |
| 11 | 2 | `u16` | `arg` | Interpretation depends on `mode` |
<!-- END GENERATED: can_subscription -->

The table is paged because it does not fit. At the minimum ATT MTU of 100 a
response carries 97 bytes, of which three are the opcode, tag and status and
six are the page header, leaving six entries — while `can_subscription_slots`
may be far larger. A client reads from `start` 0 and repeats with
`index + count` until that total reaches `total`.

`start` beyond the end of the table is not an error: the device answers `ok`
with `count` zero and the true `total`. `total` MUST be the number of
subscriptions installed at the moment the page was produced.

A device MUST report its table exactly as installed. `CAN_LIST` exists so a
client can verify device state rather than assume it, and a device that
normalises, reorders or summarises here defeats its only purpose.

### 9.1 Link parameters

The detail of a successful `GET_LINK_PARAMS` response is one `link_params`
record:

<!-- BEGIN GENERATED: link_params -->
*The device's view of the negotiated link. Reported, never negotiated here.*

Total: **16 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 2 | `u16` | `validity` | bitmask `link_validity` |
| 2 | 2 | `u16` | `att_mtu` | `bytes`; valid when `validity` bit 0 (`att_mtu`) is set |
| 4 | 2 | `u16` | `ll_max_tx_octets` | `octets`; valid when `validity` bit 1 (`ll_data_length`) is set |
| 6 | 2 | `u16` | `ll_max_rx_octets` | `octets`; valid when `validity` bit 1 (`ll_data_length`) is set |
| 8 | 2 | `u16` | `conn_interval` | `1.25ms`; valid when `validity` bit 2 (`conn_params`) is set |
| 10 | 2 | `u16` | `peripheral_latency` | valid when `validity` bit 2 (`conn_params`) is set; Connection events the device may skip |
| 12 | 2 | `u16` | `supervision_timeout` | `10ms`; valid when `validity` bit 2 (`conn_params`) is set |
| 14 | 1 | `u8` | `phy_tx` | enum `phy`; valid when `validity` bit 3 (`phy`) is set |
| 15 | 1 | `u8` | `phy_rx` | enum `phy`; valid when `validity` bit 3 (`phy`) is set |
<!-- END GENERATED: link_params -->

<!-- BEGIN GENERATED: bitmask:link_validity -->
| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `att_mtu` | att_mtu carries the negotiated value |
| 1 | `ll_data_length` | ll_max_tx_octets and ll_max_rx_octets are valid |
| 2 | `conn_params` | conn_interval, peripheral_latency and supervision_timeout are all valid |
| 3 | `phy` | phy_tx and phy_rx are valid |
| 4+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:link_validity -->

`phy_tx` and `phy_rx`:

<!-- BEGIN GENERATED: enum:phy -->
| Value | Name | Meaning |
| --- | --- | --- |
| 1 | `le_1m` | LE 1M |
| 2 | `le_2m` | LE 2M |
| 3 | `le_coded` | LE Coded |
| *other* | *unknown* | MUST decode as unknown, never as a default |
<!-- END GENERATED: enum:phy -->

This record is **reporting only**. Nothing in it is negotiated through VTP/1,
and a device MUST NOT change its link configuration in response to this request.

Each validity bit governs the fields listed against it, under the same rule as
§5.1: if the bit is clear the device MUST write those fields as zero and the
receiver MUST report them absent. A device whose controller does not expose a
given parameter MUST clear the corresponding bit rather than report a guess.
There is no PHY value zero, so a zeroed `phy_tx` cannot be mistaken for LE 1M.

A device SHOULD implement this opcode. Its purpose is to let a client verify the
transport requirements of §2.1-§2.3, none of which a client can observe from its
own Bluetooth stack: negotiated link-layer payload, PHY and connection interval
are unavailable to applications on at least one major mobile platform. A client
that finds a device reporting a link-layer payload well below its ATT MTU SHOULD
surface that to the user as a device defect, since it costs roughly three times
the radio airtime per byte delivered and that cost is borne by every other
device sharing the central.

A client MUST NOT treat a device that answers `unsupported_opcode` as
non-conforming.

---

## 10. Security

The Control characteristic MUST require an encrypted link. A device MUST reject
writes on an unencrypted link with status `needs_encryption`.

Stream characteristics MAY be readable on an unencrypted link. A device
SHOULD require encryption for them and MUST do so if it is fitted to a vehicle
bus carrying anything beyond powertrain telemetry.

LE Secure Connections is REQUIRED. Just Works pairing is acceptable.

---

## 11. Versioning and compatibility

### 11.1 Major versions

A major version has its own service UUID (§3). A client scans for the majors it
implements; an unimplemented major is a discovery outcome, never a parsing one.

A device MAY expose several major versions simultaneously as separate services.

### 11.2 Minor versions

A client conforming to minor *N* MUST correctly parse minor *N + k* for all *k*.
Three rules make this structural rather than aspirational:

1. A record's size MUST NOT change within a major version.
2. New fields MUST be added as extension records, never appended to a base
   record.
3. Reserved bits and reserved bytes MAY be assigned in a minor version. They
   read as zero from older firmware and are ignored by older clients.

### 11.3 Prohibited changes

Within major version 1, an implementation MUST NOT:

- Change the meaning, units or scale of an existing field.
- Change the size, offset or type of an existing field.
- Remove or repurpose a field. Deprecation is expressed by ceasing to set the
  field's validity bit.
- Change the value or meaning of an existing enum member.
- Change any UUID.

New enum members MAY be added. A receiver encountering an unknown enum value
MUST report it as unknown and MUST NOT substitute a default.

### 11.4 No negotiation

The device declares one version; the client adapts. There is no version
negotiation exchange, and a client MUST NOT expect one.

---

## 12. Conformance vectors

`conformance/vectors/` contains byte vectors with their expected decodes. Every
implementation MUST pass those for the roles it declares.

Each case carries `hex` and either `expect` or `must_reject`. A case with
`must_reject` MUST fail to decode; a runner that decodes it has not passed.

The corpus is generated from `schema/vtp1.yaml`. A minor version MAY add cases
and MUST NOT modify or remove an existing case. A change that alters the expected
decode of an existing vector is by definition not a minor version.

### 12.1 What the corpus does not cover

The corpus decodes bytes. Every requirement in this specification that is
expressed as a byte layout, a validity rule, an enum value or a length check is
therefore mechanically testable, and passing the corpus is evidence about all of
them.

The transport requirements of §2.1-§2.3 are not of that kind. Link-layer
payload, PHY and connection parameters have no representation in any
notification, so no vector can assert them and passing the corpus says nothing
about whether a device honours them. They are **integration requirements**: real
and normative, but verifiable only against hardware.

`GET_LINK_PARAMS` (§9.1) exists to narrow that gap. It moves a device's own view
of those three parameters onto the wire, where a client can check it and this
corpus can test the reporting. It remains a report rather than a proof — a
device that misreports cannot be caught by any means this specification
provides — but a value a client can read and log is a considerable improvement
on a requirement nobody can observe.

An implementer SHOULD treat §2.1-§2.3 as the part of this specification that
needs verifying on a bench rather than in CI.

---

## Appendix A — Reserved space

| Location | Reserved | Purpose |
| --- | --- | --- |
| `gps_fix.validity` | bits 12–31 | Validity for fields added in a later minor |
| `gps_fix.fix_flags` | bits 4–7 | Additional solution-quality flags |
| `info.capabilities` | bits 8–31 | Roles and features added in a later minor |
| `can_header.reserved` | 2 bytes | Low byte earmarked for a bus index (§6.9); high byte unassigned |
| `can_list_page.reserved` | 1 byte | Paging metadata |
| `imu_header.reserved` | 2 bytes | In-band IMU metadata |
| `imu_header.flags` | bits 2–7 | Additional sensor groups |
| Extension types | `0x00`–`0xFF` | `0x80`–`0xFF` are reserved for vendor-private use and MUST NOT be assigned by this specification |
