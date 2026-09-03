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
| `aiding` | client → device, write without response | A 3-byte chunk header, then the chunk's payload | §14 |

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
| Characteristic `aiding` | `56544307-5f05-5b56-af87-dcab2baf2522` |
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
| 20 | 1 | `u8` | `obd_poll_slots` | Most PIDs one OBD_POLL_SET may name (SPEC.md 15.4); 0 if no OBD |
| 21 | 1 | `u8` | `clock_flags` | bitmask `clock_flags` |
| 22 | 2 | `u16` | `reserved_22` | Was obd_min_interval_ms; withdrawn with the fixed poll clock (SPEC.md 15.4); **reserved — MUST be zero** |
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
| 7 | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
| 8 | `power` | Device reports its own power state (SPEC.md 9.7) **Requires `control`.** |
| 9 | `gnss_aiding` | Client supplies orbit data to the device's receiver (§14) **Requires `gps`, `control`.** |
| 10 | `obd` | Device transmits OBD-II diagnostic requests on the bus (§15) **Requires `can`, `control`.** |
| 11+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
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
| `aiding` | bit 9 (`gnss_aiding`) | `write-without-response` (write without-response) | — | client | device | writes are silently discarded; a client cannot reach this characteristic legitimately without GNSS_AID_BEGIN having answered ok first |
<!-- END GENERATED: profile:attributes -->

A device MUST NOT add a characteristic to the VTP/1 service beyond these, and
MUST expose at least the properties listed. It MAY expose more — making `gps`
readable is a common convenience — and a client MUST NOT rely on any property
the table does not list, so it MUST NOT read `gps` in place of subscribing.

An inert characteristic costs its implementer almost nothing. A device without
the `control` bit exposes the Control characteristic and rejects every write
with an ATT error; it does not parse opcodes, implement indications, or answer
`unsupported_opcode`. The same goes for `monitor_values`.

`aiding` is the one exception, and only because ATT gives it no choice: a Write
Command carries no response of any kind, so an inert `aiding` discards silently
and §14 puts every refusal a client needs on Control instead. A GPS-only build
is a service declaration, five inert attributes and one notify path.

Every notifying and indicating characteristic carries its Client
Characteristic Configuration descriptor whatever the capability bit says. A
device MUST accept a CCCD write on an inert stream — and then simply never
notifies — and MUST NOT reject one on the grounds that the capability is
absent. A client enables the CCCD for a role whose bit is set, and leaves the
others alone.

**Capability implications are normative.**

<!-- BEGIN GENERATED: profile:capabilities -->
| Bit | Capability | Requires | Capacity fields that MUST be zero when clear | ...and non-zero when set |
| --- | --- | --- | --- | --- |
| 0 | `gps` | — | `gps_rate_hz`, `gps_max_rate_hz` | — |
| 1 | `can` | bit 4 (`control`) | `can_subscription_slots`, `can_max_frames_per_s` | — |
| 2 | `imu` | — | `imu_rate_hz`, `imu_max_rate_hz` | — |
| 3 | `monitor` | bit 4 (`control`) | — | — |
| 4 | `control` | — | — | — |
| 5 | `can_fd` | bit 1 (`can`) | — | — |
| 6 | `masked_subscriptions` | bit 1 (`can`) | — | — |
| 8 | `power` | bit 4 (`control`) | — | — |
| 9 | `gnss_aiding` | bit 0 (`gps`), bit 4 (`control`) | — | — |
| 10 | `obd` | bit 1 (`can`), bit 4 (`control`) | `obd_poll_slots` | `obd_poll_slots` |
<!-- END GENERATED: profile:capabilities -->

**The largest CAN payload follows from the bits and is not a field.** A client
computes it:

| `can` | `can_fd` | Largest payload |
| --- | --- | --- |
| clear | clear | 0 — the device has no CAN |
| set | clear | 8 — Classic CAN |
| set | set | 64 — CAN FD |

`set`/`set` is the only combination `can_fd` permits, because `can_fd`
requires `can`. Byte 20 of Info once carried this value, was reserved when
this section began deriving it — a field whose every value is derivable is a
field two implementations can disagree about — and §15 has since assigned it
to `obd_poll_slots`.

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

One notification carries exactly one GNSS solution — a position solution
where the receiver has one, and otherwise whatever solution `fix_type` names
(§5.2). There is no pairing between characteristics and no reassembly.

<!-- BEGIN GENERATED: gps_fix -->
*One complete GNSS solution. Never split, never paired, never packed.*

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
| 68 | 2 | `u16` | `p_dop` | scale 0.01; Position dilution of precision; absent on a fix reporting no position (SPEC.md 5.2); valid when `validity` bit 10 (`p_dop`) is set |
| 70 | 1 | `u8` | `fix_type` | enum `fix_type` |
| 71 | 1 | `u8` | `num_sv` | Satellites used in the solution fix_type names, positional or not (SPEC.md 5.2); valid when `validity` bit 11 (`num_sv`) is set |
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
| 10 | `p_dop` | Clear on a fix reporting no position (SPEC.md 5.2) |
| 11 | `num_sv` | Set on any solution satellites were used in, position or time (SPEC.md 5.2) |
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

`fix_type` has no validity bit and is present on every fix. It is the only
field that says which solution the receiver reached, and it says so on a fix
carrying no position as much as on one carrying a position.

**`num_sv` counts the satellites used in the solution `fix_type` names**, which
is not always a position solution. Bit 11 answers whether the device has that
count and nothing else; it is not a second statement about whether the record
carries a position. A `time_only` solution is computed from real satellites, so
a device holding the count MUST set bit 11 and report it — withholding
`num_sv` because the fix carries no position is not conforming. A `fix_type` of
`none` names no solution at all, so no satellite was used in one and a device
MUST leave bit 11 clear: satellites tracked but unused are not what this field
counts, and this specification carries no field that counts them.

Zero is a measurement where the reported solution genuinely used no satellites
— a `dead_reckon` fix — and a device holding that count sets bit 11 and writes
zero. §5.1 keeps the two apart: absence is the bit's to signal, and no value of
`num_sv` signals it.

**A `fix_type` of `none` or `time_only` reports no position**, so a device
MUST leave the `position` bit clear on such a fix. The two never disagree: a
record naming no position solution and carrying a position leaves a client to
choose between them, and nothing on the wire says which is the defect.

**`p_dop` describes the geometry of a position solution**, so a device MUST
leave bit 10 clear on a fix reporting no position: a `fix_type` of `none` or
`time_only`, or any fix whose `position` bit is clear.

The two fields are adjacent and their bits are not a pair. On a `time_only`
fix, bit 11 carries a measurement and bit 10 is clear. Every rule in this
subsection binds the device; a receiver MUST decode a fix that breaks one —
the payload is well-formed — and SHOULD surface the violation as a device
defect. It MUST NOT
reject the fix, and MUST NOT read a `p_dop` beside an absent position as
evidence that a position exists. The same holds for a position beside a
`fix_type` that names none: a receiver decodes both and MUST NOT pick a winner
on the device's behalf.

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
| 1 | `polling` | The OBD poll set is non-empty: this device is transmitting diagnostic requests (SPEC.md 15.6) |
| 2+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
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

`flags` bit 1 (`polling`) belongs to the OBD role and is specified in §15.6:
it is set exactly while the device's OBD poll set is non-empty, so anyone
reading the stream can tell a transmitting device from a listening one.

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
the interval, and "the first matching frame" — **per (subscription,
identifier) pair**. Not one set of state per subscription: a shared interval
lets whichever identifier arrives first consume it, so a client sees one signal
out of a group it subscribed to as a group, and the failure looks like a quiet
bus rather than a bug. And not one set per identifier: state keyed by the
identifier alone belongs to whichever subscription last matched it, so a second
subscription covering one identifier of a masked group destroys the schedule of
the first.

**A displaced subscription keeps its schedule.** §9.2 decides which
subscription governs a frame; it does not decide that the others have stopped
applying. Scheduling state belongs to the subscription that owns it, and a
subscription displaced from governance of an identifier — by a more specific
subscription installed later, say — MUST retain that state and resume from it
if governance returns, subject only to the bound below. A device that discards
it forwards immediately on the identifier's next frame, inside an interval the
client set and never withdrew.

**A re-install that changes nothing changes nothing.** A `CAN_SUBSCRIBE` or
`CAN_SUBSCRIBE_MASK` naming an `(id, mask)` already installed, with the same
`mode` and the same `arg`, MUST leave that subscription's scheduling state
exactly as it was: no interval restarts, and no first frame is owed. §9.4 makes
every request here safe to retry, and a client retrying a request whose
response was lost MUST NOT be paid for it with a frame inside the interval it
asked for — the client cannot tell the two cases apart, so the device must
make them identical.

A re-install that changes `mode` or `arg` is a new instruction rather than a
repetition. It MUST re-arm the first matching frame for every identifier the
subscription matches, so a client that changes its mind sees a value without
waiting out the new interval. Installation order is kept either way (§9.1).

The number of such pairs is bounded by the identifiers actually seen, which a
device cannot know when the subscription is installed. A device MAY bound the
state it keeps; when the bound is reached it sheds, exactly as for load (§6.3):
frames it can no longer schedule are discarded, counted in `dropped`, and the
shedding flag is set. A device MUST NOT silently forward such frames
unscheduled, and MUST NOT drop the subscription.

**What a bounded device sacrifices.** State retained for a displaced
subscription is a rate limit nothing is currently using; a shed frame is a
subscription the client installed and is hearing nothing from, including the
first frame this section promises it. So a device that has reached its bound
MUST reclaim state belonging to subscriptions that do not currently govern
their identifier before it sheds a frame whose governing subscription has no
state. Reclaiming costs one early frame if governance returns, and that cost is
conformant: it is the exception the displacement rule above names. When every
entry belongs to a governing subscription there is nothing left to reclaim,
the device sheds, and which entry it then keeps is its own choice.

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

**A response is owed from the moment its request is accepted until the device
has sent it** — handed it to the transport with nothing further for the device
to do. A response it has not composed yet is owed too: `OBD_INFO` is answered
only once its probe completes (§15.2), and its request is outstanding for the
whole of that.

**The send, and not the confirmation, because the client's boundary is the
arrival.** A client writes again as soon as the response reaches it, and ATT
permits that before its confirmation has gone out. A device that kept owing
until the confirmation would answer `busy` to a client that had waited exactly
as long as this section tells it to, and the retry would meet the same window
again. A device's boundary has to fall no later than the client's; the send
does, the confirmation does not (RATIONALE §8.7).

A device MUST answer `busy` to a request that arrives while it still owes a
response, and MUST NOT apply it — unless it has no room to hold the refusal,
in which case it MUST discard the request unanswered and unapplied rather than
apply one it cannot answer. A client that receives `busy` has broken the rule
above; it MUST wait for the outstanding response and MAY then retry, and MUST
NOT treat the request as refused — `busy` says nothing about the request
itself.

**One outstanding indication is a reason to hold a response, not to refuse a
request.** The link carries one indication at a time, so a response composed
while an earlier one is still unconfirmed waits for that confirmation before
it is sent. **A device MUST be able to hold one such response**, because that
window is exactly where a conforming client's next request arrives. A `busy`
refusal is a response and waits its turn the same way. Past that a device has
no room, which is the discard above: a client writing faster than a bounded
device can answer has already broken the one-outstanding rule, and holding
more would let it size the device's memory.

A client MUST NOT reuse a `tag` while a request bearing it is outstanding. It
needs no enforcement: with one request outstanding, a second write is refused
`busy` whatever tag it carries, so a device keeps no table of tags — each tag
rides in the response composed for it and is gone once that response is sent.
A tag becomes reusable as soon as the response bearing it has arrived.

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
| `0x01` | `CAN_RESET` | `can` | — | — | Clear all subscriptions, clear the OBD poll set (SPEC.md 15.7), and stop the CAN stream |
| `0x02` | `CAN_SUBSCRIBE` | `can` | `id:u32, mode:u8, arg:u16` | — | Equivalent to CAN_SUBSCRIBE_MASK with mask 0x3FFFFFFF |
| `0x03` | `CAN_SUBSCRIBE_MASK` | `masked_subscriptions` | `id:u32, mask:u32, mode:u8, arg:u16` | — | — |
| `0x04` | `CAN_UNSUBSCRIBE` | `can` | `id:u32, mask:u32` | — | Removes the subscription whose id and mask these are (SPEC.md 9.1) |
| `0x10` | `GPS_SET_RATE` | `gps` | `hz:u16` | — | 0 stops the stream; unsupported rates answer bad_params (SPEC.md 9.6) |
| `0x11` | `GNSS_AID_INFO` | `gnss_aiding` | — | `gnss_aid_caps record` | What aiding this device accepts, and what it already holds (SPEC.md 14.2) |
| `0x12` | `GNSS_AID_BEGIN` | `gnss_aiding` | `format:u8, total_bytes:u32` | `aid_begin_result record` | Open a transfer; the response fixes the chunk size (SPEC.md 14.3) |
| `0x13` | `GNSS_AID_COMMIT` | `gnss_aiding` | `crc32:u32` | `aid_commit_result record` | Close the open transfer and report what became of it (SPEC.md 14.4) |
| `0x20` | `IMU_SET_RATE` | `imu` | `hz:u16` | — | 0 stops the stream; unsupported rates answer bad_params (SPEC.md 9.6) |
| `0x30` | `TIME_SYNC` | — | — | `time_sync record` | The device clock when the request arrived and when the answer was prepared (SPEC.md 9.5) |
| `0x40` | `MONITOR_LIST` | `monitor` | — | `monitor_declaration record` | Every channel this device asks the client to supply, in one response (SPEC.md 13.3) |
| `0x50` | `GET_POWER` | `power` | — | `power_state record` | What the device knows about its own supply, measured when asked (SPEC.md 9.7) |
| `0x60` | `OBD_INFO` | `obd` | — | `obd_probe record` | Probe the bus and report what answered; replaces the probe result and clears the poll set (SPEC.md 15.2) |
| `0x61` | `OBD_POLL_SET` | `obd` | `interval_ms:u16, count:u8, schedule:u8*` | — | Replace the whole poll set; count 0 stops transmitting. Response-paced: interval_ms is a MINIMUM spacing and 0 means none (SPEC.md 15.4). Bit 7 of a PID byte groups it with the next and each group carries a u16 minimum interval in ms (SPEC.md 15.4.1, 15.4.2) |
<!-- END GENERATED: control -->

Opcode values `0x05`, `0x14` and `0x31` were assigned by pre-1.0 drafts and
remain unassigned in major version 1.

`status` values:

<!-- BEGIN GENERATED: enum:status -->
| Value | Name | Meaning |
| --- | --- | --- |
| 0 | `ok` | Request accepted |
| 1 | `unsupported_opcode` | Opcode not implemented |
| 2 | `bad_params` | Parameters malformed or out of range |
| 3 | `table_full` | No free subscription slot |
| 4 | `rate_exceeded` | Requested rate is above gps_max_rate_hz or imu_max_rate_hz (SPEC.md 9.6). Never used for CAN |
| 5 | `busy` | A response is still owed; wait for it, then retry (SPEC.md 9) |
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
with `table_full`, rather than accepting it and silently discarding frames. A
re-install of an `(id, mask)` the table already holds exceeds nothing and MUST
NOT be refused on capacity grounds (§9.1).

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
0–29 — the pair as the client wrote it, not `id & mask`. Two subscriptions
whose masks are equal and whose `id`s differ only in bits that mask clears
match the same frames, and are nevertheless two subscriptions: each occupies a
slot and each is removed by the parameters that installed it. A client is
answerable for the bytes it sent and nothing else.

Installing a subscription whose `id` and `mask` equal one already installed
MUST update that subscription's `mode` and `arg` in place, keeping its
installation order (§9.2); it MUST NOT consume a second slot, and it MUST be
answered `ok` whether or not the table is full — the free-slot check governs a
subscription being created, and this creates none. A client that reprograms
unconditionally on every connection therefore cannot exhaust the table — which
is the strategy §4 already forces on it. What becomes of the subscription's
scheduling state is §6.8: nothing at all when `mode` and `arg` are unchanged.

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

This section decides which subscription forwards a frame, and nothing more. A
subscription that loses an identifier to a more specific one is still
installed, still covers the rest of what its mask matches, and keeps its
schedule for the identifier it lost (§6.8).

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
be delivered — indications not enabled, or a response still owed from an
earlier request — the request MUST NOT take effect, and the device MUST NOT
count it as received. Deliverability is decided *before* dispatch, not after: a
device that applies a request whose response is then lost leaves the client no
way to find out what happened.

**Every opcode in this specification is safe to retry:**

| Opcode | Why a retry is safe |
| --- | --- |
| `CAN_SUBSCRIBE`, `CAN_SUBSCRIBE_MASK` | §9.1 — the same `id` and `mask` update in place, and §6.8 — an unchanged `mode` and `arg` leave the schedule untouched, so the retry costs no frame |
| `CAN_UNSUBSCRIBE` | A second attempt answers `unknown_subscription`; the table is the same either way |
| `CAN_RESET` | Clearing an empty table is clearing an empty table |
| `GPS_SET_RATE`, `IMU_SET_RATE` | Setting a rate to the value it already holds |
| `MONITOR_LIST` | A read |
| `TIME_SYNC` | Each attempt is answered with a fresh reading, never a stale one |
| `OBD_INFO` | Each attempt probes afresh and answers with a fresh reading — at the cost §15.2 states: a probe transmits, and replaces the probe result and poll set the lost attempt had already replaced |
| `OBD_POLL_SET` | §15.4 — the request replaces the whole set, so repeating it replaces the set with itself |

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

### 9.7 Power

A device that runs on a battery knows something a client cannot measure and a
driver needs before a session rather than after it. `GET_POWER` asks for it.
The detail of a successful response is one `power_state` record:

<!-- BEGIN GENERATED: power_state -->
*The detail of a GET_POWER response. What the device knows about its own supply, and no more.*

Total: **4 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 1 | `u8` | `validity` | bitmask `power_validity` |
| 1 | 1 | `u8` | `source` | enum `power_source`; valid when `validity` bit 0 (`source`) is set |
| 2 | 1 | `u8` | `percent` | `%`; Charge remaining, 0..100; a device MUST NOT emit a larger value (SPEC.md 9.7); valid when `validity` bit 1 (`percent`) is set |
| 3 | 1 | `u8` | `reserved` | Power metadata; **reserved — MUST be zero** |
<!-- END GENERATED: power_state -->

<!-- BEGIN GENERATED: bitmask:power_validity -->
| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `source` | source is valid |
| 1 | `percent` | percent is valid |
| 2+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:power_validity -->

<!-- BEGIN GENERATED: enum:power_source -->
| Value | Name | Meaning |
| --- | --- | --- |
| 1 | `external` | Running from an external supply. Says nothing about a battery: `percent` reports one if the device has one |
| 2 | `discharging` | Running from its battery |
| 3 | `charging` | External supply present, battery taking charge |
| 4 | `charged` | External supply present, battery no longer taking charge |
| *other* | *unknown* | MUST decode as unknown, never as a default |
<!-- END GENERATED: enum:power_source -->

**The value is measured when the request arrives**, so the record carries no
timestamp; a client that wants a fresher answer asks again. Nothing here is
pushed — a supply reading changes over minutes, and §11.3 makes an opcode the
extension point for a value a client asks for rather than one the device
sends.

**`percent` is 0..100, and the bound is the device's.** A device MUST NOT emit
a `percent` above 100 with its bit set. The record is well formed, so a
receiver MUST decode it — and MUST NOT clamp or repair the value, and SHOULD
surface it as a device defect: a clamp shows a full battery on a device that
has lost track of its own pack. This is the malformed/content division §1.1
draws, the same one §5.4 draws for a latitude beyond the pole.

**`source` is what keeps `percent` honest.** A logger wired to the car's
ignition feed has no charge to report, and without somewhere to say so it must
answer 100% forever — a reserved value meaning "not applicable" in the one
field a client renders as a gauge, which is the failure §1.1 exists to
prevent. The two fields are independently valid: a device on external power
sets `source` and clears `percent`; a device whose gauge failed mid-session
does the reverse. A field whose bit is clear MUST be written as zero and MUST
be reported absent, exactly as everywhere else (§1.1).

**`source` says what the device is running on, not what it contains.**
`external` claims nothing about a battery, so `external` with a valid
`percent` is an ordinary device — plugged in, with a pack at 40% — and
`external` with the percent bit clear is one with no pack at all. A device
SHOULD report the most specific member it can support — `charging` and
`charged` are for hardware that can tell them apart — and MUST NOT report one
it cannot.

**A device MUST NOT declare `power` and then answer with every validity bit
clear.** With nothing valid it has said what a device without the capability
says by not declaring it. The empty record still decodes — this is a rule
about what a device declares, not about a payload, and one of the several
rules in §9 no byte vector can reach.

A device MAY also expose the standard Battery Service (`0x180F`) for generic
tools, exactly as §3.4 recommends the Device Information Service; a device
that exposes both MUST NOT let them disagree. `power` is capability bit 8, the
first bit past the eight the advertisement carries (§3.3), so a client learns
of it from Info, after connecting — which is also when it could first draw it.
RATIONALE §9 has the full argument, including why there are no volts here.

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
| `power_state` | No — closed for major version 1 | On request |
| `gnss_aid_caps` | No — closed for major version 1 | — |
| `aid_begin_result` | No — closed for major version 1 | — |
| `aid_chunk` | No — closed for major version 1 | — |
| `aid_commit_result` | No — closed for major version 1 | — |
| `obd_probe` | No — closed for major version 1 | — |
| `obd_ecu` | No — closed for major version 1 | — |
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

§9.7's power reading sits between the two. Its layout and validity rules are
in the corpus; whether the numbers are true is not, and cannot be — a device
reporting a full pack that is flat produces bytes no vector can distinguish
from a device that is right, and no protocol can verify a measurement about
the thing doing the measuring.

§15 splits the same way, more sharply than anything else here. The probe
record, the polling flag and both capacities are bytes, and the corpus holds
them. The transmit rules of §15.1 — what goes on the bus, how far apart,
never retried — appear in no payload at all, and they are the rules the role
exists for. They are tested where behaviour is tested: the harness drives
the poll loop and watches the flag, and the bus itself is verified on a
bench, §2's own division.

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

## 14. Aiding characteristic — WRITE

Aiding runs the same direction as Monitor (§13) — client to device — but
carries bulk data rather than values for a display. A GNSS receiver starting
without valid orbit data must read it from the satellites themselves, which
takes tens of seconds under an open sky and may not complete at all in a
paddock or under a grandstand. A client has a network connection and can
supply the same data in about a second.

A device implementing this role MUST set `capabilities` bit 9, which §4.1
requires it to set `gps` and `control` alongside. The `aiding` characteristic
is present on every VTP/1 device whether or not the role is implemented
(§4.1).

This is the only attribute written **without a response**, and the only one
whose inert form discards writes silently rather than answering an ATT error.
Both follow from the same fact: a chunk is only ever legitimate after
`GNSS_AID_BEGIN` has answered `ok`, so a chunk arriving at a device that never
answered one is a client that broke §14.3, not a condition needing a reply. Every
outcome a client acts on is reported by `GNSS_AID_COMMIT` on the Control plane,
which has tagged responses and typed failures already.

Chunks are not control requests. A client MAY write them while a control
request is outstanding, and §9's one-outstanding-request rule does not apply to
them.

### 14.1 The device declares; the client supplies

The bytes of a transfer are **opaque to this specification**. A device forwards
them to its receiver without interpreting them, and no requirement here
constrains their content.

What the protocol does carry is which format those bytes are in, and that is
the device's declaration rather than the client's choice — the format belongs
to the receiver, and the client cannot derive it:

<!-- BEGIN GENERATED: enum:aid_format -->
| Value | Name | Meaning |
| --- | --- | --- |
| 1 | `ubx_mga` | A concatenated sequence of UBX-MGA messages, as served by u-blox AssistNow |
| *other* | *unknown* | MUST decode as unknown, never as a default |
<!-- END GENERATED: enum:aid_format -->

It is an enumeration for §13.2's reason. A device names a format; it does not
describe one. A client therefore cannot fail to understand a declaration in any
way except not implementing it, and there is no vendor string namespace on
either side.

A client MUST read `GNSS_AID_INFO` before opening a transfer, and MUST NOT open
one naming a format the device did not declare. A device MUST answer
`bad_params` to a `GNSS_AID_BEGIN` naming any other format.

New formats MAY be added in a minor version, so a client MUST treat an
unrecognised format value as unknown and MUST NOT substitute another.

### 14.2 What the device already holds

The detail of a successful `GNSS_AID_INFO` response is one `gnss_aid_caps`
record:

<!-- BEGIN GENERATED: gnss_aid_caps -->
*What aiding this device accepts, and what it already holds.*

Total: **16 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 1 | `u8` | `validity` | bitmask `aid_validity` |
| 1 | 1 | `u8` | `format` | The one format this device accepts; enum `aid_format` |
| 2 | 2 | `u16` | `reserved_2` | Was aid_flags; aiding metadata; **reserved — MUST be zero** |
| 4 | 4 | `u32` | `max_bytes` | `bytes`; Largest total_bytes this device will accept in one transfer |
| 8 | 8 | `i64` | `held_until` | `ms`; Unix epoch; the end of the validity window this device already holds (SPEC.md 14.2); valid when `validity` bit 0 (`held_until`) is set |
<!-- END GENERATED: gnss_aid_caps -->

<!-- BEGIN GENERATED: bitmask:aid_validity -->
| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `held_until` | held_until carries the end of a window the device already holds |
| 1+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:aid_validity -->

`max_bytes` is a device ceiling. A `GNSS_AID_BEGIN` whose `total_bytes` exceeds
it MUST be answered `bad_params`.

`held_until` describes what the device holds **now**, on this connection. A
client SHOULD NOT send data whose validity the device already covers;
predicted-orbit products run to tens of kilobytes, and re-sending one the
device is still holding costs a phone's radio that much for nothing. With the
`held_until` validity bit clear the device holds nothing, or does not know
what it holds; either way the client sends.

Whether what it holds survives a power cycle is deliberately not declared: the
client re-reads `GNSS_AID_INFO` on every connection (§14.1), and that read is
the answer for that connection. A claim about the next boot would be a value
nothing can act on.

### 14.3 Opening a transfer, and filling it

`GNSS_AID_BEGIN` carries the format and the total byte count. Its response
detail is one `aid_begin_result` record:

<!-- BEGIN GENERATED: aid_begin_result -->
*The detail of a GNSS_AID_BEGIN response. Opens a transfer and fixes its chunking.*

Total: **4 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 1 | `u8` | `token` | Names this transfer; echoed in every chunk. MUST differ from the previous transfer's (SPEC.md 14.3) |
| 1 | 2 | `u16` | `chunk_bytes` | `bytes`; Payload bytes in every chunk but the last; byte offset is index x chunk_bytes (SPEC.md 14.3) |
| 3 | 1 | `u8` | `reserved_3` | Transfer metadata; **reserved — MUST be zero** |
<!-- END GENERATED: aid_begin_result -->

`chunk_bytes` MUST NOT be zero and MUST NOT exceed `ATT_MTU - 6` — three
bytes of ATT Write Command header and the three-byte chunk header below — for
**every ATT bearer the client may write chunks on**. Unenhanced ATT has
exactly one bearer, so a client there reads this as the negotiated MTU; EATT
lets a client hold several bearers with MTUs of their own, and a device
cannot see which one a chunk will take, so a device that has granted more
than one SHOULD size `chunk_bytes` against the smallest of them.

**A transfer MUST NOT require more than 65535 chunks.** A chunk's `index` and
§14.4's `first_missing` are both `u16`, so a transfer needing more has chunks
that cannot be named. `total_bytes` is `u32` and nothing else bounds the pair,
so the device enforces it where both numbers are first known: a device MUST
answer `bad_params` to a `GNSS_AID_BEGIN` whose `total_bytes` would need more
chunks than that at the `chunk_bytes` it would have chosen.

**A device holds at most one transfer open, and `GNSS_AID_BEGIN` is what
closes the previous one.** A BEGIN arriving while a transfer is open MUST
discard the open transfer whole — its chunks with it — and start the new one.
The new transfer's `token` MUST differ from the discarded one's. The token is
what keeps the two apart: ATT orders writes only **within one bearer**, and
EATT allows a client several, so a chunk of the discarded transfer still
queued on one bearer can arrive after a `GNSS_AID_BEGIN` sent on another —
carrying the old token, it is ignored instead of landing at whatever offset
its index names in the new transfer. A device MUST also discard an open
transfer on disconnect — the client that would have committed it is gone.

Chunks are written to the `aiding` characteristic:

<!-- BEGIN GENERATED: aid_chunk -->
*One chunk of an aiding transfer, written without a response.*

Total: **3 bytes + `payload`**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 1 | `u8` | `token` | Echoed from the GNSS_AID_BEGIN that opened this transfer |
| 1 | 2 | `u16` | `index` | 0-based; the payload belongs at index x chunk_bytes |
<!-- END GENERATED: aid_chunk -->

A chunk's payload belongs at byte offset `index × chunk_bytes`. **Every chunk
but the last MUST carry exactly `chunk_bytes`**, and the last MUST carry the
remainder of `total_bytes`. The mapping from index to offset is therefore
arithmetic, which is what makes resending part of a transfer possible at all:
a device that had to place variable-length chunks could not place chunk 7
without having received 0 through 6, and §14.4's missing-chunk report would
have nothing to offer.

A device MUST ignore, without any response, a chunk that:

- arrives with no transfer open,
- carries a `token` other than the open transfer's,
- carries an `index` at or beyond `⌈total_bytes ÷ chunk_bytes⌉`, or
- carries a payload of the wrong length for its index.

A client MAY write chunks in any order and MAY write the same chunk more than
once; a device MUST accept a repeat and MUST NOT treat it as an error. A
client holding more than one ATT bearer SHOULD write a transfer's chunks and
its `GNSS_AID_COMMIT` on the same bearer: a commit that overtakes its own
chunks is answered `incomplete` and costs a round trip, though never a
transfer.

**A device MUST NOT hand any part of a transfer to its receiver before
`GNSS_AID_COMMIT`.** A transfer is applied whole or not at all, which is what
makes the CRC in §14.4 worth checking and what stops a receiver being fed the
first half of something.

### 14.4 Closing it

`GNSS_AID_COMMIT` carries one parameter: a CRC-32 over the reassembled
`total_bytes` — **not** over the chunks, and not over the chunk headers. A
device MUST answer `bad_params` to a commit arriving with no transfer open;
everything else about the transfer the device already knows from its own
`GNSS_AID_BEGIN`.

The CRC-32 is the one used by IEEE 802.3 and zlib: polynomial `0x04C11DB7`,
reflected input and output, initial value `0xFFFFFFFF`, final XOR
`0xFFFFFFFF`. It is stated exactly because two implementations that each say
"CRC-32" and disagree about reflection produce a mismatch on every transfer and
a bug report that reads as data corruption.

The response detail is one `aid_commit_result` record:

<!-- BEGIN GENERATED: aid_commit_result -->
*The detail of a GNSS_AID_COMMIT response. What became of the transfer.*

Total: **4 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 1 | `u8` | `validity` | bitmask `commit_validity` |
| 1 | 1 | `u8` | `result` | enum `aid_result` |
| 2 | 2 | `u16` | `first_missing` | Lowest chunk index the device did not receive; the client resends from here (SPEC.md 14.4); valid when `validity` bit 0 (`first_missing`) is set |
<!-- END GENERATED: aid_commit_result -->

<!-- BEGIN GENERATED: bitmask:commit_validity -->
| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `first_missing` | first_missing names a chunk the device did not receive |
| 1+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:commit_validity -->

<!-- BEGIN GENERATED: enum:aid_result -->
| Value | Name | Meaning |
| --- | --- | --- |
| 1 | `applied` | Every chunk arrived, the CRC matched, and the data went to the receiver |
| 2 | `incomplete` | One or more chunks are missing; first_missing names the lowest |
| 3 | `bad_crc` | Every chunk arrived and the CRC-32 does not match |
| 4 | `rejected` | The transfer was intact and the receiver refused it |
| *other* | *unknown* | MUST decode as unknown, never as a default |
<!-- END GENERATED: enum:aid_result -->

A device MUST set the `first_missing` validity bit if and only if `result` is
`incomplete`, and MUST report the **lowest** index it did not receive.

**A result of `incomplete` leaves the transfer open.** The client writes the
chunks it is missing and commits again, and the exchange terminates because
`first_missing` strictly advances each time. Every other result closes the
transfer; a client wanting to retry after `bad_crc` or `rejected` opens a new
one, and a client abandoning a transfer simply opens a new one or disconnects
(§14.3) — there is no abort request, because both of those already are one.


### 14.5 A refused transfer is not a refused request

A well-formed `GNSS_AID_COMMIT` arriving over an open transfer is a request
the device can act on, so it applies it and answers `ok` — including when the
transfer it reports on was incomplete, failed its CRC or was refused by the
receiver. Those outcomes are in `result`, not in `status`.

They cannot be in `status`. §9 makes `detail` present if and only if `status`
is `ok`, so an `incomplete` expressed as a status would carry no
`first_missing` with it, and the client would have nothing to resend from —
the report and the refusal cannot be the same byte. `status` answers whether
the device could act on the request; the detail carries what it found.

### 14.6 What aiding is not

**It is not a source of position.** Aiding MAY seed a receiver's search. A
device MUST NOT report any part of a transfer as measurement: it MUST NOT
appear in a `gps_fix`, and a device MUST NOT set a fix's validity bits on the
strength of it. Aiding is a plausible position arriving from something that is
not a measurement, which makes this the sharpest case of §1.1 in the
specification — a fix built from it would be wrong, plausible, and indistinguishable
from a real one.

Where a format carries time initialisation, a client SHOULD account for the
transfer's own latency in whatever accuracy that format declares. A device
applies a transfer at commit, not as it arrives, so a timestamp written into
the first chunk is already as old as the transfer took.

**It is not a corrections channel.** Differential corrections — the continuous
stream behind the `rtk_float` and `rtk_fixed` solutions §5.3 reports — are the
same shape as orbit data and a different lifecycle: continuous for a session
rather than one transfer at connect. Carrying them needs an inbound rate ceiling
and rules about airtime against CAN that this version does not define, and
§11.3 makes a new opcode the place to define them if a device ever wants one.
Nothing is reserved for them here, and nothing needs to be: §11.4 lets a minor
version add an `aid_format` member at any time.

**It is not a general transport.** A device MUST NOT accept anything but aiding
in the format it declared on this characteristic, and in particular MUST NOT
use it to carry firmware.

A client SHOULD complete a transfer before it enables notifications on any
stream, and SHOULD NOT begin one while streams are running. Tens of kilobytes
of aiding and a busy CAN bus want the same connection events.

Encryption is not stated separately here. §10 governs this characteristic as it
governs Control, and for the same reason: a write that changes what the device
does is worth protecting whether it carries a subscription or an orbit.


## 15. OBD-II polling — the `obd` role

Every role before this one observes: GPS listens to satellites, CAN listens
to the bus, IMU listens to the device itself. A device with capability bit 10
(`obd`) **transmits on the vehicle's CAN bus** — it puts J1979 Mode 01
requests on the bus and the ECUs' answers arrive as ordinary CAN frames. That
is a different kind of claim from every other bit in Info, and the bit exists
precisely so the claim is declared rather than inferred: a client, or a
person reading a client's screen, can know whether the dongle in their OBD
port talks to their car. RATIONALE §11 is the full argument.

`obd` requires `can` and `control` (§4.1): responses are delivered on the CAN
stream — through the subscription table and §15.5's fallback rule — and both
opcodes live on Control. Bit 10 is past the eight bits the advertisement carries (§3.3), like
`power` and `gnss_aiding`: polling is something a client does after it
connects, not something it ranks devices by before it does.

One Info capacity describes the role, in the byte freed at offset 20 (§11.2):

- `obd_poll_slots` — the most PIDs one `OBD_POLL_SET` may name.

A device MUST NOT declare more slots than a schedule naming that many PIDs
can occupy in one Control write on the smallest link §2 permits. The worst
case is every PID its own group, which is `3 + count + 2 × count` bytes of
parameters (§15.4.1), so at the 100-byte minimum ATT MTU the ceiling is 30 —
and a device declaring 255 would be advertising a poll set 770 bytes long
that no client could ever send. It MUST be zero when bit 10 is clear and
non-zero when it is set — §4.1's
table carries both columns, because a poll set nothing fits in describes a
role no conforming exchange can use. The rule splits as every content rule
does: an Info that breaks it still decodes, a client MUST NOT use the role
and SHOULD surface the contradiction as a device defect, and a conforming
encoder refuses to produce it — unlike §9.7's power rule, whose violation
spans two payloads and so has no single record an encoder could refuse.

**There is no declared rate.** Offsets 22–23 held `obd_min_interval_ms` in
drafts of this section and are reserved again. A device is plugged into a car
it has never met, so a rate it publishes as *safe* is a guess about a vehicle
it cannot see — the same guess a client makes when it hard-codes an interval,
relocated to the party with even less information. What bounds this role
instead is §15.4's pacing, which is a discipline rather than a number and
holds on every car without knowing any of them.

Because Info is read on every connection and never cached (§4), bit 10
describes **this connection, not the model**. A device with a physical
listen-only switch — a lifted TXD, a sniff-only jumper — MUST clear bit 10
while the switch is set, and MUST answer `unsupported_opcode` to both opcodes
exactly as if it had never implemented them.

### 15.1 What a device may transmit

This section is a complete enumeration. A device MUST NOT put a frame on the
bus except as one of:

1. A probe request (§15.2), in service of an `OBD_INFO` it is answering.
2. A poll request (§15.4), while its poll set is non-empty.

Both are client-initiated, so a conforming device transmits nothing until a
client asks it to, and the first frame it ever transmits is one the client
asked for. There is no keep-alive, no wake-up sequence, and no transmission
the client did not cause.

Every frame either rule permits is a **single frame**: a classic CAN data
frame carrying one Mode 01 request — `[0x02, 0x01, pid]` and padding for a
single PID, or `[1+g, 0x01, pid₁ … pid_g]` and padding for a group of *g*
PIDs (§15.4.1) — on the request identifier of §15.2. A device MUST NOT transmit a
flow-control frame, which is the ISO 15765-2 primitive that continues a
multi-frame exchange: a device that cannot send one cannot be drawn into
another tester's transfer, and cannot complete one of its own (§15.5).

Three bounds hold across the probe and the poll loop together:

- A device MUST NOT have more than one request outstanding on the bus.
- A device MUST NOT transmit until the outstanding request has been answered
  or abandoned, and while a poll set is active MUST NOT transmit two requests
  less than its `interval_ms` apart (§15.4). A probe continues from the same
  last transmission — and since a probe clears the poll set (§15.2), its
  requests and the poll loop's never contend.
- A device MUST NOT retry an unanswered request. A request unanswered
  `OBD_RESPONSE_TIMEOUT_MS` after it was transmitted is abandoned; the poll
  loop simply comes round again (§15.4), and a probe moves on, or falls back
  to the other addressing — a different request, not the same one again
  (§15.2).

A group is **one request** under all three: one outstanding at a time, one
per pass of the schedule, never retried. Grouping therefore does not move any
bound in this section, and cannot — the request frame is padded to eight
bytes whether it names one PID or six.

**`OBD_RESPONSE_TIMEOUT_MS` is 100.** It exists only so a PID nothing answers
cannot stall the schedule, and is deliberately generous rather than tuned:
ISO 15765-4's P2max of 50 ms is the figure a dedicated tester uses, and a
logger that abandons a slow gatewayed ECU has lost the reading a tester would
have waited for. A device MUST NOT make it configurable — a second timing
knob is a second thing for a client to guess wrong.

These bounds are what make the role auditable, and the claim is a discipline
rather than a rate: **a conforming device has at most one diagnostic request
outstanding, waits for its answer, never retries, and transmits nothing a
client did not ask for.** That is checkable by inspection, true on every car,
and — unlike a published interval — needs no guess about a vehicle the device
has never met. What actually bounds the request rate is the car: a device
that waits for an answer cannot outrun the ECU replying to it.

### 15.2 OBD_INFO — the probe

`OBD_INFO` (`0x60`) takes no parameters. It probes the bus and reports what
answered, **measured when the request arrives**: each request probes afresh,
exactly as `GET_POWER` measures afresh (§9.7), so the answer describes the
car the device is plugged into now, not the one it met last week. The probe
is a transmission — the first this specification permits — and a device MUST
NOT begin it before a client asks.

The probe transmits a Mode 01 request for PID `0x00` using **functional
addressing** (ISO 15765-4): the device SHOULD request on 11-bit `0x7DF`
first and, if nothing answers within its collection window, SHOULD repeat
the request with 29-bit functional addressing (`18DB33F1`). It MUST wait at
least 50 ms for responses to each probe request before concluding nothing
answered, MUST NOT retry an unanswered probe request beyond the addressing
fallback above, and MUST NOT continue past PID `0x00` if nothing answered
it. If the union mask read so far claims PID `0x20`, it requests `0x20`; if
that result claims `0x40`, it requests `0x40`; it MUST NOT request a mask
PID the union does not claim. A whole probe is therefore at most a handful
of single frames, and the response is sent only when the probe is complete
— the round trip is slow because it is measuring, which is the §9.5 shape.

The detail of a successful response is one `obd_probe` record followed by
`count` `obd_ecu` entries:

<!-- BEGIN GENERATED: obd_probe -->
*The detail of an OBD_INFO response. What the car answered, measured when asked. Followed by `count` obd_ecu entries.*

Total: **20 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 1 | `u8` | `validity` | bitmask `obd_validity` |
| 1 | 1 | `u8` | `count` | obd_ecu entries following; MUST be 0 exactly when `responded` is clear, and at most 8 (SPEC.md 15.2) |
| 2 | 4 | `u32` | `request_id` | The identifier this device transmits requests on; bits 0-28 arbitration id, b29 extended, bits 30-31 MUST be zero (SPEC.md 15.2); valid when `validity` bit 0 (`responded`) is set |
| 6 | 4 | `u32` | `supported_01_20` | Union over responding ECUs of Mode 01 PID support; bit n = PID 0x01+n, LSB first (SPEC.md 15.3); valid when `validity` bit 0 (`responded`) is set |
| 10 | 4 | `u32` | `supported_21_40` | As supported_01_20 for PIDs 0x21-0x40; bit n = PID 0x21+n (SPEC.md 15.3); valid when `validity` bit 0 (`responded`) is set |
| 14 | 4 | `u32` | `supported_41_60` | As supported_01_20 for PIDs 0x41-0x60; bit n = PID 0x41+n (SPEC.md 15.3); valid when `validity` bit 0 (`responded`) is set |
| 18 | 2 | `u16` | `reserved_18` | Probe metadata; **reserved — MUST be zero** |
<!-- END GENERATED: obd_probe -->

`validity` bits:

<!-- BEGIN GENERATED: bitmask:obd_validity -->
| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `responded` | An OBD-II ECU answered the probe; request_id and the three supported masks are valid |
| 1+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:obd_validity -->

<!-- BEGIN GENERATED: obd_ecu -->
*One ECU that answered the probe, named by the identifier it responds on.*

Total: **4 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 4 | `u32` | `id` | Response identifier; bits 0-28 arbitration id, b29 extended, bits 30-31 MUST be zero. Entries strictly ascending over bits 0-29 (SPEC.md 15.2) |
<!-- END GENERATED: obd_ecu -->

**`responded` means an ECU gave a positive Mode 01 response** — `41 00` and
a mask — to a probe request. Other traffic on the diagnostic identifiers,
including negative responses, does not set it. When `responded` is clear the
probe found no OBD-II ECU: a gatewayed port, an ignition-off bus, a race car
with no J1979 stack. Every gated field is then absent under §1.1, and a
receiver MUST NOT read an empty mask out of a silent car — "no PIDs
supported" and "nothing answered" are different findings.

Every completed probe — answered or not — also **clears the poll set**
(§15.7): the poll set never outlives the probe result it was verified
against, and a probe replaces that result. The uniform rule closes both
failure shapes at once. A silent re-probe would otherwise leave a device
transmitting requests whose answers nothing can deliver — §15.5's fallback
follows the most recent probe, which now reports no identifiers. An
*answered* re-probe would otherwise leave a set verified against the
previous car still transmitting PIDs the new result may not claim — an
unverified transmission wearing an old probe's consent. A client that
re-probes mid-session re-arms with one `OBD_POLL_SET`, which is the
declare-verify-use sequence doing its job.

**`request_id` is the identifier the device's requests go out on** — the one
that actually elicited the responses being reported, never one the device
did not use. Its layout is `can_record`'s: bits 0–28 arbitration identifier,
bit 29 the format bit, so 11- versus 29-bit addressing is derived from the
value rather than stated beside it, and the value drops directly into a
`CAN_SUBSCRIBE` `id`. Each `obd_ecu` entry carries the response identifier
of one answering ECU in the same layout: these are the identifiers §15.5's
fallback delivers on while a poll set is active, and the values a client
uses directly if it chooses to govern that delivery with subscriptions of
its own.

Four rules bind the record's content, and they are content rules in §1.1's
sense — the layout is sound, so a receiver MUST decode the response, MUST
NOT repair it, and SHOULD surface a violation as a device defect:

- `count` MUST be zero if and only if `responded` is clear. A probe that
  says something answered and lists nothing that did — or the reverse — has
  contradicted itself.
- Entries MUST be **strictly ascending** by identifier, compared over bits
  0–29. Ascending order makes the list canonical — two conforming devices
  probing one car produce identical bytes — and strictly so means one ECU
  cannot appear to be two. A duplicate here is decoded and flagged, where a
  duplicate Monitor slot rejects (§13.3): a slot is an address whose
  ambiguity poisons every later update, an entry here is a report.
- `count` MUST NOT exceed 8: ISO 15765-4 caps the responders to a
  functional request at eight.
- A device MUST NOT report through this record any ECU that did not answer
  this probe.

Identifier validity is not a content rule, and it is scoped by the
validity mask. Every entry `id` — and `request_id` when `responded` is set
— MUST satisfy §6.4 with bits 30–31 zero: these fields name identifiers,
not how a frame travelled, and a receiver MUST **reject the whole
response** on a violation, for §6.4's reason exactly — a standard
identifier that does not fit in eleven bits, or one carrying
frame-transmission flags, can only be used by masking it into a different
identifier that looks entirely valid. When `responded` is clear,
`request_id` is absent (§1.1): a receiver MUST NOT read it, so it MUST NOT
reject on it either. Stale bytes behind the cleared bit — whether or not
they happen to form a valid identifier — decode and are surfaced exactly
as the stale-value rule above requires, and a conforming encoder
normalises them to zero. A field a receiver may not read cannot be the
reason it discards the response it may.

A payload whose length is not exactly the record plus `count` entries is
malformed and MUST be rejected whole (§1.1).

### 15.3 Supported PIDs

J1979 makes PIDs `0x00`, `0x20` and `0x40` bitmasks of what an ECU actually
implements, and the three `supported_*` fields carry what the probe read:
one query at connect tells a client exactly which standard channels this
specific car offers, before it polls any of them. That is the same
capability negotiation the rest of this protocol runs on — declare, verify,
use — and it is what makes the role a universal floor rather than
poll-and-see.

**Bit numbering is pinned, and it is not J1979's.** In each field, bit *n*
(of the little-endian `u32`, LSB first) is PID `base + n`: bit 0 of
`supported_01_20` is PID `0x01`, bit 31 is PID `0x20`; bit 0 of
`supported_21_40` is PID `0x21`; bit 0 of `supported_41_60` is PID `0x41`.
J1979's own encoding puts PID `0x01` in the **most** significant bit of the
first data byte, so a device transcribing response bytes into this field
unconverted produces a mask that is plausible, non-zero and wrong for
nearly every car — which is why the order is stated here and pinned by a
conformance vector rather than left to convention.

The fields carry the **union over every ECU that answered**, not a mask per
ECU. The union answers the question the poll set asks — may this PID be
polled at all — and per-ECU attribution is already carried by the response
identifier on every answering frame. What is genuinely not carried is the
per-ECU supported *set*: a client cannot learn from this record which of
two ECUs implements a PID without polling it and watching who answers.
That cost is accepted, and stated here: eight ECUs of per-ECU masks do not
fit a control response at the minimum ATT MTU (§2), and the union is the
part a client acts on.

PIDs above `0x60` are not represented and not pollable (§15.4). The window
`0x01`–`0x60` is exactly the range in which every Mode 01 response fits a
single frame, which is what §15.5's no-reassembly rule stands on; later
windows (`0x60`'s mask names PIDs `0x61`–`0x80`) carry multi-byte responses
that break it, and adding them is a later minor's opcode, not a silent
widening of this one.

### 15.4 OBD_POLL_SET — the poll set

`OBD_POLL_SET` (`0x61`) takes `interval_ms:u16`, `count:u8`, and a schedule
naming `count` PIDs (§15.4.1). It **replaces the whole poll set** — the
request is a complete statement, like a Monitor update (§13.4), so there is
no add, no remove and no read-back: a client knows the set because it
installed it, which is the §9.1 argument that removed `CAN_LIST`. The
response carries no detail.

The schedule is a list of **groups** (§15.4.1), walked in order and wrapping.
Entries are ordered and MAY repeat, and each group carries a minimum interval
(§15.4.2), so both faster-than-the-cycle and an absolute slower rate are
expressible.

**Polling is response-paced.** While the set is non-empty, the device
transmits the next group when *both* hold:

1. the previous request has been **answered** — the first frame received on
   an identifier the most recent probe reported **whose echoed Mode 01 PID is
   one the outstanding request named** — or `OBD_RESPONSE_TIMEOUT_MS` has
   elapsed since it was transmitted, whichever comes first; and
2. at least `interval_ms` has elapsed since the previous transmission.

`interval_ms` is a **minimum spacing, not a period**, and **0 means the client
imposes none** — the device then goes as fast as the car answers, which is
what a dedicated tester does and what the pacing rule makes safe. A non-zero
value is a client throttling the device below the car's own speed, for a
vehicle it has reason to be careful with; it is not a sample period and it is
not a guess about latency the client cannot make.

Zero is admissible here precisely because it is **not** unbounded, which is
the difference from a `periodic` subscription's `arg` (§6.8) and from the
fixed clock this rule replaces: a device that waits for an answer before
transmitting cannot generate traffic faster than the car produces it. The
bound moved from a number to the pacing itself.

**The echo test is what makes "one outstanding" hold on a real car.** A
functionally addressed request is answered by every ECU implementing any PID
in it (§15.4.1), and those ECUs do not answer together — a reply at 10 ms and
another at 15 ms is ordinary. Releasing on any frame at all, the first reply
frees the next request and the second, still answering the *previous* one,
frees the one after that before it has been answered: two requests
outstanding, which §15.1 forbids. A Mode 01 response echoes its PID — §15.5
relies on exactly that, which is why no client needs correlation state — so
comparing the echo against the group just asked separates an answer from a
straggler with no correlation table and no J1979 knowledge beyond reading one
byte.

Two consequences worth stating. A schedule that names the same group twice in
a row cannot distinguish the second request's answer from the first's
straggler, so the echo test does nothing there and the client is back to
`interval_ms` as its control. And a frame on a diagnostic response identifier
that echoes a PID this device did not ask for never releases anything, which
is what keeps another tester's traffic (§15.4.1) from pacing this device even
though the fallback still delivers it.

A request the bus did not answer is abandoned at `OBD_RESPONSE_TIMEOUT_MS`
(§15.1); the loop does not stall, retry, or reorder, and comes round again on
the next pass. The client sees the gap as the absence of a response frame,
which is the truth.

Refusals, checked in this order after the §9 capability gate:

1. A payload the schedule parse does not consume exactly, or that names other
   than `count` PIDs, is `bad_params` (§15.4.1 gives the layout).
2. `count` above `obd_poll_slots` is `table_full` — the capacity was in
   Info, so the answer is a fact the client could have read, §9.6's
   argument for `rate_exceeded`.
3. With `count` 0, `interval_ms` MUST be 0, and the empty set **stops
   polling**: it is how a client turns transmit off, it is accepted
   whatever the probe state, and it is not an error.
4. A PID outside `0x01`–`0x60`, or one whose bit in the most recent probe's
   union is clear, is `bad_params`. With no probe completed this
   connection, nothing is pollable — so a device MUST answer `bad_params`
   to any non-empty poll set before `OBD_INFO` has been answered, and the
   sequence declare (bit 10), verify (`OBD_INFO`), use (`OBD_POLL_SET`) is
   structural rather than convention.
5. A group longer than **6 PIDs** is `bad_params` (§15.4.1). A minimum
   interval of 0 is legal and means "no minimum" (§15.4.2) — unlike a ratio,
   a floor of zero subtracts nothing rather than naming a group that never
   transmits.

A replacement **preserves the cursor, and preserves each group's last
transmission for any group the new schedule names again** — a group is the
same group if it names the same PIDs in the same order. Neither is reset,
for the same reason the transmission spacing is not: re-issuing a poll set is
the only way to change a PID, so a client does it routinely, and a device
that started the schedule over each time would starve the tail of a set
replaced faster than it cycles, while one that restarted the minimum
intervals of §15.4.2 would let a client defeat its own rate limits by
reinstalling. Groups the new schedule does not name lose their history with
the set that held them.

A refused request MUST leave the installed poll set unchanged. The opcode
is idempotent — replacing a set with itself is the same set — which is its
§9.4 retry-safety. The change takes effect promptly: after an `ok`, at most
one further request MAY go out under the old set.

An accepted, non-empty poll set is the whole of what a client must do to
receive the answers: §15.5 delivers them on the probe's reported response
identifiers with no subscription required. Subscriptions remain what they
are everywhere — the client's choice of broadcast traffic, and its means
of governing the diagnostic identifiers more tightly than `every_frame`.

### 15.4.1 PID grouping and the schedule layout

A schedule entry is a **group**: one or more PIDs asked in a single Mode 01
request. Bit 7 of a PID byte is the `more` flag — a byte with bit 7 set is
grouped with the byte that follows, and a group is a maximal such run
terminated by the first byte with bit 7 clear. PIDs are `0x01`–`0x60`, so
bit 7 is free. **Each group's terminating byte is followed by a `u16`
minimum interval**, little-endian, in milliseconds (§15.4.2).

So the schedule is parsed in one pass: read PID bytes until one without
`more`, read the two interval bytes that follow it, repeat until the payload
is consumed. `count` counts PID bytes only, so `obd_poll_slots` means what it
always meant, and the payload is `3 + count + 2 × (number of groups)` bytes.

`[0x8C, 0x0D, 0x00, 0x00, 0x85, 0x8F, 0x04, 0xF4, 0x01]` is two groups:
`(0C, 0D)` with no minimum, and `(05, 0F, 04)` no oftener than every 500 ms.

The device transmits one Mode 01 request per group —
`[1+g, 0x01, pid₁ … pid_g]`, padded to eight bytes exactly as a single-PID
request is. **Grouping is free on the bus**: a six-PID request occupies the
same eight bytes a one-PID request occupies (§15.1), and the response side
gets strictly smaller — one frame per ECU per group where there was one per
ECU per PID.

A group longer than 6 PIDs is `bad_params`: seven would not fit the request
frame, and that is the only bound on grouping the device checks.

**The device does not check response sizes, and MUST NOT.** Whether a group's
answer fits a single frame is arithmetic over J1979 response lengths, and
those tables live in the client (§15.5). A group whose response exceeds seven
bytes is answered with a first frame, and §15.5 already governs it with no
special case: the subscription table decides first, and a first frame the
table does not match, on a probe-reported response identifier, while the poll
set is non-empty, **is forwarded by §15.5's fallback like any other frame
there**. The client therefore *receives* the first frame — it is not silently
dropped — and receives nothing further, because the transfer it opens dies
for want of a flow control this device will not send. The device reassembles
nothing and transmits no flow control, exactly as §15.1 requires of it.

That the failure is delivered rather than swallowed is the point: a client
that oversized a group sees a frame whose PCI says a multi-frame answer is
coming, learns immediately that its own arithmetic was wrong, and regroups.

A client sizing a group counts six bytes of budget: a single-frame response
is `41` plus one `pid`+`data` pair per PID, and no Mode 01 response in
`0x01`–`0x60` exceeds four data bytes. Three PIDs per group is therefore the
practical ceiling and two is common — well under the six that bounds the
request.

**A group is functionally addressed like every other request**, so every ECU
implementing *any* PID in it answers, each with the subset it implements —
and each such response is smaller than the group's worst case, so a client
sizing against "one ECU answers everything" is sizing conservatively. The
probe reports the *union* of the ECUs' masks and not per-ECU masks (§15.3),
so a client wanting per-ECU attribution before it groups obtains it the way
§15.3 already says: poll the PIDs singly, watch which response identifiers
answer, then install the grouped schedule.

One consequence of pacing belongs here: "the answer arrived" means *an*
answer on a diagnostic response identifier, and §15.5 notes those carry other
testers' answers too — a splitter with a second dongle in it, or a
vehicle-internal module making its own requests. A device can therefore be
paced by traffic it did not cause and run faster than it otherwise would.
The effect is bounded, because the other requester keeps its own schedule and
does not accelerate in response, and a client that cares sets a non-zero
`interval_ms`.

### 15.4.2 Per-group minimum intervals

Every group carries a `u16` **minimum interval in milliseconds**. A group is
transmitted only when at least that long has passed since *that group* last
transmitted; **0 means no minimum**, and the group runs at whatever pace the
schedule and the car allow.

This is a rate and not a ratio, and the difference is load-bearing under
§15.4's pacing. A ratio — one pass in *d* — names a different rate on every
car, because the cycle time is the car's response latency and not a constant.
The same schedule that gives 2 Hz on one vehicle gives 6 Hz on a faster one
and drifts inside a single session as the bus does. A client wanting a
channel at 2 Hz would have to measure the achieved cycle and reissue the poll
set whenever it moved, which is the control loop §15.4 exists to abolish. An
interval holds its rate whatever the car does.

The range matters too: a `u8` ratio bottoms out around 0.3 Hz on a car
answering in 5 ms, so a genuinely slow channel — ambient temperature, fuel
level — could not be expressed at all. `u16` milliseconds reaches 65.5 s.

Minimum intervals exist because repetition is one-way. A client can already
make a PID faster than the cycle by naming it twice, and before this
subsection it could not make one slower than once per cycle at all:
everything in the schedule was sampled at the cycle rate whether it needed to
be or not, and the only currency for buying a ratio was `obd_poll_slots`. A
client wanting one channel slower had to pay in channels it could no longer
read.

**A minimum interval cannot increase the request rate.** The device still
transmits at most one request per pass of the schedule, and a minimum only
ever causes one to be skipped, so §15.1's bounds are untouched and there is
nothing to arbitrate — there is still one schedule and one cursor. That
asymmetry is why per-group minimums are admissible where RATIONALE §11.6
refused per-PID *rates*: N independent rates make the bus load a sum only the
client knows, while N ceilings can only ever subtract from a load already
bounded by the pacing.

A skipped group advances the cursor without transmitting. A moment at which
no group is due is a moment the device does not transmit, which is the client
having asked for less traffic and got it.

### 15.5 Delivery — responses are ordinary frames

There is no OBD record type and no OBD stream. An ECU's answer is a CAN
frame on its response identifier, and it reaches the client exactly as any
other frame does: through the CAN characteristic, inside batches, with
bus-arrival timestamps, subject to `seq`, `dropped` and shedding (§6).
Which frames reach it is decided by the subscription table plus exactly one
rule:

> **While the poll set is non-empty, a frame whose identifier (over bits
> 0–29) equals an entry id reported by the most recent probe, and which
> matches no installed subscription, MUST be forwarded, as if by an
> `every_frame` subscription.**

A client that sets a poll therefore receives the answers, with no further
ceremony: `OBD_POLL_SET` is the one instruction, and a protocol that let a
device transmit on a car while discarding the replies as unsubscribed would
have made its worst state the price of forgetting a call. The rule is a
**fallback, not an entry in the table**, and each half of that is load-
bearing:

- **Frames the table matches are governed by the table**, exactly as if the
  OBD role did not exist: §9.1 and §9.2 apply unchanged, and this rule
  never sees those frames. A client that wants the responses rate-limited
  installs an ordinary `periodic` subscription on the response identifiers,
  and it governs. The fallback yields to anything the client says.
- **It is not subscription state.** It consumes no slot against
  `can_subscription_slots`, `CAN_UNSUBSCRIBE` cannot name it — an attempt
  is `unknown_subscription`, as for anything the client never installed —
  and it needs no per-identifier mode state, because `every_frame` has
  none. It exists exactly while the poll set is non-empty and the probe
  result stands, and dies with them (§15.7).

Frames the fallback forwards are accepted frames like any other: they are
batched, timestamped, counted by `seq`, and shed under load with the loss
reported in `dropped` (§6.3, §8.3).

The identifiers are the probe's, so the fallback delivers **what the bus
says on the diagnostic response identifiers**, not only what this device
asked for: another tester's answers on `0x7E8` arrive too, exactly as they
would through an explicit subscription. That is deliberate — the frames are
self-describing, as the next paragraph makes them, and a logger that
suppressed them would be hiding real traffic — and it is the stated cost: a
client is delivered
frames on identifiers it never named. A probe's own mask responses ride the
same rule: delivered through a matching subscription, or through the
fallback when a poll set is already active, and otherwise not at all — the
client gets their content in the `OBD_INFO` detail either way.

**The CAN stream carries what the device hears, never what it says.** A
device MUST NOT emit a `can_record` for a frame it transmitted itself,
whatever the subscriptions match: a `can_record`'s timestamp is a
bus-arrival measurement of a received frame (§6.1), and a stream in which
the device's own `0x7DF` requests appear is indistinguishable from a bus
carrying a second diagnostic tool. The device's transmissions are disclosed
by bit 10 and observed through the polling flag (§15.6), not reconstructed
from the stream.

Mode 01 responses are self-describing — `41`, the PID, then the data — so a
client needs no request/response correlation state: the frame says which
PID it answers, whichever device asked and however long ago. The decode —
`41 0C 1A F8` into 1726 rpm — belongs in the client, which is where the
formula tables live and are updated; this device's job is the transaction,
not the arithmetic.

The device performs **no ISO-TP reassembly** and, per §15.1, transmits no
flow control. Within `0x01`–`0x60` every response to a one-PID request is a
single frame by J1979's own sizes (§15.3), so there is nothing to
reassemble; a first frame arriving anyway — another tester's transfer, an
out-of-spec ECU, a group a client oversized (§15.4.1) — is an ordinary
frame, governed by the table if it matches one and by the fallback above if
it does not, and dropped only where neither reaches it; the exchange it
opens dies for want of a flow control this device will not send.

### 15.6 The polling flag

`can_header.flags` bit 1 (`polling`) MUST be set on every CAN batch flushed
while the device's poll set is non-empty, and MUST be clear on every batch
flushed while it is empty. Bit 10 in Info says this device *can* transmit;
this bit says it currently *is* — continuously, on the stream any client of
the CAN role is already reading, at the cost of one reserved bit. It is
what makes §15.7 observable: after a stop, the next batch says whether the
transmitter actually stopped, and whoever is watching the stream — not
necessarily whoever sent `OBD_POLL_SET` — can tell a transmitting dongle
from a pure sniffer.

The probe does not set the flag: it is a bounded handful of frames inside
one control round trip, disclosed by the `OBD_INFO` exchange itself, and a
flag that flickered for it would blink faster than a batch flush can
report.

### 15.7 What stops the transmitter

Transmit MUST NOT outlive the client that asked for it. The poll set is
cleared, and polling therefore stops, on every one of:

- **The empty poll set** — `OBD_POLL_SET` with `count` 0, the explicit
  stop, always accepted (§15.4).
- **`CAN_RESET`** — which clears the poll set along with the subscription
  table (§9). The CAN role has one reset, and it resets everything the
  role does: after it, the device neither transmits nor forwards, and a
  client rebuilds both halves the way §9.1 already makes the strategy.
- **Link loss** — exactly as the subscription table is cleared (§9.1). A
  reconnecting client finds a known, silent state and reprograms
  everything, which §9.1 already makes the strategy.
- **A completed probe** — every `OBD_INFO` clears the poll set along with
  the probe result it replaces (§15.2), answered or not: the set never
  outlives the result it was verified against, so a re-probe that finds a
  different car — or none — cannot leave yesterday's PIDs transmitting.
- **Bus-off** — a device whose controller reaches bus-off MUST clear the
  poll set and MUST NOT resume transmitting on its own; recovery is a new
  `OBD_POLL_SET` from the client. `responded` reporting and the polling
  flag then tell the truth about a bus the device can no longer speak on.

A device MUST NOT re-arm polling itself in any of these cases, and MUST
NOT persist a poll set across connections. There is no state in which a
VTP/1 device transmits and no connected client asked it to.

§15.5's fallback delivery ends with the poll set, in every one of these
cases: it exists so a polling client receives the answers, and once nothing
is being asked, nothing is delivered that the subscription table does not
name. Stopping the transmitter MUST NOT strand what it already accepted:
frames batched for delivery when the poll set clears are flushed, or
discarded and counted in `dropped` (§8.3) — never held to surface on a
later subscription carrying a `t_base` from a period the device had
declared itself silent. What survives which edge follows what each thing is — the probe
result is a fact about the car, so `CAN_RESET` leaves it standing and a new
poll set re-arms without a second probe; the link's death clears
everything, and the next connection starts at declare, verify, use.

### 15.8 Sharing the bus

An OBD port is one bus, and this device may not be the only tester on it. A
scan tool, a dealer gateway, an insurance dongle — any of them may be
polling while this device is. The rules are fixed, not adaptive:

- A device MUST NOT attempt to detect other diagnostic testers, and MUST
  NOT suspend, delay or resume polling on any inference about one. Every
  heuristic that stops polling on a suspicion stops it on a coincidence,
  and a device whose transmit behaviour varies with unmodelled traffic is
  a device whose behaviour cannot be stated. What it does is what §15.1
  says, always.
- A device MUST NOT shorten its spacing, reorder its schedule, or add
  requests in response to anything it hears. Bus contention costs it
  answers, never restraint.
- A response the device did not solicit — another tester's answer arriving
  on `0x7E8` — is an ordinary frame, delivered exactly as §15.5 delivers
  any frame on those identifiers: through a matching subscription, through
  the fallback while a poll set is active, and otherwise not at all. The
  device MUST NOT suppress, deduplicate or re-attribute it. Mode 01 responses are self-describing (§15.5), so a
  client sees a true record of what the bus carried; that record showing
  more answers than this device's questions is the truth about a shared
  bus, not a defect.

### 15.9 What the OBD role is not

The role is deliberately the floor, not the ceiling. Out of scope, each a
candidate for a later minor's opcodes (§11.3) rather than a silent widening
of these two:

- **Mode 22 manufacturer DIDs** — where brake pressure and steering angle
  live on gatewayed cars. It needs ISO-TP reassembly and flow control,
  which §15.1 forbids; its DID space is manufacturer-private, so there is
  no supported-DID mask to verify against and the declare-verify-use shape
  collapses; and it points requests at ECU behaviour nobody has published.
- **Diagnostic trouble codes** (Modes 03, 04, 07) — reading DTCs is
  multi-frame, and *clearing* them (Mode 04) writes to the vehicle, a
  categorically different act from asking a mandated question.
- **Modes 02, 06, 08, 09, 0A** — freeze frames, test results, actuator
  control, vehicle information. Actuator control in particular is exactly
  what this role's bounds exist to make unexpressible.
- **PIDs above `0x60`** — §15.3 states the single-frame boundary they
  break.
- **Decoding** — no formula tables in firmware, no scaled values on the
  wire. The client owns the arithmetic (§15.5).

A device wanting any of these is a device wanting a new capability bit,
whose declaration a client can refuse to use — which is this role's own
shape, applied to whatever comes next.

---

## Appendix A — Reserved space

Generated from `schema/vtp1.yaml`, so it cannot disagree with the bitmask and
record tables above.

<!-- BEGIN GENERATED: reserved_space -->
| Location | Reserved | Purpose |
| --- | --- | --- |
| `gps_fix.validity` | bits 12–31 | Validity for fields added in a later minor |
| `gps_fix.fix_flags` | bits 5–7 | Additional solution-quality flags |
| `info.capabilities` | bit 7, bits 11–31 | Roles and features added in a later minor |
| `can_header.flags` | bits 2–7 | Additional batch-level CAN status |
| `imu_header.flags` | bits 3–7 | Additional sensor groups |
| `info.clock_flags` | bits 2–7 | Additional clock properties |
| `monitor_value.validity` | bits 1–7 | Validity for values added in a later minor |
| `power_state.validity` | bits 2–7 | Validity for power fields added in a later minor |
| `gnss_aid_caps.validity` | bits 1–7 | Validity for aiding capabilities added in a later minor |
| `aid_commit_result.validity` | bits 1–7 | Validity for commit results added in a later minor |
| `obd_probe.validity` | bits 1–7 | Validity for probe results added in a later minor |
| `info.reserved_22` | 2 bytes | Was obd_min_interval_ms; withdrawn with the fixed poll clock (SPEC.md 15.4) |
| `can_header.reserved` | 2 bytes | Low byte earmarked for a bus index (SPEC.md 6.9); high byte unassigned |
| `imu_header.reserved` | 2 bytes | In-band IMU metadata |
| `monitor_declaration.reserved` | 1 byte | Declaration metadata |
| `monitor_header.reserved` | 1 byte | Update metadata |
| `power_state.reserved` | 1 byte | Power metadata |
| `gnss_aid_caps.reserved_2` | 2 bytes | Was aid_flags; aiding metadata |
| `aid_begin_result.reserved_3` | 1 byte | Transfer metadata |
| `obd_probe.reserved_18` | 2 bytes | Probe metadata |
| Extension types | `0x80`–`0xFF` | Vendor-private; this specification MUST NOT assign them (§5.5) |
<!-- END GENERATED: reserved_space -->

---

## Appendix B — A minimal device

*Non-normative. Every rule below is stated normatively elsewhere; this is the
shortest path through them.*

The smallest conforming VTP/1 device is a GPS-only logger, and it is small:

1. **Advertise** the VTP/1 service UUID (§3.3).
2. **Expose the fixed attribute table** (§4.1): all seven characteristics.
   Five are inert — `can` and `imu`'s CCCDs exist and accept writes; `control`
   and `monitor_values` reject every write with an ATT error; `aiding`
   discards writes silently. Inert code is a handful of lines.
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
