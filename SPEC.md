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

This document states the rules and stops. The reasoning behind them —
trade-offs, history, and the failures each rule exists to prevent — lives in
[RATIONALE.md](RATIONALE.md), whose §8 is organised to mirror the sections
below. [Appendix B](#appendix-b--a-minimal-device) sketches the smallest
conforming device.

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

Malformed means the receiver cannot know where the payload's fields are: a
wrong length, a count the buffer does not hold, a record boundary the reader
and writer disagree about. A payload whose layout is sound but whose *content*
breaks a device-side rule — an out-of-range coordinate (§5), contradictory
flags (§5.3), an Info that breaks the capability matrix (§4.1) — is decoded,
and the violation is surfaced rather than repaired. The device MUST NOT emit
it; the receiver MUST NOT paper over it.

### 1.2 Roles and terms

| Term | Meaning |
| --- | --- |
| **Device** | The logger, and the GATT peripheral. It holds the GNSS receiver, the CAN transceiver and the IMU, and it produces the streams. |
| **Client** | The host application — a phone or laptop app — and the GATT central. It connects, subscribes, decodes and records. |
| **Receiver** | Whichever end is decoding the payload under discussion: the client on the GPS, CAN and IMU streams, the device on the Monitor characteristic (§13), where the direction reverses. A requirement addressed to a receiver binds whoever is decoding. |
| **Notification** | An unacknowledged GATT push from device to client on one characteristic. The three streams are notifications, and one notification is one complete payload — never a fragment continued in the next. |
| **Indication** | An acknowledged GATT push. Control responses are indications (§9). |
| **Record** | One fixed-size little-endian struct, as tabulated in this document. |
| **Batch** | A header record followed by `count` further records, all inside one notification. |
| **Fix, frame, sample** | One GNSS position solution, one CAN bus frame, one IMU sample: the item its stream carries and the unit `dropped` counts. |

The characteristic named `gps` carries a solution from whatever constellations
the receiver uses. The name is not a claim about GPS in particular.

Two words describing counters are used precisely throughout. A counter that
**wraps** resumes at zero after its maximum, so a step backwards is ordinary
and means nothing was lost. A counter that **saturates** stops at its maximum
and stays there. `seq` wraps and `dropped` saturates (§8).

### 1.3 The shape of the streams

Each characteristic carries one shape of payload, and that shape does not vary
between notifications:

| Characteristic | Direction | One payload is | Detail |
| --- | --- | --- | --- |
| `info` | device → client, on read | One 24-byte record | §4 |
| `gps` | device → client, notify | One 74-byte fix, plus any extension records | §5 |
| `can` | device → client, notify | A 16-byte batch header, then `count` variable-length frame records | §6 |
| `imu` | device → client, notify | A 20-byte batch header, then `count` fixed-size samples | §7 |
| `control` | client → device, write; answered by indication | One request, or its response | §9 |
| `monitor_values` | client → device, write | A 4-byte header, then `count` values | §13 |

A fix is never batched; a CAN frame never travels without a batch header, even
alone. The asymmetry follows the data rates (RATIONALE §2.4). The IMU is
batched like CAN but timestamped differently: its samples are evenly spaced,
so one interval in the header describes all of them (§7).

Three properties are common to all three streams and are specified once, in §8.
Every payload begins with `seq` and `dropped`. Every timestamp in every stream
is a reading of one monotonic device clock, so aligning a CAN frame against a
GPS fix is subtraction. Nothing in this protocol is timestamped by the client,
and no stream carries a clock of its own.

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
negotiated maximum when batching (§6, §7). A notification never exceeds the
negotiated ATT payload, which the client's own stack already knows; a client
sizes its receive buffer from that and from nothing in this protocol.

Signed integers are two's complement. Reserved fields MUST be written as zero
and MUST be ignored on receive.

The Device Information Service is a SHOULD, specified in §3.4. The three
subsections below cannot be tested by the conformance corpus, because none of
them appears in any payload; §12.1 says what follows from that.

### 2.1 Link-layer payload

A device MUST negotiate the largest link-layer payload its controller supports,
up to `max_tx_octets` and `max_rx_octets` of 251. A large ATT MTU over the
27-octet default link-layer payload costs roughly three times the radio
airtime per byte delivered, at every other radio user's expense
(RATIONALE §8.1).

### 2.2 PHY

A device SHOULD request the LE 2M PHY and MUST function correctly on the LE 1M
PHY. The 2M PHY halves the airtime of a given payload; nothing else in this
specification changes with it.

### 2.3 Connection parameters

The connection interval and peripheral latency are granted by the central, not
chosen by the device. A device MUST function correctly at whatever values the
central applies, including values it did not request and values that change
during a connection, and MUST NOT assume it received the interval it requested.
Batch flush timing (§6.1, §7) MUST therefore follow the device's own clock
rather than an assumed interval, and MUST respect the `dt` bound of §6.1 in
every case.

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

### 3.4 Device Information

A device SHOULD expose the standard Bluetooth Device Information Service
(`0x180A`) with at least a manufacturer name, model number and firmware
revision. Nothing in VTP/1 reads it: it is what answers "which firmware is on
this logger" to generic Bluetooth tools, and Info (§4) remains the only thing
a client parses (RATIONALE §8.2).

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
| 20 | 1 | `u8` | `reserved_20` | Was can_max_payload; derived from the capability bits since (SPEC.md 4.1); **reserved — MUST be zero** |
| 21 | 1 | `u8` | `clock_flags` | bitmask `clock_flags` |
| 22 | 2 | `u16` | `reserved_22` | Was max_notify_bytes; a notification is bounded by the negotiated ATT payload, which the client's stack already knows (SPEC.md 2); **reserved — MUST be zero** |
<!-- END GENERATED: info -->

`clock_flags` bits:

<!-- BEGIN GENERATED: bitmask:clock_flags -->
| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `gnss_disciplined` | The device clock is disciplined by GNSS |
| 1 | `survives_reconnect` | The device clock does not restart when the link drops |
| 2+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:clock_flags -->

`capabilities` bits:

<!-- BEGIN GENERATED: bitmask:capabilities -->
| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `gps` | — |
| 1 | `can` | **Requires `control`.** |
| 2 | `imu` | — |
| 3 | `monitor` | Device asks the client for values to display (§13) **Requires `control`.** |
| 4 | `control` | — |
| 5 | `can_fd` | **Requires `can`.** |
| 6 | `masked_subscriptions` | **Requires `can`.** |
| 7+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:capabilities -->

A client MUST read this characteristic on every connection and MUST NOT cache
it across connections: a DIY device is reflashed by its owner, so its minor
version, capability set and rate ceilings can all change while its Bluetooth
address does not.

If `protocol_major` does not match the major version implied by the discovered
service UUID, the client MUST treat the device as non-conforming and disconnect.

A capacity field of zero means "none", not "unspecified". A client MUST NOT
substitute a default for any capacity it did not read.

### 4.1 The profile matrix

Everything a capability bit changes is here, and nowhere else. Both tables are
generated from `schema/vtp1.yaml`, so an implementation, the conformance
runner and this section cannot disagree about what a bit requires.

**The attribute table is fixed.** A VTP/1 device MUST expose the service and
**every** characteristic in the first table, whatever its capabilities say. A
characteristic whose capability bit is clear is **inert**, not absent, and the
last column says exactly what inert means for it. The table is fixed because
central stacks cache the attribute table across connections; a table that
changes between connections hands the client a stale handle (RATIONALE §8.2).

<!-- BEGIN GENERATED: profile:attributes -->
| Characteristic | Capability | Properties | CCCD | Written by | Read by | When the capability bit is clear |
| --- | --- | --- | --- | --- | --- | --- |
| `info` | — always present | `read` | — | device | client | never; Info is always meaningful |
| `gps` | bit 0 (`gps`) | `notify` | always present; client enables it for a set bit | device | client | the CCCD exists; no notification is ever sent on it |
| `can` | bit 1 (`can`) | `notify` | always present; client enables it for a set bit | device | client | the CCCD exists; no notification is ever sent on it |
| `imu` | bit 2 (`imu`) | `notify` | always present; client enables it for a set bit | device | client | the CCCD exists; no notification is ever sent on it |
| `control` | bit 4 (`control`) | `write`, `indicate` (write with-response) | always present; client enables it for a set bit | client | client | the CCCD exists; writes are rejected with an ATT error and no opcode is parsed |
| `monitor_values` | bit 3 (`monitor`) | `write` (write with-response) | — | client | device | writes are rejected with an ATT error and change nothing |
<!-- END GENERATED: profile:attributes -->

A device MUST NOT add a characteristic to the VTP/1 service beyond these, and
MUST expose at least the properties listed. It MAY expose more — making `gps`
readable is a common convenience — and a client MUST NOT rely on any property
the table does not list, so it MUST NOT read `gps` in place of subscribing.

An inert characteristic costs its implementer almost nothing. A device without
the `control` bit exposes the Control characteristic and rejects every write
with an ATT error; it does not parse opcodes, implement indications, or answer
`unsupported_opcode`. The same goes for `monitor_values`.

Every notifying and indicating characteristic carries its Client
Characteristic Configuration descriptor whatever the capability bit says. A
device MUST accept a CCCD write on an inert stream — and then simply never
notifies — and MUST NOT reject one on the grounds that the capability is
absent. A client enables the CCCD for a role whose bit is set, and leaves the
others alone.

**Capability implications are normative.**

<!-- BEGIN GENERATED: profile:capabilities -->
| Bit | Capability | Requires | Capacity fields that MUST be zero when clear |
| --- | --- | --- | --- |
| 0 | `gps` | — | `gps_rate_hz`, `gps_max_rate_hz` |
| 1 | `can` | bit 4 (`control`) | `can_subscription_slots`, `can_max_frames_per_s` |
| 2 | `imu` | — | `imu_rate_hz`, `imu_max_rate_hz` |
| 3 | `monitor` | bit 4 (`control`) | — |
| 4 | `control` | — | — |
| 5 | `can_fd` | bit 1 (`can`) | — |
| 6 | `masked_subscriptions` | bit 1 (`can`) | — |
<!-- END GENERATED: profile:capabilities -->

**The largest CAN payload follows from the bits and is not a field.** A client
computes it:

| `can` | `can_fd` | Largest payload |
| --- | --- | --- |
| clear | clear | 0 — the device has no CAN |
| set | clear | 8 — Classic CAN |
| set | set | 64 — CAN FD |

`set`/`set` is the only combination `can_fd` permits, because `can_fd`
requires `can`. Byte 20 of Info once carried this value and is now reserved:
a field whose every value is derivable is a field two implementations can
disagree about.

A device MUST NOT set a capability bit without also setting every bit the
second column names, and MUST NOT publish a non-zero value in a capacity field
whose capability bit is clear. This is the device-side half of "a capacity of
zero means none": a device reporting `can_max_frames_per_s` of 4000 with the
`can` bit clear has published a capability it does not have.

A client MUST decode an Info that breaks either rule — the record is
well-formed — but MUST NOT use a role whose required bit is missing, MUST NOT
rely on a capacity published behind a cleared bit, and SHOULD surface the
contradiction to the user as a device defect. It MUST NOT guess which half was
meant.

`can` and `monitor` require `control` because neither role is reachable
without it: a CAN device forwards nothing until a client has sent
`CAN_SUBSCRIBE` (§9.1), and a Monitor device can only name its channels
through `MONITOR_LIST` (§13.3). `can_fd` and `masked_subscriptions` require
`can` because each qualifies how CAN subscriptions behave.

**Each qualifier bit says what a device does when it is clear:**

| Bit | Set | Clear |
| --- | --- | --- |
| `can_fd` | The device MAY emit records with the FD bit set, carrying up to 64 payload bytes | The device MUST NOT emit a record with the FD bit set, and no record carries more than 8 payload bytes |
| `masked_subscriptions` | `CAN_SUBSCRIBE_MASK` is accepted | `CAN_SUBSCRIBE_MASK` MUST answer `unsupported_opcode` |

`CAN_SUBSCRIBE` is unaffected by `masked_subscriptions`: §9.1 defines it as
`CAN_SUBSCRIBE_MASK` with a full mask, but it is a separate opcode and every
CAN device implements it. The capability governs whether a client may choose
the mask, not whether masking exists.

The "written by" and "read by" columns say which end produces each record. A
conformance role covers both directions of the records it names
(`conformance/README.md`).

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
| 8 | 8 | `u64` | `t_device` | `µs`; Monotonic device clock (§8.1) |
| 16 | 8 | `i64` | `t_utc` | `ms`; Unix epoch; valid when `validity` bit 0 (`t_utc`) is set |
| 24 | 4 | `i32` | `lat` | `deg`; scale 1e-07; Latitude — positive north, negative south; valid when `validity` bit 2 (`position`) is set |
| 28 | 4 | `i32` | `lon` | `deg`; scale 1e-07; Longitude — positive east, negative west; valid when `validity` bit 2 (`position`) is set |
| 32 | 4 | `i32` | `alt_msl` | `mm`; Altitude above mean sea level; valid when `validity` bit 3 (`alt_msl`) is set |
| 36 | 4 | `i32` | `alt_ellipsoid` | `mm`; Altitude above the WGS-84 ellipsoid; valid when `validity` bit 4 (`alt_ellipsoid`) is set |
| 40 | 4 | `i32` | `vel_n` | `mm/s`; Velocity, north component; valid when `validity` bit 5 (`velocity`) is set |
| 44 | 4 | `i32` | `vel_e` | `mm/s`; Velocity, east component; valid when `validity` bit 5 (`velocity`) is set |
| 48 | 4 | `i32` | `vel_d` | `mm/s`; Velocity, down component — positive descending; valid when `validity` bit 5 (`velocity`) is set |
| 52 | 4 | `i32` | `head_mot` | `deg`; scale 1e-05; Heading of motion, clockwise from true north; valid when `validity` bit 6 (`head_mot`) is set |
| 56 | 4 | `u32` | `h_acc` | `mm`; Horizontal position accuracy estimate, 1 sigma; valid when `validity` bit 7 (`h_acc`) is set |
| 60 | 4 | `u32` | `v_acc` | `mm`; Vertical position accuracy estimate, 1 sigma; valid when `validity` bit 8 (`v_acc`) is set |
| 64 | 4 | `u32` | `s_acc` | `mm/s`; Speed accuracy estimate, 1 sigma; valid when `validity` bit 9 (`s_acc`) is set |
| 68 | 2 | `u16` | `p_dop` | scale 0.01; Position dilution of precision; valid when `validity` bit 10 (`p_dop`) is set |
| 70 | 1 | `u8` | `fix_type` | enum `fix_type` |
| 71 | 1 | `u8` | `num_sv` | Satellites used in the solution; valid when `validity` bit 11 (`num_sv`) is set |
| 72 | 1 | `u8` | `fix_flags` | bitmask `fix_flags` |
| 73 | 1 | `u8` | `ext_count` | Extension records following the base record |
<!-- END GENERATED: gps_fix -->

**Reading a scaled field.** A scaled field holds a plain integer; the quantity
is `raw × scale` in the units given. Signed fields are two's complement (§2),
and the sign carries the whole of the direction — no field in this
specification is paired with a hemisphere byte, a sign flag or a direction
letter.

| Field | On the wire | Bytes | Decodes to |
| --- | --- | --- | --- |
| `lat` | `515074000` | `d0 67 b3 1e` | 51.5074° N |
| `lat` | `-337868000` | `20 8b dc eb` | 33.7868° S |
| `lon` | `-1223948000` | `20 09 0c b7` | 122.3948° W |
| `alt_msl` | `12500` | `d4 30 00 00` | 12.5 m above mean sea level |
| `vel_n` | `-4200` | `98 ef ff ff` | 4.2 m/s southward |
| `head_mot` | `12345678` | `4e 61 bc 00` | 123.45678° from true north |
| `p_dop` | `145` | `91 00` | PDOP 1.45 |

A receiver that reads the two negative rows as unsigned gets 395.7° and
307.1° — numbers no coordinate can hold, which is why hemisphere is a sign
rather than a flag somebody has to remember to apply.

**Ranges.** When its validity bit is set, `lat` MUST lie within ±90°, `lon`
within ±180°, and `head_mot` within 0° to 360° exclusive of 360. These bounds
bind the device: it MUST NOT emit a fix that breaks them. A receiver MUST
decode such a fix — the payload is well-formed — but MUST NOT clamp the value,
SHOULD NOT use the affected fields, and SHOULD surface the violation to the
user as a device defect. Clamping is forbidden under §1.1: 91° is not a place
a clamp could move closer to, and clamping to 90° puts the vehicle at the pole
and lets the client draw it there.

**Datum.** `lat`, `lon` and `alt_ellipsoid` MUST be referenced to WGS-84.
`alt_msl` is height above mean sea level as the receiver computes it, from
whatever geoid model it carries; this specification does not name one.

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
| 4 | `solution_epoch` | t_device is the epoch of the solution; when clear it is when the fix reached the device (SPEC.md 5.6) |
| 5+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:fix_flags -->

`rtk_float` and `rtk_fixed` are **mutually exclusive** — a carrier-phase
solution has either resolved its integer ambiguities or it has not — and
either implies `differential`, since an RTK solution is by definition a
differentially corrected one.

A device MUST NOT set both RTK bits, and MUST set `differential` whenever it
sets either. A receiver MUST decode a fix that breaks either rule and SHOULD
surface the contradiction; it MUST NOT resolve the pair by guessing — in
particular it MUST NOT read both-set as `rtk_fixed`, which upgrades a device's
accuracy claim on the strength of a bug.

### 5.4 Reference frames and derived quantities

The velocity triple is a local north-east-down frame at the reported position:
`vel_n` toward true north, `vel_e` toward true east, and `vel_d` positive
downward, so a climbing vehicle reports a negative `vel_d`.

Ground speed is `hypot(vel_n, vel_e)` and is exact. A device MUST NOT report a
separately computed scalar ground speed; the velocity vector is the only
representation.

`head_mot` is measured clockwise from **true** north — never magnetic north,
and never a grid bearing. VTP/1 carries no magnetic declination and no magnetic
heading. `head_mot` is the receiver's filtered heading of motion and MAY differ
from `atan2(vel_e, vel_n)`; a client SHOULD prefer `head_mot` when its validity
bit is set.

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

### 5.6 When a fix is timestamped

**`fix_flags` bit 4 (`solution_epoch`) says which instant `t_device` names**:

| Bit 4 | `t_device` is |
| --- | --- |
| set | the **epoch of the solution** — the instant the reported position was true |
| clear | the instant the fix **reached the device** |

A device MUST set the bit when it can determine the solution epoch, and MUST
clear it otherwise. `t_utc`, when its validity bit is set, always refers to the
epoch of the solution whichever way bit 4 reads — it comes from the receiver's
own solution and is not a reading of the device clock at all.

The two instants are tens to hundreds of milliseconds apart, so a device that
stamps delivery reports every GPS sample late against CAN and IMU by a latency
that varies with the receiver's load (RATIONALE §8.3). A client aligning GPS
against CAN below the tens of milliseconds SHOULD check this bit, and SHOULD
NOT assume the offset is constant when it is clear.

---

## 6. CAN characteristic — NOTIFY

A notification is one batch header followed by `count` frame records.

<!-- BEGIN GENERATED: can_header -->
*Batch header. Followed by `count` can_record entries.*

Total: **16 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 2 | `u16` | `seq` | Notifications sent on this characteristic; +1 each, wraps, restarts at 0 per connection |
| 2 | 2 | `u16` | `dropped` | Frames accepted then discarded since the previous notification; excludes frames no subscription matched or a subscription mode did not select; saturates |
| 4 | 8 | `u64` | `t_base` | `µs`; Bus-arrival time of record 0, on the device clock (§8.1) |
| 12 | 1 | `u8` | `count` | — |
| 13 | 1 | `u8` | `flags` | bitmask `can_flags` |
| 14 | 2 | `u16` | `reserved` | Low byte earmarked for a bus index (SPEC.md 6.9); high byte unassigned; **reserved — MUST be zero** |
<!-- END GENERATED: can_header -->

`flags` bits:

<!-- BEGIN GENERATED: bitmask:can_flags -->
| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `shedding` | Device is discarding accepted frames (SPEC.md 6.3) |
| 1+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:can_flags -->

<!-- BEGIN GENERATED: can_record -->
*One CAN frame with a device-measured bus-arrival time.*

Total: **7 bytes + `payload`**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 2 | `u16` | `dt` | `10µs`; Ticks since this batch's own t_base; window is 0..655.35 ms |
| 2 | 4 | `u32` | `id` | bits 0-28 arbitration id; b29 extended; b30 CAN FD; b31 RTR |
| 6 | 1 | `u8` | `len` | Payload length; Classic 0..8, CAN FD a DLC rung (SPEC.md 6.10) |
<!-- END GENERATED: can_record -->

Records are variable length — seven bytes plus `len` payload bytes — so a
receiver walks them in order, and the *n*th record does not sit at a fixed
offset. One notification carrying three classic frames of eight bytes each is
16 + 3 × 15 = 61 bytes:

| Bytes | Contents |
| --- | --- |
| 0–15 | `can_header`: `seq`, `dropped`, `t_base`, `count` = 3, `flags`, `reserved` |
| 16–22 | Record 0: `dt`, `id`, `len` = 8 |
| 23–30 | Record 0's payload |
| 31–37 | Record 1: `dt`, `id`, `len` = 8 |
| 38–45 | Record 1's payload |
| 46–52 | Record 2: `dt`, `id`, `len` = 8 |
| 53–60 | Record 2's payload |

The frames in one batch need not share an identifier, a payload length or a
subscription: a batch is a unit of transmission and carries no other meaning.
How many frames go in one is bounded by the negotiated ATT MTU (§2) and by the
`dt` window (§6.1), and by nothing else.

### 6.1 Timestamps

`dt` counts 10 µs ticks from `t_base`, so a frame's bus-arrival time is
`t_base + dt × 10` microseconds on the device clock (§8).

`t_base` MUST be the bus-arrival time of record 0, measured by the device, not
the time the notification was queued or sent. It is an absolute reading of the
device clock, not an offset from anything, and every `dt` is measured from the
`t_base` in the same notification — never from the record before it or the
previous batch. **Record 0's `dt` MUST be zero**, `t_base` being its arrival
time by definition, and a receiver MUST reject a batch whose first record says
otherwise.

A batch whose `t_base` is 12 345 678 000 and whose `count` is 3:

| Record | `dt` | Bus-arrival time on the device clock |
| --- | --- | --- |
| 0 | 0 | 12 345 678 000 µs — `t_base` itself |
| 1 | 250 | 12 345 680 500 µs — 2.5 ms after record 0 |
| 2 | 1000 | 12 345 688 000 µs — 10 ms after record 0, not 10 ms after record 1 |

`dt` spans 0 … 655 350 µs. A device MUST emit a batch before `dt` would exceed
that range, which bounds worst-case batch latency at 655.35 ms. A device SHOULD
flush far more frequently — once per connection interval is RECOMMENDED.

### 6.2 Batches

**`count` MUST NOT be zero**, and a receiver MUST reject a batch that carries
no records: `t_base` is defined as the bus-arrival time of record 0, so a batch
with no record 0 timestamps a frame that does not exist. A quiet bus is
reported by sending nothing — there is no empty-batch heartbeat. `dropped` and
the shedding flag ride on the next batch that has content, which §8.3 permits.

A receiver MUST reject a notification whose length does not exactly match the
header plus `count` complete records.

### 6.3 Loss

`dropped` is defined in §8.3. A device MUST report discards there rather than
silently omitting frames, and MUST NOT count a frame that matched no
subscription, or that the governing subscription's mode did not select (§6.8) —
neither of those was ever accepted. A client SHOULD surface a non-zero
`dropped` to the user.

`flags` bit 0 indicates the device is actively **shedding load**: discarding
frames it accepted and cannot forward. The bit reports that the condition is
current; `dropped` counts what it has cost since the previous notification. A
device sheds rather than stalls because a bus does not wait; §9.3 is why the
condition is reachable at all.

### 6.4 Identifier validity

A standard frame is one whose `id` bit 29 is clear. Its arbitration identifier
is eleven bits, so bits 11–28 MUST be zero. A receiver MUST **reject the whole
batch** if a standard frame carries a larger identifier: an eleven-bit
identifier that does not fit in eleven bits is malformed, and the only
alternative is to truncate it to a different identifier that looks entirely
valid.

Bit 30 (CAN FD) and bit 31 (RTR) MUST NOT both be set: CAN FD has no remote
frames. A remote frame carries no data: if bit 31 is set, `len` MUST be zero.
A receiver MUST reject the whole batch on either violation.

Each of these is rejected rather than repaired, under §1.1: a repaired frame is
a plausible wrong value with a correct-looking identifier.

### 6.5 What a remote frame does not carry

The data length a remote frame *requests* is not represented in major version
1: `len` is the payload length, and the batch's length arithmetic depends on
it being exactly that. A logger records that a remote frame for an identifier
occurred, and not how many bytes it asked for.

### 6.6 CAN FD flags not carried

Bit Rate Switch and Error State Indicator are not represented: both are
per-frame, and a byte on `can_record` costs 4 kB/s at 4000 frames per second
on the one stream that can saturate a link (RATIONALE §4.1). Record sizes are
frozen for the life of a major version (§11.4), so adding them later is a
VTP/2 change. That cost is stated plainly rather than discovered.

### 6.7 When a timestamp is taken

`t_base` and every `dt` are bus-arrival times measured at the **end of the
frame** — the point at which the controller signals a completed reception.
Not start-of-frame, which a device cannot generally measure (RATIONALE §8.4).
A long frame is therefore stamped later than a short one relative to the
moment it began, by its own transmission time; a client aligning below a
millisecond should know which end of the frame it is aligning to.

### 6.8 Subscription modes and the first frame

The first matching frame is forwarded in every mode, so a client that installs
a subscription and waits for a value to display never waits for a second frame.

| Mode | `arg` | Behaviour after the first frame |
| --- | --- | --- |
| `every_frame` | ignored | Every matching frame. |
| `periodic` | minimum interval, ms | At most one frame per `arg` ms. `arg` 0 means no limit. |

Mode values 2 and 3 were assigned by pre-1.0 drafts and remain unassigned; a
receiver treats them as unknown (§11.4), and a device MUST answer `bad_params`
to a subscription naming one.

A masked subscription may match several identifiers, and each is a different
signal from a different sender. A device MUST therefore keep `periodic` state —
the interval, and "the first matching frame" — **per matching identifier**,
not per subscription: a shared interval lets whichever identifier arrives
first consume it, so a client sees one signal out of a group it subscribed to
as a group, and the failure looks like a quiet bus rather than a bug.

Per-identifier state is bounded by the identifiers actually seen, which a
device cannot know when the subscription is installed. A device MAY bound the
state it keeps; when the bound is reached it shed, exactly as for load (§6.3):
frames it can no longer schedule are discarded, counted in `dropped`, and the
shedding flag is set. A device MUST NOT silently forward such frames
unscheduled, and MUST NOT drop the subscription.

### 6.9 One bus

Major version 1 addresses a single CAN bus. A device with more than one
transceiver cannot say which bus a frame arrived on, and the subscription
commands have no bus parameter. The low byte of `can_header.reserved` is
earmarked for a bus index, and subscribing per bus would be a new opcode, so a
later minor version can close this gap (§11.2). Until then the byte MUST be
written as zero and MUST be ignored on receive.

### 6.10 Payload length

`len` is the number of payload bytes that follow the record, and the lengths a
CAN bus can actually carry are not contiguous.

A **Classic** frame — bit 30 clear — carries zero to eight bytes. A receiver
MUST reject the whole batch if `len` exceeds 8.

A **CAN FD** frame — bit 30 set — carries a length its four-bit DLC can express,
which above eight is a fixed ladder:

    0  1  2  3  4  5  6  7  8  12  16  20  24  32  48  64

A receiver MUST reject the whole batch if a CAN FD `len` is not one of these.

Rejection rather than repair follows §1.1: a `len` off the ladder means the
reader and the writer disagree about where this record ends, so every byte
after it is suspect — including the identifier of the next frame, which will
still look valid.

## 7. IMU characteristic — NOTIFY

<!-- BEGIN GENERATED: imu_header -->
*Batch header. Followed by `count` evenly spaced imu_sample entries.*

Total: **20 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 2 | `u16` | `seq` | Notifications sent on this characteristic; +1 each, wraps, restarts at 0 per connection |
| 2 | 2 | `u16` | `dropped` | Samples accepted then discarded since the previous notification; saturates, never wraps |
| 4 | 8 | `u64` | `t_base` | `µs`; Device-clock timestamp of sample 0 (§8.1) |
| 12 | 4 | `u32` | `period` | `µs`; Interval between consecutive samples |
| 16 | 1 | `u8` | `count` | — |
| 17 | 1 | `u8` | `flags` | bitmask `imu_flags` |
| 18 | 2 | `u16` | `reserved` | In-band IMU metadata; **reserved — MUST be zero** |
<!-- END GENERATED: imu_header -->

`flags` bits:

<!-- BEGIN GENERATED: bitmask:imu_flags -->
| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `accel` | Accelerometer triple is present |
| 1 | `gyro` | Gyroscope triple is present |
| 2 | `saturated` | A sample in this batch hit the sensor's limit (SPEC.md 7.2) |
| 3+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:imu_flags -->

<!-- BEGIN GENERATED: imu_sample -->
*Sensor-frame acceleration and rotation. Vehicle alignment is the client's job.*

Total: **12 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 2 | `i16` | `ax` | `mg`; milli-g; present when `imu_header.flags` bit 0 (`accel`) is set |
| 2 | 2 | `i16` | `ay` | `mg`; present when `imu_header.flags` bit 0 (`accel`) is set |
| 4 | 2 | `i16` | `az` | `mg`; present when `imu_header.flags` bit 0 (`accel`) is set |
| 6 | 2 | `i16` | `gx` | `deg/s`; scale 0.05; present when `imu_header.flags` bit 1 (`gyro`) is set |
| 8 | 2 | `i16` | `gy` | `deg/s`; scale 0.05; present when `imu_header.flags` bit 1 (`gyro`) is set |
| 10 | 2 | `i16` | `gz` | `deg/s`; scale 0.05; present when `imu_header.flags` bit 1 (`gyro`) is set |
<!-- END GENERATED: imu_sample -->

Samples are evenly spaced: sample *i* is at `t_base + i × period` microseconds.

**`count` MUST NOT be zero**, and a receiver MUST reject an empty batch:
`t_base` is defined as the acquisition time of sample 0, so a batch with no
sample 0 timestamps a sample that does not exist. A device with nothing to
report sends nothing. §6.2 says the same of CAN, for the same reason.

`t_base` MUST be the acquisition time of sample 0 — the instant the sensor
took that reading — and not the instant the device read it out. A device
draining a FIFO knows its own sampling schedule, so sample 0's time is the
drain time less the samples behind it; no flag or exception applies
(RATIONALE §8.5).

**Every sample in one batch MUST be evenly spaced by `period`.** If the FIFO
overflowed, the sensor was reconfigured, or a read was missed, the samples
either side of the gap are no longer `period` apart, and every derived
timestamp after it would be silently wrong. A device MUST end the batch at the
discontinuity and start the next one with a fresh `t_base` taken from the
first sample after it, counting the samples lost across the gap in `dropped`
(§8.3).

`period` is an interval rather than a rate because real sensor output data
rates are not integer hertz. A device MUST report its actual sample interval,
rounded to the nearest microsecond — the one approximate value in VTP/1. The
error re-anchors at every batch's measured `t_base`, so it accumulates only
within a batch: at 833 Hz over nineteen samples the worst case is about 9 µs,
below the 10 µs resolution of a CAN timestamp (§6.1).

A device MUST NOT report a `period` of zero, and a receiver MUST reject a
batch that does: zero says every sample was taken at the same instant, which
describes no measurement, and a client dividing by it to recover a rate
divides by zero.

`flags` declares which sensor groups are populated. If a group's flag is clear,
its fields MUST be zero and the receiver MUST report them absent — not as a
measurement of zero.

### 7.1 Axes and signs

The sensor frame is the device's own. **Vehicle alignment is the client's
job** — this specification does not say where the device is mounted or which
way it faces, because it cannot know. A device MUST NOT rotate samples into a
vehicle frame; mounting orientation is outside the scope of major version 1.

What it must say is how to read the numbers, because a client cannot infer any
of it and getting it wrong produces a plausible result rather than an obvious
one:

- The three axes form a **right-handed** frame: with *x* forward and *y* left,
  *z* is up.
- The accelerometer reports **specific force**, not acceleration. A device at
  rest reports **+1000 mg on whichever axis points up**, because the sensor
  measures the reaction that holds it against gravity. In free fall it reports
  zero on every axis.
- The gyroscope follows the **right-hand rule**: a positive `gx` is a rotation
  that carries *y* toward *z*. Viewed from the positive end of an axis looking
  back at the origin, positive is counter-clockwise.

The accelerometer convention is the one worth stating twice: both signs are in
use in the wild, and a client that assumes the wrong one sees a car braking
when it is accelerating — a mistake that survives every plausible sanity
check, because the magnitudes are right.

### 7.2 Saturation

A sample beyond the sensor's range is a measurement the device did not make:
the reading at the rail is "at least this much", not "this much".

A device MUST set `imu_header.flags` bit 2 when any sample in the batch is at
or beyond the range of the sensor that produced it. A client MUST treat every
sample in a batch so marked as a lower bound on the magnitude rather than a
measurement, and SHOULD NOT integrate one.

The flag is per batch rather than per sample because `imu_sample` is
deliberately closed (§11.3) and a batch is a short window — nineteen samples
at 833 Hz is 23 ms. Saturation is not absence: a saturated axis keeps its
presence flag set, because the sensor is fitted and did report. "Present but
railed" is a different state from "not fitted".

---

## 8. Clock, sequence and loss

The three bookkeeping fields every stream carries. All are cross-cutting: a
client uses them the same way whichever characteristic they arrived on.

### 8.1 The clock

A device MUST maintain one monotonic microsecond clock and MUST timestamp GPS
fixes, CAN frames and IMU samples against it. This single shared time base is
what makes cross-channel alignment possible and is REQUIRED even when only one
role is implemented.

The clock MUST NOT jump backwards while connected. A device that disciplines
its clock to GNSS MUST set `clock_flags` bit 0 and MUST apply corrections as a
frequency adjustment, not as a step.

`t_utc` in a GPS fix and `TIME_SYNC` (§9.5) are the two ways a client maps this
clock to wall time.

Timestamps derived from the clock — `t_base + dt × 10` for a CAN frame (§6.1)
and `t_base + i × period` for an IMU sample (§7) — are computed modulo 2^64,
the width of the field they derive from. A device will not reach that wrap; the
arithmetic is specified so that two conforming implementations agree bit for
bit rather than by accident.

### 8.2 Sequence

`seq` counts **notifications sent on its own characteristic**. It increments by
exactly one per notification, on all three streams, and wraps at 65535.

A gap therefore means one thing only: notifications the device sent that the
client did not receive. A receiver MUST NOT treat a wrap from 65535 to 0 as a
gap.

**The first notification sent on a characteristic after a connection is
established carries `seq` 0**, and the second carries 1. A client consequently
never has to distinguish a reconnection from a wrap, and the protocol needs no
session or boot identifier. The rule is stated as a property of the
notification, not of a counter, because "restarts at 0" has already been read
as putting 1 on the wire (RATIONALE §8.6).

### 8.3 Loss

`dropped` counts items the device **accepted and then discarded**, since the
previous notification on that characteristic.

Accepted is the load-bearing word. A CAN frame that matched no subscription
was never accepted, and a frame the governing subscription's mode did not
select (§6.8) was filtered as instructed; a device MUST NOT count either.
`dropped` reports capacity that was exceeded, not filtering that worked.

`dropped` **saturates** at 65535 and MUST NOT wrap: a wrapping drop counter
reads 0 after exactly 65536 discards — perfect health at the precise moment
the device is losing data fastest. A receiver MUST read 65535 as "at least
65535", never as exactly that many.

Together the two fields separate the two ways data goes missing, and neither
can mask the other: `seq` gaps are the transport losing what the device sent;
`dropped` is the device losing what the source produced.

**`dropped` is a best-effort diagnostic**, not an audit trail, and a client
MUST NOT use it to reconcile counts. A device MUST count every
accepted-then-discarded item, MUST saturate rather than wrap, and MUST NOT
report loss it did not have; it MAY report a discard in the next notification
rather than the one it strictly belonged to. `seq` is the field with the exact
guarantee, because it is cheap to be exact about: it counts notifications
actually sent, committed when the transport accepts one.

---

## 9. Control characteristic — WRITE, response by INDICATE

Requests are `[opcode:u8][tag:u8][params…]`. Responses are
`[opcode:u8][tag:u8][status:u8][detail…]`.

`tag` is chosen by the client, is opaque to the device, and MUST be echoed
unchanged in the response so that requests and responses can be correlated. A
device MUST respond to every request it applies.

**A client MUST have at most one request outstanding.** It writes a request,
waits for the indication that answers it, and only then writes the next one.
Nothing on the control plane is latency-critical: subscriptions are installed
once at connect, rates change when a user changes them, and `TIME_SYNC`
measures the round trip it is already waiting for (RATIONALE §8.7).

A device MUST answer `busy` to a request that arrives while it still owes a
response, and MUST NOT apply it. A client that receives `busy` has broken the
rule above; it MUST wait for the outstanding response and MAY then retry, and
MUST NOT treat the request as refused — `busy` says nothing about the request
itself.

A client MUST NOT reuse a `tag` while a request bearing it is outstanding. It
needs no enforcement: with one request outstanding, a second write is refused
`busy` whatever tag it carries, so a device keeps no table of tags — it echoes
the tag and forgets it. A tag becomes reusable as soon as its response has
been sent.

**`detail` is present if and only if `status` is `ok`.** A refused request is
answered with exactly three bytes, and a client MUST NOT read the detail of a
response whose status is anything else: a fixed-width response would put a
well-formed zero in front of a client that has already decided the request
succeeded.

<!-- BEGIN GENERATED: control_response -->
*The envelope of every Control response. Detail follows only when status is ok.*

Total: **3 bytes + `detail`**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 1 | `u8` | `opcode` | Echoed from the request |
| 1 | 1 | `u8` | `tag` | Echoed from the request; opaque to the device |
| 2 | 1 | `u8` | `status` | enum `status` |
<!-- END GENERATED: control_response -->

The detail's shape is decided by the opcode, and §11.3 allows a minor version
to add opcodes carrying anything at all, so a client MUST treat the detail of
an opcode it does not implement as opaque rather than malformed.

**Every opcode is owned by a capability, and availability is decided before
parameters.** The `Needs` column names it. A device MUST answer
`unsupported_opcode` to an opcode whose owning capability bit it has not set,
and MUST do so **without parsing the parameters** — so a malformed
`GPS_SET_RATE` on a device with no GPS is `unsupported_opcode`, never
`bad_params`. One refusal says "not on this device, ever"; the other says "try
again with better arguments"; a client that gets them the wrong way round
either retries forever or gives up on a device that would have worked.

The same order applies one level down: a subscription mode the device does not
support is `bad_params`, checked *after* the opcode's own capability.

`TIME_SYNC` has no owning capability. It is about the clock, which every
device has, and reaching it at all means the Control characteristic is live.

<!-- BEGIN GENERATED: control -->
| Opcode | Command | Needs | Params | Response detail | Notes |
| --- | --- | --- | --- | --- | --- |
| `0x01` | `CAN_RESET` | `can` | — | — | Clear all subscriptions and stop the CAN stream |
| `0x02` | `CAN_SUBSCRIBE` | `can` | `id:u32, mode:u8, arg:u16` | — | Equivalent to CAN_SUBSCRIBE_MASK with mask 0x3FFFFFFF |
| `0x03` | `CAN_SUBSCRIBE_MASK` | `masked_subscriptions` | `id:u32, mask:u32, mode:u8, arg:u16` | — | — |
| `0x04` | `CAN_UNSUBSCRIBE` | `can` | `id:u32, mask:u32` | — | Removes the subscription whose id and mask these are (SPEC.md 9.1) |
| `0x10` | `GPS_SET_RATE` | `gps` | `hz:u16` | — | 0 stops the stream; unsupported rates answer bad_params (SPEC.md 9.6) |
| `0x20` | `IMU_SET_RATE` | `imu` | `hz:u16` | — | 0 stops the stream; unsupported rates answer bad_params (SPEC.md 9.6) |
| `0x30` | `TIME_SYNC` | — | — | `time_sync record` | The device clock when the request arrived and when the answer was prepared (SPEC.md 9.5) |
| `0x40` | `MONITOR_LIST` | `monitor` | — | `monitor_declaration record` | Every channel this device asks the client to supply, in one response (SPEC.md 13.3) |
<!-- END GENERATED: control -->

Opcode values `0x05` and `0x31` were assigned by pre-1.0 drafts and remain
unassigned in major version 1.

`status` values:

<!-- BEGIN GENERATED: enum:status -->
| Value | Name | Meaning |
| --- | --- | --- |
| 0 | `ok` | Request accepted |
| 1 | `unsupported_opcode` | Opcode not implemented |
| 2 | `bad_params` | Parameters malformed or out of range |
| 3 | `table_full` | No free subscription slot |
| 4 | `rate_exceeded` | Requested rate is above gps_max_rate_hz or imu_max_rate_hz (SPEC.md 9.6). Never used for CAN |
| 5 | `busy` | A response is already outstanding; wait for it, then retry (SPEC.md 9) |
| 6 | `needs_encryption` | Allocated, never sent: encryption is enforced by GATT permission (SPEC.md 10) |
| 7 | `unknown_subscription` | No installed subscription with that id and mask |
| *other* | *unknown* | MUST decode as unknown, never as a default |
<!-- END GENERATED: enum:status -->

Subscription modes:

<!-- BEGIN GENERATED: enum:sub_mode -->
| Value | Name | Meaning |
| --- | --- | --- |
| 0 | `every_frame` | Forward every frame |
| 1 | `periodic` | arg = minimum interval, ms |
| *other* | *unknown* | MUST decode as unknown, never as a default |
<!-- END GENERATED: enum:sub_mode -->

A device MUST reject a subscription that would exceed `can_subscription_slots`
with `table_full`, rather than accepting it and silently discarding frames.

### 9.1 CAN subscriptions

A subscription matches a frame when `frame.id & mask == sub.id & mask`, taken
over **bits 0–29**: the twenty-nine arbitration bits and bit 29, the standard/
extended format bit. A set bit in `mask` is a bit that a frame must match; a
clear bit is a bit that may hold anything. One entry therefore covers a family
of identifiers, and a mask of zero covers every frame on the bus. Bits 30 and
31 — CAN FD and RTR — describe how a frame was transmitted rather than which
frame it is, and take no part in matching or in identity; a device MUST ignore
them in both `id` and `mask`. Why the table is addressed by mask is
RATIONALE §6.

`CAN_SUBSCRIBE` is exactly `CAN_SUBSCRIBE_MASK` with a mask of `0x3FFFFFFF`.

The format bit is part of a frame's identity because standard `0x1A0` and
extended `0x1A0` are two different frames from possibly two different ECUs. A
client that wants both formats clears bit 29 in its `mask`.

**A subscription is identified by its `(id, mask)` pair**, compared over bits
0–29. Installing a subscription whose `id` and `mask` equal one already
installed MUST update that subscription's `mode` and `arg` in place, keeping
its installation order (§9.2); it MUST NOT consume a second slot. A client
that reprograms unconditionally on every connection therefore cannot exhaust
the table — which is the strategy §4 already forces on it.

`CAN_UNSUBSCRIBE` names the subscription the same way: it removes the one
whose installed `id` and `mask` equal its parameters, and MUST be answered
`unknown_subscription` when none does.

Subscriptions do **not** survive disconnection. A device MUST clear its
subscription table when the link drops, so that a client always finds a known
state and never inherits one it did not install.

### 9.2 Overlapping subscriptions

A frame matching several subscriptions MUST be forwarded **at most once**, and
the subscription that governs it is chosen as follows:

1. The match whose `mask` has the most bits set — the most specific.
2. Among equally specific matches, the one installed earliest.

Both terms are known to the client — it installed the table, this connection —
so which subscription governs a frame is something a client can determine
rather than discover. A device MUST NOT forward one frame once per matching
subscription: duplicate frames on one bus-arrival timestamp are
indistinguishable from a bus fault.

### 9.3 Load

**A device MUST NOT refuse a CAN subscription on rate grounds.** It admits,
and if the resulting load exceeds what it can forward it sheds — reporting the
loss in `dropped` and setting `flags` bit 0 (§6.3). `can_max_frames_per_s`
(§4) describes what the device can forward; it is not a budget the device
polices at admission. The load a subscription will produce is not knowable
when it is installed (RATIONALE §8.7), so shedding is the honest mechanism:
observable, degrading rather than failing, and needing no prediction.

`rate_exceeded` remains for `GPS_SET_RATE` and `IMU_SET_RATE`, where the
device knows its own ceilings and the answer is a fact rather than a forecast.

### 9.4 The request lifecycle

**A client MUST enable indications on Control before its first write.**
Responses arrive by indication on that characteristic, so a write that precedes
enablement is a request whose answer has nowhere to go.

**A device MUST NOT apply a request it cannot answer.** If the response cannot
be delivered — indications not enabled, or a response already outstanding — the
request MUST NOT take effect, and the device MUST NOT count it as received.
Deliverability is decided *before* dispatch, not after: a device that applies
a request whose response is then lost leaves the client no way to find out
what happened.

**Every opcode in this specification is safe to retry:**

| Opcode | Why a retry is safe |
| --- | --- |
| `CAN_SUBSCRIBE`, `CAN_SUBSCRIBE_MASK` | §9.1 — the same `id` and `mask` update in place |
| `CAN_UNSUBSCRIBE` | A second attempt answers `unknown_subscription`; the table is the same either way |
| `CAN_RESET` | Clearing an empty table is clearing an empty table |
| `GPS_SET_RATE`, `IMU_SET_RATE` | Setting a rate to the value it already holds |
| `MONITOR_LIST` | A read |
| `TIME_SYNC` | Each attempt is answered with a fresh reading, never a stale one |

A client MAY therefore retry any request whose response it did not receive. It
MUST NOT assume the original did not take effect — only that repeating it is
harmless.

**A client MUST discard a response whose tag it is no longer waiting on.** A
late response is a measurement of a moment that has passed — most sharply for
`TIME_SYNC`, whose readings were true when taken and are not true now.

### 9.5 TIME_SYNC

The response carries two readings of the device clock:

<!-- BEGIN GENERATED: time_sync -->
*The detail of a TIME_SYNC response. Two readings of one clock, so a client can bound its own error.*

Total: **16 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 8 | `u64` | `t_device_rx` | `µs`; Device clock when the request arrived |
| 8 | 8 | `u64` | `t_device_tx` | `µs`; Device clock when this answer was prepared; MUST NOT be earlier than t_device_rx |
<!-- END GENERATED: time_sync -->

`t_device_rx` is the clock when the write arrived; `t_device_tx` is the clock
when the device finished preparing this answer, and MUST NOT be earlier. A
device MUST take `t_device_rx` when the write arrives, not when it begins
composing the reply — the gap between the two is exactly the processing time
this exchange exists to expose.

The request carries no parameters.

**Units and clocks.** All four timestamps below are **microseconds on a
monotonic clock**. *t₁* and *t₄* are readings of the **client's** monotonic
clock — taken as it issues the write and as the indication arrives — and
`t_device_rx` and `t_device_tx` are readings of the **device's** (§8.1).
Neither clock is UTC and neither is required to relate to it; a GPS fix's
`t_utc` (§5) is the separate mechanism for relating the device to wall time.

    offset ≈ ((t_device_rx − t₁) + (t_device_tx − t₄)) ÷ 2
    delay  ≈ (t₄ − t₁) − (t_device_tx − t_device_rx)

**`offset` is the device clock minus the client clock**, so a device timestamp
is converted to client time by subtracting it and a client timestamp to device
time by adding it. The sign is stated because a client that has it backwards
produces timestamps wrong by twice the offset that look entirely ordinary.

This is the exchange NTP uses, for the reason NTP uses it: one timestamp
cannot bound its own error (RATIONALE §8.7). A client SHOULD issue `TIME_SYNC`
several times and keep the sample with the smallest `delay`. What remains
unmeasurable is the asymmetry between the two queuing delays — the response is
queued until the next connection event — so `delay` is a floor, not a total.

### 9.6 Setting a rate

`GPS_SET_RATE` and `IMU_SET_RATE` each take one `hz:u16` and answer with no
detail. Four rules govern them:

**Zero stops the stream.** The device stops producing notifications on that
characteristic, keeps the client's GATT subscription, and reports
`gps_rate_hz` or `imu_rate_hz` as 0 in Info. It is not an error and not a
shorthand for "restore the default": there is no default (§4).

**A rate the device does not support is `bad_params`.** A device MAY support
only a discrete set of rates. It MUST NOT silently apply the nearest one it
can manage: answering `ok` for a rate it did not adopt is a plausible wrong
value under §1.1. A rate above `gps_max_rate_hz` or `imu_max_rate_hz` is
`rate_exceeded` instead, because that ceiling is a fact the client could have
read in advance. There is deliberately no way to enumerate the supported
rates: a client asks and finds out, one round trip on a link it already has.

**The applied rate is read back from Info**, where `gps_rate_hz` and
`imu_rate_hz` are the rate currently in effect (§4). Putting the applied rate
in the response as well would create two statements of it that a device could
let disagree.

**The change takes effect within one notification.** After an `ok`, at most
one further notification MAY be produced at the old rate — the one already
batched when the request arrived. A device MUST NOT reuse a batch across the
change: §7's `period` and §6.1's `t_base` describe the batch they are in, so a
batch spanning a rate change describes itself wrongly.

Both opcodes are idempotent: setting a rate to the value it already holds is
`ok` and changes nothing (§9.4).

---

## 10. Security

**Encryption is the device's decision, not this specification's.** A device MAY
require an encrypted link on any characteristic, on all of them, or on none.

**A client MUST support encryption on every characteristic.** A client that
meets `Insufficient Encryption` or `Insufficient Authentication` on any read,
write or subscription MUST initiate pairing and retry, and MUST NOT report the
device as faulty or absent. The obligation is one-sided on purpose: requiring
encryption costs a device author real work, while supporting it costs a client
almost nothing — every major central stack turns `Insufficient Encryption`
into a pairing attempt on its own (RATIONALE §8.8).

### 10.1 How a device requires it

A device that requires encryption MUST enforce that with the GATT encryption
permission, not with an application-level check. The two are not
interchangeable: a characteristic carrying the permission is enforced by the
ATT layer, so an unencrypted write never reaches application code and there is
nothing there to generate a reply from.

Status `needs_encryption` (6) remains allocated and MUST NOT be reused for
anything else, but a conforming device has no occasion to send it.

### 10.2 What to protect, and what it buys

A device SHOULD leave the Info characteristic readable on an unencrypted link,
so a client that cannot pair — or has not yet — can still identify what it has
found. Info carries no measurement.

A device that protects anything SHOULD protect the streams and not only
Control: the streams carry the measurement, including position, and encrypting
Control alone guards who may reconfigure the device while leaving what it
reports in the clear. A device fitted to a vehicle bus carrying anything
beyond powertrain telemetry SHOULD require encryption on every characteristic
— a modern bus carries location, identifiers, and door and lock activity, so a
device with access to it is handling personal data whatever it was built to
measure.

When pairing does occur, LE Secure Connections is REQUIRED. Just Works pairing
is acceptable: LE Secure Connections protects against a passive listener even
under Just Works, but Just Works has no authentication step, so it does not
protect against an active man-in-the-middle.

---

## 11. Versioning and compatibility

### 11.1 Major versions

A major version has its own service UUID (§3). A client scans for the majors it
implements; an unimplemented major is a discovery outcome, never a parsing one.
A device MAY expose several major versions simultaneously as separate services.

### 11.2 Minor versions

A client conforming to minor *N* MUST correctly parse minor *N + k* for all *k*.
Two rules make that structural rather than aspirational:

1. A record's size MUST NOT change within a major version.
2. Reserved bits and reserved bytes MAY be assigned in a minor version. They
   read as zero from older firmware and are ignored by older clients.

### 11.3 What a minor version may add

A minor version has exactly three places to put something new. The list is
closed because a conforming receiver rejects a payload whose length it does
not expect (§5.5, §6.2, §7), so a trailer that did not exist in 1.0 cannot be
introduced later: extensibility is decided before 1.0 or not at all
(RATIONALE §5).

**Extension records**, on the records that carry them:

<!-- BEGIN GENERATED: extensibility -->
| Record | Extensible | Appears |
| --- | --- | --- |
| `info` | No — closed for major version 1 | Once per connection |
| `gps_fix` | **Yes** — `ext_count` trailer (§5.5) | One per notification |
| `can_header` | No — closed for major version 1 | One per notification |
| `can_record` | No — closed for major version 1 | Up to 4000 per second |
| `imu_header` | No — closed for major version 1 | One per notification |
| `imu_sample` | No — closed for major version 1 | Up to 833 per second |
| `monitor_declaration` | No — closed for major version 1 | — |
| `monitor_channel` | No — closed for major version 1 | — |
| `monitor_header` | No — closed for major version 1 | — |
| `monitor_value` | No — closed for major version 1 | — |
| `control_response` | No — closed for major version 1 | — |
| `time_sync` | No — closed for major version 1 | — |
<!-- END GENERATED: extensibility -->

**Reserved space**, for flags and small values. Appendix A lists it.

**New control opcodes.** Control requests and responses are not fixed-size
records, so a minor version may add as many as it needs, with any payload.
This is the general-purpose extension point; multi-bus CAN (§6.9) is intended
to be closed this way.

A record marked closed above stays closed for the life of major version 1. A
field it does not carry today is a VTP/2 change, and §6.5 and §6.6 name two
already: a remote frame's requested length, and CAN FD's BRS and ESI.

### 11.4 Prohibited changes

Within major version 1, an implementation MUST NOT:

- Change the meaning, units or scale of an existing field.
- Change the size, offset or type of an existing field.
- Remove or repurpose a field. Deprecation is expressed by ceasing to set the
  field's validity bit.
- Change the value or meaning of an existing enum member.
- Change any UUID.

New enum members MAY be added. A receiver encountering an unknown enum value
MUST report it as unknown and MUST NOT substitute a default.

### 11.5 No negotiation

The device declares one version; the client adapts. There is no version
negotiation exchange, and a client MUST NOT expect one.

---

## 12. Conformance vectors

`conformance/vectors/` contains byte vectors with their expected decodes. Every
implementation MUST pass those for the roles it declares.

Each case carries `hex` and either `expect` or `must_reject`. A case with
`must_reject` MUST fail to decode; a runner that decodes it has not passed.

The corpus is generated from `schema/vtp1.yaml`. A minor version MAY add cases
and MUST NOT modify or remove an existing case. A change that alters the
expected decode of an existing vector is by definition not a minor version.

### 12.1 What the corpus does not cover

The corpus decodes bytes. Every requirement expressed as a byte layout, a
validity rule, an enum value or a length check is mechanically testable, and
passing the corpus is evidence about all of them.

The transport requirements of §2.1–§2.3 are not of that kind: link-layer
payload, PHY and connection parameters appear in no payload, so no vector can
assert them. They are **integration requirements** — real and normative, but
verifiable only against hardware. An implementer SHOULD treat them as the part
of this specification that needs verifying on a bench rather than in CI.
Everything that exists only as *behaviour* — what a device answers, what its
clock and sequence numbers do over time, what survives a reconnect — is the
harness's job (`harness/`).

---

## 13. Monitor characteristic — WRITE

Every other role carries measurement from the device to the client. Monitor
runs the other way: the client supplies values the device cannot compute, so
that a device with a display can show them. Lap time is the example that
justifies the role — where the start/finish line is exists only in the client.

A device implementing this role MUST set `capabilities` bit 3, which §4.1
requires it to set `control` alongside. Without the bit the `monitor_values`
characteristic is inert (§4.1), and a write to it is answered with an ATT
error rather than silently accepted.

### 13.1 The device asks; the client supplies

The device declares which channels it wants. The client reads that declaration
with `MONITOR_LIST` (§9), evaluates the channels it can, and writes values to
`monitor_values`.

The declaration is **fixed for the duration of a connection**. A device that
needs a different set asks for everything it might display and chooses locally,
or reconnects — the same rule as §9.1's subscription table, for the same
reason: a client that establishes state at connect never inherits state it did
not install.

A client MUST NOT write to `monitor_values` before reading the declaration. A
device MUST ignore a value for a slot it did not ask for.

### 13.2 Channels are enumerated, not computed

<!-- BEGIN GENERATED: enum:channel -->
| Value | Name | Meaning |
| --- | --- | --- |
| 1 | `lap_time` | Elapsed time in the current lap, ms |
| 2 | `last_lap_time` | ms |
| 3 | `best_lap_time` | ms |
| 4 | `delta_best` | Time ahead of or behind the best lap, ms, signed |
| 5 | `predicted_lap_time` | ms |
| 6 | `lap_number` | Laps completed in this session, from 1 |
| 7 | `speed` | mm/s |
| 8 | `session_distance` | m |
| 9 | `session_time` | ms |
| *other* | *unknown* | MUST decode as unknown, never as a default |
<!-- END GENERATED: enum:channel -->

A device names a channel; it does not send an expression to be evaluated, so
the protocol needs no expression language and no parser on either side. Each
channel has exactly one unit, fixed by this table: `lap_time` is milliseconds
everywhere, forever.

A client that does not implement a requested channel MUST report it absent
(§13.4) rather than omitting it: absent is a state the device can render, while
silence is indistinguishable from the client having crashed.

New channels MAY be added in a minor version, so a device MUST treat an
unrecognised channel value as unknown and MUST NOT substitute another.

### 13.3 The declaration

<!-- BEGIN GENERATED: monitor_declaration -->
*Every channel this device asks the client to supply. Followed by `count` monitor_channel entries.*

Total: **2 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 1 | `u8` | `count` | Channels requested; the whole declaration, never a page of it |
| 1 | 1 | `u8` | `reserved` | Declaration metadata; **reserved — MUST be zero** |
<!-- END GENERATED: monitor_declaration -->

followed by `count` entries:

<!-- BEGIN GENERATED: monitor_channel -->
*One channel a device asks the client to supply.*

Total: **4 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 1 | `u8` | `slot` | The device's own name for this value; used in monitor_value |
| 1 | 2 | `u16` | `channel` | enum `channel` |
| 3 | 1 | `u8` | `max_age` | `100ms`; Longest this value may be shown without a refresh; MUST NOT be zero (SPEC.md 13.5) |
<!-- END GENERATED: monitor_channel -->

`slot` is the device's own name for the value; the client quotes it back in
every update. A device MAY use any slot numbers it likes and MUST NOT repeat
one.

**The declaration is not paged.** `MONITOR_LIST` takes no parameters and
answers with the whole of it: §13.4 caps a device at 15 channels, which is 62
bytes — comfortably inside the 97 a response carries at the minimum ATT MTU.
A page index could never be anything but zero.

### 13.4 Values

The client writes a batch header followed by values:

<!-- BEGIN GENERATED: monitor_header -->
*Batch header for a client-to-device value update. Followed by `count` monitor_value entries.*

Total: **4 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 2 | `u16` | `seq` | Updates written by the client; +1 each, wraps, restarts at 0 per connection |
| 2 | 1 | `u8` | `count` | — |
| 3 | 1 | `u8` | `reserved` | Update metadata; **reserved — MUST be zero** |
<!-- END GENERATED: monitor_header -->

<!-- BEGIN GENERATED: monitor_value -->
*One value for a slot the device asked for.*

Total: **6 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 1 | `u8` | `slot` | — |
| 1 | 1 | `u8` | `validity` | bitmask `monitor_validity` |
| 2 | 4 | `i32` | `value` | valid when `validity` bit 0 (`present`) is set |
<!-- END GENERATED: monitor_value -->

<!-- BEGIN GENERATED: bitmask:monitor_validity -->
| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `present` | The client can currently supply this channel |
| 1+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:monitor_validity -->

The write length MUST equal the header plus exactly `count` values, and a device
MUST reject any other length. A client MUST NOT write more than the negotiated
ATT MTU permits.

**A value whose `present` bit is clear MUST be written as zero and MUST be
rendered as unavailable.** This is §1.1 in the one place the protocol reverses
direction: before the first lap there is no last lap time, and a device that
displays 0.000 for it has been told something false. The client MUST clear the
bit rather than omit the slot or send a placeholder.

**Every write MUST carry a value for every slot the device asked for.** A
Monitor write is a complete statement of what the client can currently supply,
not a set of changes to what it said before. Complete writes cost almost
nothing at any plausible channel count, and they buy two things deltas do not:
a lost write changes nothing permanently, so `seq` gaps need no recovery
procedure — and the client never has to remember what it last sent, which is
the state that silently diverges when an app is backgrounded and resumed.

A slot MUST appear at most once in a write. A device MUST reject a write
containing a slot twice, because nothing says which of the two wins.

**`count` MUST NOT be zero**, and a device MUST reject a write that carries no
values: on a device that asked for channels, an empty write names none of
them, which is not "nothing changed" but "I can supply nothing" said in a way
that leaves every previous value standing. A client with nothing to supply
writes every slot with the `present` bit clear; a client with nothing to say
does not write at all, and §13.5's deadlines take care of the rest.

A device that asked for no channels (§13.5) has no complete write to receive,
so a client MUST NOT write to it at all.

A device MUST NOT ask for more channels than fit in a single write at the
minimum ATT MTU of §2: with a 4-byte header and 6 bytes per value that is
**15 channels**. Complete writes are only a workable rule if a complete write
always fits.

### 13.5 Freshness

A value the client stopped updating is a value the device cannot display
honestly, and the device is the one with the screen.

Each channel in the declaration carries `max_age`, in units of 100 ms. **A
device MUST render a value as unavailable once `max_age` has passed since the
write that last carried it**, exactly as it renders one whose `present` bit is
clear.

**`max_age` MUST NOT be zero.** Every channel a device declares carries a
deadline, so every channel expires, and there is no second rule. A device MUST
NOT declare a channel with `max_age` of 0, and a receiver MUST reject a
declaration containing one.

A device MAY declare no channels at all — a `count` of zero is the state of a
device that has not yet configured itself, or one whose display currently
needs nothing. A client MUST accept the empty declaration and MUST NOT write
values to a device that asked for none.

The client MUST refresh before the deadline. A client SHOULD write only when
something it can supply has changed — but "nothing has changed" is not a
reason to let a value expire, so it MUST write anyway as the deadline
approaches.

`max_age` is per channel because the channels differ in kind: a `lap_time`
ticking up is wrong within a second of going stale, while a `best_lap_time`
stays true until it is beaten. A device SHOULD choose a `max_age` several
times its expected update interval — it bounds how wrong a display may be, not
how often a client must talk — and for a channel that changes rarely it
chooses a large deadline rather than none: 25.5 s is the ceiling, and still a
bound.

### 13.6 What Monitor is not

It is not a route for vehicle data. A client MUST NOT use it to send back
anything it received from the device, and a device MUST NOT rely on it for
anything it records. Monitor drives a display; the recording is the client's.

---

## Appendix A — Reserved space

Generated from `schema/vtp1.yaml`, so it cannot disagree with the bitmask and
record tables above.

<!-- BEGIN GENERATED: reserved_space -->
| Location | Reserved | Purpose |
| --- | --- | --- |
| `gps_fix.validity` | bits 12–31 | Validity for fields added in a later minor |
| `gps_fix.fix_flags` | bits 5–7 | Additional solution-quality flags |
| `info.capabilities` | bits 7–31 | Roles and features added in a later minor |
| `can_header.flags` | bits 1–7 | Additional batch-level CAN status |
| `imu_header.flags` | bits 3–7 | Additional sensor groups |
| `info.clock_flags` | bits 2–7 | Additional clock properties |
| `monitor_value.validity` | bits 1–7 | Validity for values added in a later minor |
| `info.reserved_20` | 1 byte | Was can_max_payload; derived from the capability bits since (SPEC.md 4.1) |
| `info.reserved_22` | 2 bytes | Was max_notify_bytes; a notification is bounded by the negotiated ATT payload, which the client's stack already knows (SPEC.md 2) |
| `can_header.reserved` | 2 bytes | Low byte earmarked for a bus index (SPEC.md 6.9); high byte unassigned |
| `imu_header.reserved` | 2 bytes | In-band IMU metadata |
| `monitor_declaration.reserved` | 1 byte | Declaration metadata |
| `monitor_header.reserved` | 1 byte | Update metadata |
| Extension types | `0x80`–`0xFF` | Vendor-private; this specification MUST NOT assign them (§5.5) |
<!-- END GENERATED: reserved_space -->

---

## Appendix B — A minimal device

*Non-normative. Every rule below is stated normatively elsewhere; this is the
shortest path through them.*

The smallest conforming VTP/1 device is a GPS-only logger, and it is small:

1. **Advertise** the VTP/1 service UUID (§3.3).
2. **Expose the fixed attribute table** (§4.1): all six characteristics. Four
   are inert — `can`, `imu` and `gps`'s CCCDs exist and accept writes;
   `control` and `monitor_values` reject every write with an ATT error. Inert
   code is a handful of lines.
3. **Answer Info** (§4): one constant 24-byte record. `protocol_major` 1,
   `capabilities` bit 0 (`gps`) only, `gps_rate_hz` and `gps_max_rate_hz` set
   to the fix rate, every other field zero.
4. **Notify one 74-byte `gps_fix` per solution** (§5): set the validity bits
   for the fields the receiver actually supplies and write zeroes behind the
   rest; stamp `t_device` from one monotonic microsecond clock (§8.1); start
   `seq` at 0 each connection and count notifications (§8.2).

There is no control plane, no batching, no subscription table and no Monitor
on this device: each belongs to a capability bit it leaves clear. Adding a
role later means setting its bit and implementing its section — nothing about
the GPS path changes.

Check it by decoding your own notifications with a reference decoder
(`reference/`), then point the harness at the running device:

```sh
uv run vtp1-harness
```

It reads Info, sees which roles you declared, and tests exactly those.
