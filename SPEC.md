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
and stays there. `seq` wraps, `dropped` saturates, and §8 says why each does
what it does.

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

A fix is never batched, and a CAN frame never travels without a batch header
even when it is the only frame in the notification. The asymmetry follows the
rates rather than a preference: a GNSS receiver produces at most a few tens of
complete solutions a second, while a busy chassis bus produces some four
thousand frames a second, which no one-frame-per-notification framing can carry
(RATIONALE §2.4). The IMU is batched for the same reason as CAN but timestamped
differently — its samples are evenly spaced, so one interval in the header
describes all of them (§7).

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
negotiated maximum when batching (§6, §7).

The Device Information Service is a SHOULD, specified in §3.4.

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
revision.

Nothing in VTP/1 reads it, which is the point: it is where every generic
Bluetooth tool already looks, so it is what answers "which firmware is on the
logger that is misbehaving" without the asker needing to know anything about
this protocol. Info (§4) is the protocol's own self-description and remains the
only thing a client parses; the two do not overlap and neither substitutes for
the other.

It is a SHOULD rather than a MUST because it carries no protocol meaning: a
device that omits it is fully usable, and requiring it would add a conformance
surface that no client behaviour depends on.

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
| 20 | 1 | `u8` | `reserved_20` | Was can_max_payload; derived from the capability bits since (SPEC.md 4.2); **reserved — MUST be zero** |
| 21 | 1 | `u8` | `clock_flags` | bitmask `clock_flags` |
| 22 | 2 | `u16` | `max_notify_bytes` | `bytes`; Largest notification this device will ever send; a fixed device ceiling, NOT the negotiated ATT payload (SPEC.md 4.2) |
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
| 7 | `on_change_subscriptions` | **Requires `can`.** |
| 8 | `gnss_aiding` | Client supplies orbit data to the device's receiver (§14) **Requires `gps`, `control`.** |
| 9+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:capabilities -->

A client MUST read this characteristic on every connection and MUST NOT cache it
across connections. A DIY device is reflashed by its owner: its minor version,
capability set and rate ceilings can all change while its Bluetooth address does
not.

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
last column says exactly what inert means for it.

<!-- BEGIN GENERATED: profile:attributes -->
| Characteristic | Capability | Properties | CCCD | Written by | Read by | When the capability bit is clear |
| --- | --- | --- | --- | --- | --- | --- |
| `info` | — always present | `read` | — | device | client | never; Info is always meaningful |
| `gps` | bit 0 (`gps`) | `notify` | always present; client enables it for a set bit | device | client | the CCCD exists; no notification is ever sent on it |
| `can` | bit 1 (`can`) | `notify` | always present; client enables it for a set bit | device | client | the CCCD exists; no notification is ever sent on it |
| `imu` | bit 2 (`imu`) | `notify` | always present; client enables it for a set bit | device | client | the CCCD exists; no notification is ever sent on it |
| `control` | bit 4 (`control`) | `write`, `indicate` (write with-response) | always present; client enables it for a set bit | client | client | the CCCD exists; writes are rejected with an ATT error and no opcode is parsed |
| `monitor_values` | bit 3 (`monitor`) | `write` (write with-response) | — | client | device | writes are rejected with an ATT error and change nothing |
| `aiding` | bit 8 (`gnss_aiding`) | `write-without-response` (write without-response) | — | client | device | writes are silently discarded; a client cannot reach this characteristic legitimately without GNSS_AID_BEGIN having answered ok first |
<!-- END GENERATED: profile:attributes -->

A device MUST NOT add a characteristic to the VTP/1 service beyond these, and
MUST expose at least the properties listed. It MAY expose more — making `gps`
readable is a common convenience — and a client MUST NOT rely on any property
the table does not list, so it MUST NOT read `gps` in place of subscribing.

**An inert characteristic costs its implementer almost nothing**, and that is
the point of requiring one. A device without the `control` bit exposes the
Control characteristic and **rejects every write with an ATT error**; it does
not parse opcodes, does not implement indications, and never answers
`unsupported_opcode`, because answering requires the response path it does not
have. The same goes for `monitor_values`.

`aiding` is the one exception, and only because ATT gives it no choice: a Write
Command carries no response of any kind, so an inert `aiding` discards silently
and §14 puts every refusal a client needs on Control instead.

A GPS-only build is a service declaration, five inert attributes and one notify
path.

**A CCCD is an attribute, so it is part of the fixed table too.** Every
notifying and indicating characteristic above carries its Client Characteristic
Configuration descriptor whatever the capability bit says, for exactly the
reason the characteristics themselves are always present: removing one changes
the attribute table a central has cached.

A client enables the CCCD for a role whose bit is set, and leaves the others
alone. A device MUST accept a CCCD write on an inert stream — it costs a
two-byte descriptor and a stored value nothing reads — and then simply never
notifies, which is what "inert" already means. A device MUST NOT reject a CCCD
write on the grounds that the capability is absent.

The alternative — omitting the characteristics a device does not implement —
fails for a reason that has nothing to do with elegance. Central stacks
**cache the attribute table** across connections, and
several cache it across reboots of the phone. A device whose table changes
between connections, because a capability was switched off in firmware or
because a build shipped without a role, hands the client a stale handle. The
client then reads or writes the wrong attribute rather than discovering a
missing one, which is precisely the plausible-wrong-value failure §1.1 exists
to prevent. A fixed table cannot produce it: discovery answers "is this
VTP/1?", Info answers "what does it do?", and neither half-answers the other.

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
| 7 | `on_change_subscriptions` | bit 1 (`can`) | — |
| 8 | `gnss_aiding` | bit 0 (`gps`), bit 4 (`control`) | — |
<!-- END GENERATED: profile:capabilities -->

**The largest CAN payload follows from the bits and is not a field.** A client
computes it:

| `can` | `can_fd` | Largest payload |
| --- | --- | --- |
| clear | clear | 0 — the device has no CAN |
| set | clear | 8 — Classic CAN |
| set | set | 64 — CAN FD |

`set`/`set` is the only combination `can_fd` permits, because §4.1 makes
`can_fd` require `can`. Info carried a `can_max_payload` byte for this until it
turned out that every value it could hold was already decided here — so two
statements of one fact existed, an implementation could publish them
disagreeing, and neither reference checked. Byte 20 of Info is now reserved.

A device MUST NOT set a capability bit without also setting every bit the
second column names. A client MUST treat an Info whose capabilities break an
implication as non-conforming, exactly as it treats a `protocol_major`
mismatch, and MUST NOT guess which half was meant.

`can` and `monitor` require `control` because neither role is reachable
without it. A CAN device forwards nothing until a client has sent
`CAN_SUBSCRIBE` (§9.2), and a Monitor device cannot say which channels it wants
except through `MONITOR_LIST` (§13.3). A device advertising either without
Control is advertising a role no client can use.

`can_fd`, `masked_subscriptions` and `on_change_subscriptions` require `can`
for the same reason: each qualifies how CAN subscriptions behave, and qualifies
nothing at all on a device with no CAN.

**Each of the three says what a device does when it is clear**, because a
capability bit that only says "supported" leaves the other half to the reader
and a client cannot plan around it:

| Bit | Set | Clear |
| --- | --- | --- |
| `can_fd` | The device MAY emit records with the FD bit set, carrying up to 64 payload bytes | The device MUST NOT emit a record with the FD bit set, and no record carries more than 8 payload bytes |
| `masked_subscriptions` | `CAN_SUBSCRIBE_MASK` is accepted | `CAN_SUBSCRIBE_MASK` MUST answer `unsupported_opcode` |
| `on_change_subscriptions` | `on_change` is accepted as a subscription mode | A subscription naming `on_change` MUST be refused with `bad_params` |

`CAN_SUBSCRIBE` is unaffected by `masked_subscriptions`: §9.2 defines it as
`CAN_SUBSCRIBE_MASK` with a full mask, but it is a separate opcode and every
CAN device implements it. The capability governs whether a client may choose
the mask, not whether masking exists.

A device without `on_change_subscriptions` refuses the mode rather than
silently substituting `every_frame`. Quietly forwarding every frame where a
client asked for changes only is the difference between a channel that updates
on an event and one that floods, and the client would have no way to find out.

**Capacity fields follow the bit.** Every field in the third column MUST be
zero when its capability bit is clear. This is what makes "a capacity of zero
means none" checkable rather than a promise: a device reporting
`can_max_frames_per_s` of 4000 with the `can` bit clear has published a
capability it does not have, and a client sizing a buffer from it has been told
something false.

**Direction.** The "written by" and "read by" columns say which end produces
each record. Two of the six run client-to-device — `control` requests and
`monitor_values` — and everything else runs device-to-client. A conformance
role covers both directions of the records it names (`conformance/README.md`).

### 4.2 `max_notify_bytes` is a device ceiling

`max_notify_bytes` is the largest notification the **device** will ever send,
on any link. It is a property of the device, fixed for the connection, and it
is **not** the negotiated ATT payload.

Those two readings look interchangeable and are not, because of when each
becomes available. A client reads Info as its first act after connecting. A
peripheral commonly does not learn the negotiated maximum until a central
subscribes, which is strictly later — on CoreBluetooth the only object that
knows it arrives in the subscribe callback. A `max_notify_bytes` defined as the
negotiated value is therefore a field whose correct value does not exist yet at
the only moment anyone reads it, and a device answering it can only report the
previous link's number or a configured guess.

Defined as a ceiling, it has an answer at every instant: a client sizes its
receive buffer once, from a number that cannot change underneath it, and a
device that would exceed the ceiling on a generous link simply does not.

A device MUST NOT send a notification larger than the `max_notify_bytes` it
published, and MUST size batches to the negotiated ATT payload when that is
**smaller** — the ceiling bounds the device, the link bounds the notification,
and the smaller of the two always wins.

A client that wants the negotiated value asks for it with `GET_LINK_PARAMS`
(§9.1), which is a request made after subscribing rather than a value read
before it, and which reports `att_mtu` with a validity bit that is clear when
the device genuinely cannot see it.

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

A receiver that reads those two negative rows as unsigned gets 395.7099296° and
307.1019296° — numbers no coordinate can hold, and the reason the southern and
western hemispheres are a sign rather than a flag somebody has to remember to
apply.

**Ranges.** When its validity bit is set, `lat` MUST lie within ±90°, `lon`
within ±180°, and `head_mot` within 0° to 360° exclusive of 360. A receiver
MUST reject a fix that breaks any of these.

Rejected rather than clamped, under §1.1. A latitude of 91° is not a place a
clamp could move it closer to; it is a field that has been corrupted, and every
other field in the same record came from the same bytes. Clamping to 90° would
put the vehicle at the north pole and let the client draw it there.

**Datum.** `lat`, `lon` and `alt_ellipsoid` MUST be referenced to WGS-84. A
position is plotted against a map the device knows nothing about, and a
coordinate in an unstated datum is metres of silent error: a plausible wrong
value of exactly the kind §1.1 exists to prevent.

`alt_msl` is height above mean sea level as the receiver computes it, from
whatever geoid model it carries; this specification does not name one. The
difference between the two altitude fields is the geoid separation at that
position, and a client needing to know which model produced it needs the
receiver's documentation rather than a protocol field.

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

`rtk_float` and `rtk_fixed` are **mutually exclusive**: a carrier-phase
solution has either resolved its integer ambiguities or it has not. Either RTK
bit also implies `differential`, since an RTK solution is by definition a
differentially corrected one.

A device MUST NOT set both RTK bits, and MUST set `differential` whenever it
sets either. A receiver MUST reject a fix that breaks either rule, as it
rejects any other self-contradictory record (§1.1) — the flags and the position
came from the same bytes, and the natural client reading of both-RTK-set is
"fixed wins", which would upgrade a device's accuracy claim on the strength of
a bug.

### 5.4 Reference frames and derived quantities

The velocity triple is a local north-east-down frame at the reported position:
`vel_n` toward true north, `vel_e` toward true east, and `vel_d` positive
downward, so a climbing vehicle reports a negative `vel_d`.

Ground speed is `hypot(vel_n, vel_e)` and is exact. A device MUST NOT report a
separately computed scalar ground speed; the velocity vector is the only
representation.

`head_mot` is measured clockwise from **true** north — never magnetic north,
and never a grid bearing. VTP/1 carries no magnetic declination and no magnetic
heading, so a client that wants either derives it from the position with a
model of its own.

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

An earlier draft stated the epoch as an unconditional requirement *and* gave
the fallback, which are two rules that cannot both hold. The conditional form
is the one that was meant: the flag exists precisely because the requirement
cannot be unconditional.

The two are far apart. A GNSS receiver computes a solution for a specific
instant and delivers it over a serial link some tens to some hundreds of
milliseconds later, depending on the receiver, its output rate and how busy the
link is. A device that stamps delivery therefore reports a position that was
true at one time with a timestamp naming another, and every GPS sample is late
against CAN and IMU by that latency. Cross-channel alignment is what §8.1's
single shared clock exists to provide, and a systematic offset on one of the
three channels removes it while leaving every number looking entirely
plausible.

Whether the receiver exposes the epoch — through a timing message, or a PPS
edge the device can latch — is a property of the hardware, not of this
protocol, and a specification that required what some hardware cannot do would
be met by devices setting a timestamp they cannot justify.

The flag exists so a client can tell which it has. §1.1 applies exactly as it
does to a validity bit: the honest answer to "when was this true?" is either a
measured epoch or an admission that the device does not know, and never a
delivery time presented as a measurement. A client aligning GPS against CAN
below the tens of milliseconds SHOULD check this bit, and SHOULD NOT assume the
offset is constant when it is clear — receiver latency varies with the number
of satellites and the solution type.

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
the time the notification was queued or sent.

`t_base` is an absolute reading of that clock. It is not an offset from
anything: it does not start at zero when the connection opens, and it does not
accumulate from batch to batch. Every notification carries its own `t_base`,
and every `dt` is measured from the `t_base` in the same notification — not
from the record before it, and not from the previous batch. **Record 0's `dt`
MUST be zero**, `t_base` being its arrival time by definition, and a receiver
MUST reject a batch whose first record says otherwise.

The rule follows from `t_base`'s own definition, so a non-zero first `dt` means
the sender and the receiver disagree about what `t_base` is — and a receiver
that accepts it has no way to tell which of the two readings it should trust.
Four vectors in this repository carried non-zero first offsets while this
sentence said they could not, and both reference decoders accepted them, which
is how a definitional rule survives as prose without ever becoming a rule.

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
no records. `t_base` is defined as the bus-arrival time of record 0, so a batch
with no record 0 carries a timestamp naming a frame that does not exist — the
same reason §7 forbids an empty IMU batch.

A quiet bus is reported by sending nothing. There is no empty-batch heartbeat
and none is needed: a client learns the device is alive from any of the streams
it subscribed to, and a device with a genuinely silent bus and nothing else to
send has nothing to say. `dropped` and the shedding flag ride on the next batch
that has content, which §8.3 already permits — they are a best-effort
diagnostic, not a delivery obligation.

A receiver MUST reject a notification whose length does not exactly match the
header plus `count` complete records.

### 6.3 Loss

`dropped` is defined in §8.3. A device MUST report discards there rather than
silently omitting frames, and MUST NOT count a frame that matched no
subscription, or that the governing subscription's mode did not select (§6.8) —
neither of those was ever accepted. A client SHOULD surface a non-zero
`dropped` to the user.

`flags` bit 0 indicates the device is actively **shedding load**: discarding
frames it accepted and cannot forward, because the bus is producing them faster
than the device can pack them or the link can carry them. The bit reports that
the condition is current; `dropped` counts what it has cost since the previous
notification. A device sheds rather than stalls because a bus does not wait,
and §9.4 is why the condition is reachable at all — an `every_frame` or
`on_change` subscription cannot be refused on rate grounds, because the load it
will produce is not knowable when it is installed.

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
sizes are frozen for the life of a major version (§11.4), which means adding
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

A masked subscription may match several identifiers, and each of them is a
different signal from a different sender. A device MUST therefore keep the
state these modes need — the `periodic` interval, the `every_nth` counter, the
`on_change` comparison payload, and "the first matching frame" — **per matching
identifier**, not per subscription.

Sharing that state across a mask makes each mode wrong in a different way, and
all three failures look like a quiet bus rather than a bug. A shared `periodic`
interval lets whichever identifier arrives first consume it, so the others are
suppressed and a client sees one signal out of a group it subscribed to as a
group. A shared `every_nth` counter forwards every Nth *frame of the group*
rather than every Nth frame of each identifier, so which signal a client
receives depends on the interleaving of the bus. A shared `on_change`
comparison compares the payload of one identifier against the payload of
another, where "changed" carries no meaning at all.

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

### 6.10 Payload length

`len` is the number of payload bytes that follow the record, and the lengths a
CAN bus can actually carry are not contiguous.

A **Classic** frame — bit 30 clear — carries zero to eight bytes. A receiver
MUST reject the whole batch if `len` exceeds 8.

A **CAN FD** frame — bit 30 set — carries a length its four-bit DLC can express,
which above eight is a fixed ladder:

    0  1  2  3  4  5  6  7  8  12  16  20  24  32  48  64

A receiver MUST reject the whole batch if a CAN FD `len` is not one of these.
Nine, ten and eleven are not short payloads; they are lengths no CAN FD
controller can produce.

Rejection rather than repair follows §1.1. A `len` off the ladder means the
reader and the writer disagree about where this record ends, so every byte
after it is suspect — including the identifier of the next frame, which will
still look like a valid identifier. Rounding the length up to the next rung
would produce a frame with plausible padding and a correct-looking identifier,
which is precisely the outcome the batch-level reject exists to avoid.

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

**`count` MUST NOT be zero.** `t_base` is defined as the acquisition time of
sample 0, so a batch with no sample 0 has a timestamp naming a sample that does
not exist. A device with nothing to report sends nothing; there is no empty-IMU
notification and a receiver MUST reject one. §6's CAN batch says the same, for
the same reason.

`t_base` MUST be the acquisition time of sample 0 — the instant the sensor
took that reading — and not the instant the device read it out.

**Every sample in one batch MUST be evenly spaced by `period`.** That is what
lets a client derive each timestamp arithmetically, and it is a real constraint
on a device draining a FIFO: if the FIFO overflowed, or the sensor was
reconfigured, or a read was missed, the samples either side of the gap are no
longer `period` apart and every timestamp the client derives after the gap is
wrong by the size of it — silently, and increasingly, because the error is
carried by `i × period` rather than announced.

A device MUST therefore **end the batch at the discontinuity** and start the
next one with a fresh `t_base` taken from the first sample after it. The
samples lost across the gap are counted in `dropped` (§8.3) exactly as any
other loss. Splitting costs one extra notification at the moment a device is
already in trouble, which is the cheapest possible price for not shipping a
timeline that is quietly wrong from that point on.

Samples are commonly drained from a sensor's FIFO in bursts, so the read
happens well after the earliest sample in the burst was taken: at 833 Hz a
sixteen-deep FIFO is nearly twenty milliseconds. A device stamping the drain
reports the whole batch late by the depth of its own buffer, and the error
changes with the buffer's occupancy, so it is not even a constant a client
could calibrate away.

Unlike a GNSS solution epoch (§5.6) this needs no flag and admits no exception.
The device sets the sampling schedule itself, so it knows the interval and how
many samples it drained; sample 0's time is the drain time less the samples
behind it. A device that cannot work that out is not measuring what it claims
to measure.

A CAN record carries its own `dt` (§6.1) because bus frames arrive when the bus
decides. IMU samples do not: the device reads its sensor on a schedule it sets,
so one interval describes the whole batch and a per-sample offset would carry
nothing the header does not already say. The two forms differ because what is
being timestamped differs, not because they are two conventions for one thing.

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
seconds. A device MUST NOT report a period of zero, and a receiver MUST reject
a batch that does: zero says every sample in the batch was taken at the same
instant, which describes no measurement, and a client dividing by it to recover
a rate divides by zero.

### 7.1 Axes and signs

The sensor frame is the device's own. **Vehicle alignment is the client's job**
— this specification does not say where the device is mounted or which way it
faces, because it cannot know.

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

The accelerometer convention is the one worth stating twice. Both signs are in
use in the wild — some parts and some libraries report the gravity vector
instead, which is the exact negative — and a client that assumes the wrong one
sees a car braking when it is accelerating. The mistake survives every
plausible sanity check, because the magnitudes are right.

### 7.2 Saturation

A sample beyond the sensor's range is a measurement the device did not make.
`i16` at these scales gives ±32.767 g and ±1638.35 °/s, and a real part
saturates well before either — but whatever the limit, the reading at it is
"at least this much", not "this much".

A device MUST set `imu_header.flags` bit 2 when any sample in the batch is at
or beyond the range of the sensor that produced it. A client MUST treat every
sample in a batch so marked as a lower bound on the magnitude rather than a
measurement, and SHOULD NOT integrate one.

The flag is per batch rather than per sample because `imu_sample` has no room
for one and is deliberately closed (§11.3): twelve bytes at up to 833 Hz is the
one stream where a per-sample byte costs real airtime. A batch is a short
enough window — nineteen samples at 833 Hz is 23 ms — that "something in here
clipped" is enough for a client to distrust the batch and say so.

Saturation is not absence. A saturated axis still has its presence flag set,
because the sensor is fitted and did report: what is in doubt is the value's
magnitude, not whether there is one. That is why this is a flag of its own
rather than a cleared presence bit — §1.1 asks for the honest state, and
"present but railed" is a different state from "not fitted".

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

**The first notification sent on a characteristic after a connection is
established carries `seq` 0**, and the second carries 1. A client consequently
never has to distinguish a reconnection from a wrap, and the protocol needs no
session or boot identifier to make that distinction for it.

Stated as a property of the notification rather than of the counter because
the counter phrasing is ambiguous, and the ambiguity has already cost
something: "restarts at 0" can be read as the counter being zeroed and the
first notification then taking the next value, which puts 1 on the wire. A
device did exactly that, and its own conformance check was written to match —
asserting the first notification carried 1 — so the test agreed with the bug it
existed to catch.

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

A subscription mode that forwards less than every frame is filtering as well. A
frame the governing subscription's mode did not select — outside a `periodic`
interval, inside an `on_change` debounce, or not the Nth under `every_nth`
(§6.8) — MUST NOT be counted in `dropped` either. A client that asked for one
frame in ten has not lost the other nine; the device did exactly what it
installed, and a counter that says otherwise sends a client hunting a fault
that does not exist.

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

**`dropped` is a best-effort diagnostic.** It is there so a client can tell "my
link is bad" from "the device is overrun", and to put a number on the second.
It is not an audit trail, and a client MUST NOT use it to reconcile counts.

That is a deliberate limit on how hard a device has to work. Attributing every
lost item to exactly one notification means owning the counter transactionally
across encoding, transmit-queue refusal and supersession, and the failure mode
when a device does not is that a count lands one notification late — which
still says the device is losing data, at roughly the rate it is losing it, and
that is the whole question the field exists to answer.

So: a device MUST count every accepted-then-discarded item, MUST saturate
rather than wrap, and MUST NOT report loss it did not have. A device MAY report
a discard in the next notification rather than the one it strictly belonged to.
`seq` is the field with the exact guarantee, and it is exact precisely because
it is cheap to be: it counts notifications actually sent, and is committed when
the transport accepts one.

---

## 9. Control characteristic — WRITE, response by INDICATE

Requests are `[opcode:u8][tag:u8][params…]`. Responses are
`[opcode:u8][tag:u8][status:u8][detail…]`.

`tag` is chosen by the client and MUST be echoed in the response so that
requests and responses can be correlated. A device MUST respond to every
request it applies.

**A client MUST have at most one request outstanding.** It writes a request,
waits for the indication that answers it, and only then writes the next one.

That is the whole of the control lifecycle, and it is deliberately the
simplest thing that works. An earlier draft let a client pipeline and required
a device to accept at least four outstanding requests, which bought one thing —
installing a subscription table without a round trip per connection interval —
and cost every implementer a queue, a depth, an ordering guarantee and a
refusal to hold them together. Nothing in this protocol is latency-critical on
the control plane: subscriptions are installed once at connect, rates change
when a user changes them, and `TIME_SYNC` measures the round trip it is
already waiting for.

A device MUST answer `busy` to a request that arrives while it still owes a
response, and MUST NOT apply it. A client that receives `busy` has broken the
rule above; it MUST wait for the outstanding response and MAY then retry, and
MUST NOT treat the request as refused — `busy` says nothing about the request
itself. The status exists so that a device meeting a client which pipelines
anyway has something true to say, rather than a choice between silence and
applying what it cannot answer.

A client MUST NOT reuse a `tag` while a request bearing it is still
outstanding. It needs no enforcement: with one request outstanding, a second
written before the answer arrives is refused `busy` whatever tag it carries,
and one written afterwards has nothing to collide with. **Tag ambiguity is
therefore not prevented, it is impossible** — and a device needs no table of
outstanding tags at all. It echoes the tag and forgets it.

A tag becomes reusable as soon as its response has been sent. The rule is "not
while outstanding", not "never twice".

**`detail` is present if and only if `status` is `ok`.** A refused request is
answered with exactly three bytes, and a client MUST NOT read the detail of a
response whose status is anything else. The alternative — a fixed-width
response with the detail zeroed on failure — puts a well-formed handle 0 or a
well-formed `link_params` of all zeroes in front of a client that has already
decided the request succeeded, which is the plausible-wrong-value failure §1.1
exists to prevent.

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
`bad_params`. The order matters because the two refusals mean different things
to a client: one says "not on this device, ever", the other says "try again
with better arguments", and a client that gets them the wrong way round either
retries forever or gives up on a device that would have worked.

The same order applies one level down. A subscription mode the device does not
support (§4.1) is `bad_params`, and it is checked *after* the opcode's own
capability: `CAN_SUBSCRIBE` with `on_change` on a device with no CAN at all is
`unsupported_opcode`, because the opcode was never available to carry the mode.

`TIME_SYNC` and `GET_LINK_PARAMS` have no owning capability. They are about the
link and the clock, which every device has, and reaching them at all means the
Control characteristic is live.

<!-- BEGIN GENERATED: control -->
| Opcode | Command | Needs | Params | Response detail | Notes |
| --- | --- | --- | --- | --- | --- |
| `0x01` | `CAN_RESET` | `can` | — | — | Clear all subscriptions and stop the CAN stream |
| `0x02` | `CAN_SUBSCRIBE` | `can` | `id:u32, mode:u8, arg:u16` | `handle:u16` | Equivalent to CAN_SUBSCRIBE_MASK with mask 0x3FFFFFFF |
| `0x03` | `CAN_SUBSCRIBE_MASK` | `masked_subscriptions` | `id:u32, mask:u32, mode:u8, arg:u16` | `handle:u16` | — |
| `0x04` | `CAN_UNSUBSCRIBE` | `can` | `handle:u16` | — | Removes one subscription by the handle its install returned |
| `0x05` | `CAN_LIST` | `can` | `start:u16` | `can_list_page record` | One page of the table, starting at index `start` |
| `0x10` | `GPS_SET_RATE` | `gps` | `hz:u16` | — | 0 stops the stream; unsupported rates answer bad_params (SPEC.md 9.8) |
| `0x11` | `GNSS_AID_INFO` | `gnss_aiding` | — | `gnss_aid_caps record` | What aiding this device accepts, and what it already holds (SPEC.md 14.2) |
| `0x12` | `GNSS_AID_BEGIN` | `gnss_aiding` | `format:u8, total_bytes:u32` | `aid_begin_result record` | Open a transfer; the response fixes the session and the chunk size (SPEC.md 14.3) |
| `0x13` | `GNSS_AID_COMMIT` | `gnss_aiding` | `session:u8, chunks:u16, crc32:u32` | `aid_commit_result record` | Close a transfer and report what became of it (SPEC.md 14.4) |
| `0x14` | `GNSS_AID_ABORT` | `gnss_aiding` | `session:u8` | — | Discard a transfer in progress and free the session |
| `0x20` | `IMU_SET_RATE` | `imu` | `hz:u16` | — | 0 stops the stream; unsupported rates answer bad_params (SPEC.md 9.8) |
| `0x30` | `TIME_SYNC` | — | — | `time_sync record` | The device clock when the request arrived and when the answer was prepared (SPEC.md 9.7) |
| `0x31` | `GET_LINK_PARAMS` | — | — | `link_params record` | — |
| `0x40` | `MONITOR_LIST` | `monitor` | — | `monitor_declaration record` | Every channel this device asks the client to supply, in one response (SPEC.md 13.3) |
<!-- END GENERATED: control -->

`status` values:

<!-- BEGIN GENERATED: enum:status -->
| Value | Name | Meaning |
| --- | --- | --- |
| 0 | `ok` | Request accepted |
| 1 | `unsupported_opcode` | Opcode not implemented |
| 2 | `bad_params` | Parameters malformed or out of range |
| 3 | `table_full` | No free subscription slot |
| 4 | `rate_exceeded` | Requested rate is above gps_max_rate_hz or imu_max_rate_hz (SPEC.md 9.8). Never used for CAN |
| 5 | `busy` | A response is already outstanding; wait for it, then retry (SPEC.md 9) |
| 6 | `needs_encryption` | Allocated, never sent: encryption is enforced by GATT permission (SPEC.md 10) |
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

`tag` is opaque to the device and MUST be echoed unchanged.
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
| 10 | 2 | `u16` | `peripheral_latency` | Connection events the device may skip; valid when `validity` bit 2 (`conn_params`) is set |
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
receiver MUST report them absent.

**A bit governing several fields MUST NOT be set unless every one of them is
known.** `conn_params` covers three values and `phy` covers two, and a device
that knows one of a pair has not learned the other: setting the bit and filling
the remainder with a zero, or with a copy of the field it does have, publishes
a guess with a validity bit asserting it is a measurement. Half a group is the
same state as none of it, and the honest encoding of that state is a clear
bit. A device whose controller does not expose a
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

### 9.2 CAN subscriptions

Installing a subscription returns a **handle**. The handle identifies that
subscription for as long as it is installed; it is assigned by the device,
opaque to the client, and MUST NOT be reused while the subscription it names
exists. It MAY be reused once that subscription has been removed.

A subscription matches a frame when `frame.id & mask == sub.id & mask`, taken
over **bits 0–29**: the twenty-nine arbitration bits and bit 29, the standard/
extended format bit. A set bit in `mask` is a bit that a frame must match; a
clear bit is a bit that may hold anything. One entry therefore covers a family
of identifiers, and a mask of zero covers every frame on the bus. Bits 30 and
31 — CAN FD and RTR — describe how a frame was transmitted rather than which
frame it is, and take no part in matching; a device MUST ignore them in both
`id` and `mask`. Why the table is addressed this way rather than by identifier
is RATIONALE §6.

`CAN_SUBSCRIBE` is exactly `CAN_SUBSCRIBE_MASK` with a mask of `0x3FFFFFFF`.

The format bit is part of a frame's identity because standard `0x1A0` and
extended `0x1A0` are two different frames, carrying two different things, from
possibly two different ECUs. A mask that stopped at `0x1FFFFFFF` could not tell
them apart, so a client that subscribed to one would silently receive both and
decode the wrong payload with the right-looking identifier — the failure §1.1
exists to prevent. A client that genuinely wants both formats says so by
clearing bit 29 in its `mask`, which is a request the device can honour because
it can see that it was asked.

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

### 9.4 Load

**A device MUST NOT refuse a CAN subscription on rate grounds.** It admits, and
if the resulting load exceeds what it can forward it sheds — reporting the loss
in `dropped` and setting `flags` bit 0 (§6.3). `can_max_frames_per_s` (§4)
describes what the device can forward; it is not a budget the device polices at
admission.

An earlier draft asked a device to predict the load a subscription would add
and refuse with `rate_exceeded` beyond that budget. The prediction cannot be
made, in three separate ways, and the rule was removed rather than patched a
third time:

- It was never decidable for `every_frame` or `on_change`, because the device
  cannot know what the bus will carry. That was acknowledged from the start and
  those two modes were exempted, which already left the rule covering half the
  cases.
- `every_nth` with N of 1 selects exactly what `every_frame` selects. One was
  rate-admitted and the other exempt, so the same subscription was accepted or
  refused according to which way the client spelled it.
- A masked subscription keeps its schedule per matching identifier (§6.8), so a
  `periodic` subscription over a mask covering ten identifiers produces ten
  times the rate its `arg` names. The arithmetic was written for a single
  identifier and has been wrong for masked subscriptions since masks existed.

Shedding is the honest mechanism and the device has it already: it is
observable by the client, it degrades rather than fails, and it needs no
prediction. A subscription that turns out to be too much is discovered by the
device in the only way it can be — by trying.

`rate_exceeded` remains for `GPS_SET_RATE` and `IMU_SET_RATE`, where the device
knows its own `gps_max_rate_hz` and `imu_max_rate_hz` and the answer is a fact
rather than a forecast.

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
| 5 | 1 | `u8` | `reserved` | Paging metadata; **reserved — MUST be zero** |
<!-- END GENERATED: can_list_page -->

followed by `count` entries:

<!-- BEGIN GENERATED: can_subscription -->
*One installed CAN subscription, as the device holds it.*

Total: **13 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 2 | `u16` | `handle` | Identifies this subscription; assigned by the device |
| 2 | 4 | `u32` | `id` | bits 0-28 arbitration id; b29 extended; b30/b31 ignored |
| 6 | 4 | `u32` | `mask` | A set bit is a bit of `id` that must match; bits 0-29 only |
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

### 9.6 The request lifecycle

**A client MUST enable indications on Control before its first write.**
Responses arrive by indication on that characteristic, so a write that precedes
enablement is a request whose answer has nowhere to go. Every client subscribes
to a characteristic before using it, so this costs a client nothing to satisfy
and removes a state a device would otherwise have to reason about.

**A device MUST NOT apply a request it cannot answer.** If the response cannot
be delivered — indications not enabled, or a response already outstanding — the
request MUST NOT take effect, and the device MUST NOT count it as received.
Deliverability is therefore decided *before* dispatch, not after.

This is the clause the rest of the lifecycle rests on, and it is the one an
implementation is most likely to get wrong, because applying first and
answering second is the natural order to write the code in. A device that
applies a request whose response is then lost leaves the client with no way to
find out what happened: the client retries, and for any request that is not
idempotent the retry applies it a second time. The failure was observed in
practice before it was specified — a device applied a subscription, dropped the
refusal it owed for a later one, and the client timed out and dropped the link
while the device believed itself correctly configured.

**Every opcode in this specification is safe to retry**, which is a property
worth stating rather than leaving each implementer to derive:

| Opcode | Why a retry is safe |
| --- | --- |
| `CAN_SUBSCRIBE`, `CAN_SUBSCRIBE_MASK` | §9.2 — the same `id` and `mask` update in place and return the existing handle |
| `CAN_UNSUBSCRIBE` | A second attempt answers `unknown_handle`; the table is the same either way |
| `CAN_RESET` | Clearing an empty table is clearing an empty table |
| `GPS_SET_RATE`, `IMU_SET_RATE` | Setting a rate to the value it already holds |
| `CAN_LIST`, `MONITOR_LIST`, `GET_LINK_PARAMS` | Reads |
| `TIME_SYNC` | Each attempt is answered with a fresh reading, never a stale one |

A client MAY therefore retry any request whose response it did not receive. It
MUST NOT assume the original did not take effect — only that repeating it is
harmless.

**A client MUST discard a response whose tag it is no longer waiting on.** A
response that arrives after the client has given up on that request is a
measurement of a moment that has passed. This matters most for `TIME_SYNC`,
where a late response carries a device clock reading that was true when it was
taken and is not true now; §9.1's link parameters and the two list opcodes have
the same property in weaker form.

---


### 9.7 TIME_SYNC

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
when the device finished preparing this answer. `t_device_tx` MUST NOT be
earlier than `t_device_rx`.

**The request carries no parameters.** An earlier draft had it carry the host's
UTC time in milliseconds, which the equations below then could not use: they
subtract client timestamps from device ones, and a millisecond count since 1970
cannot be subtracted from a microsecond count since the device booted. The
device ignored the field, which was the only thing it could do with it.

**Units and clocks.** All four timestamps are **microseconds on a monotonic
clock**. *t₁* and *t₄* are readings of the **client's** monotonic clock — taken
as it issues the write and as the indication arrives — and `t_device_rx` and
`t_device_tx` are readings of the **device's** (§8.1). The two clocks share
neither an origin nor a rate, which is the entire reason for the exchange; what
they must share is a unit, and this paragraph is where that is said.

Neither clock is UTC and neither is required to relate to it. Mapping the
client's monotonic clock to wall time is the client's own problem, solved on
the client with facilities this protocol does not need to supply; a GPS fix's
`t_utc` (§5) is the separate mechanism for relating the *device* to wall time.

    offset ≈ ((t_device_rx − t₁) + (t_device_tx − t₄)) ÷ 2
    delay  ≈ (t₄ − t₁) − (t_device_tx − t_device_rx)

**`offset` is the device clock minus the client clock**, so a device timestamp
is converted to client time by subtracting it and a client timestamp to device
time by adding it. The sign is stated because it is the half of this exchange
an implementer cannot check against reality: a client that has it backwards
produces timestamps that are wrong by twice the offset and look entirely
ordinary.

This is the exchange NTP uses, for the reason NTP uses it. **One timestamp
cannot bound its own error.** A client that knows only when it asked, when it
heard back, and one device reading has no way to separate the outbound delay
from the inbound one, so its estimate of the device clock is uncertain by the
whole round trip — and over a link with a 30 ms connection interval that is
tens of milliseconds, in an exchange whose purpose is to align a microsecond
clock. Reporting the device's own processing time is what lets the client take
it out of the arithmetic, and `delay` is a number the client can act on rather
than a bound it has to assume.

A client SHOULD issue `TIME_SYNC` several times and keep the sample with the
smallest `delay`. The sample that spent least time in flight is the one whose
outbound and inbound halves have least room to differ, so it is the one whose
offset is most nearly right. A single sample gives a client no way to tell a
good exchange from one that happened to sit behind a full transmit queue.

**What this does not remove.** The response travels by indication, so it is
queued and goes out at the next connection event; `t_device_tx` is when the
device prepared the answer, not when the radio sent it. The remaining
uncertainty is therefore the asymmetry between the two queuing delays, which
neither end can observe. The exchange bounds what it can measure and this
paragraph states what it cannot, rather than leaving a client to discover that
`delay` is a floor and not a total.

A device MUST take `t_device_rx` when the write arrives, not when it begins
composing the reply. The gap between those is exactly the processing time this
exchange exists to expose, and a device that reads its clock once and reports
it as both has silently reported the single-timestamp form while appearing to
implement this one.

### 9.8 Setting a rate

`GPS_SET_RATE` and `IMU_SET_RATE` each take one `hz:u16` and answer with no
detail. Four rules govern them:

**Zero stops the stream.** `hz` of 0 is a valid request meaning "send nothing".
The device stops producing notifications on that characteristic, keeps the
client's GATT subscription, and reports `gps_rate_hz` or `imu_rate_hz` as 0 in
Info. It is not an error and it is not a shorthand for "restore the default" —
there is no default to restore, because §4 says a client MUST NOT substitute
one.

**A rate the device does not support is `bad_params`.** A device MAY support
only a discrete set of rates; most GNSS and IMU parts do. It MUST NOT silently
apply the nearest one it can manage. Answering `ok` for a rate the device did
not adopt is a plausible wrong value in the sense of §1.1: the client believes
it is receiving 25 Hz, the timestamps say otherwise, and nothing connects the
two. A rate above `gps_max_rate_hz` or `imu_max_rate_hz` is `rate_exceeded`
instead, because that ceiling is a fact the client could have read in advance.

There is no way to enumerate the supported rates, and deliberately so. A client
that wants a rate asks for it and finds out; that is one round trip against a
device it is already talking to, and a discovery mechanism for it would be a
list format, a paging scheme and a second thing to keep in step with Info.

**The applied rate is read back from Info.** The response carries no detail, so
a client that needs to know what it got re-reads the Info characteristic, where
`gps_rate_hz` and `imu_rate_hz` are the rate **currently in effect** (§4). This
is why those fields exist alongside the `_max_` ones. Putting the applied rate
in the response as well would create two statements of it that a device could
let disagree.

**The change takes effect within one notification.** After an `ok`, at most one
further notification MAY be produced at the old rate — the one already batched
when the request arrived. Everything after it is at the new rate. A device MUST
NOT reuse a batch across the change: §7's `period` and §6.1's `t_base` describe
the batch they are in, so a batch spanning a rate change describes itself
wrongly. The reference device flushes the batch the change invalidates, which
is why `VtpDevice.handle_control` produces notifications outside `poll()`.

Both opcodes are idempotent: setting a rate to the value it already holds is
`ok` and changes nothing (§9.6).

---

## 10. Security

**Encryption is the device's decision, not this specification's.** A device MAY
require an encrypted link on any characteristic, on all of them, or on none. No
characteristic is required to be encrypted, and none is forbidden from being
so.

**A client MUST support encryption on every characteristic.** A client that
meets `Insufficient Encryption` or `Insufficient Authentication` on any read,
write or subscription MUST initiate pairing and retry, and MUST NOT report the
device as faulty or absent. This is the obligation that makes the device's
freedom safe to grant: a device author choosing to protect their link must not
thereby become unreadable by conforming clients.

The obligation is one-sided on purpose. Requiring encryption costs the device
author real work — bond storage, a bond table that fills, and a mismatch after
reflashing that presents as a broken device — and that cost lands hardest on
exactly the small implementations this protocol needs. Supporting encryption
costs a client almost nothing: every major central stack turns `Insufficient
Encryption` into a pairing attempt on its own. Putting the requirement on the
side that can bear it leaves each device free to choose its own posture without
fragmenting what clients can talk to.

### 10.1 How a device requires it

A device that requires encryption MUST enforce that with the GATT encryption
permission, not with an application-level check.

The two are not interchangeable, and an earlier draft of this section required
both, which cannot be implemented. A characteristic carrying the permission is
enforced by the ATT layer: an unencrypted write is answered `Insufficient
Encryption` and never reaches application code, so there is nothing there to
generate a reply from. A device that *can* reply has not set the permission.
The delivery path settles it for Control either way — a response travels by
indication on that characteristic, so on a device that has set the permission a
client cannot even enable indications until the link is encrypted, and an
application-level refusal would have nowhere to go.

Status `needs_encryption` (6) remains allocated and MUST NOT be reused for
anything else, but a conforming device has no occasion to send it.

### 10.2 What to protect, and what it buys

A device SHOULD leave the Info characteristic readable on an unencrypted link.
A client that cannot pair — or has not yet — can then still identify what it
has found and say so, rather than reporting a device that is present,
advertising a VTP service and apparently broken. Info carries no measurement:
version, capabilities, rates and buffer sizes, all of which the advertisement
already hints at (§3.3).

A device that protects anything SHOULD protect the streams and not only
Control. Control carries commands — which identifiers to forward, at what rate
— and an eavesdropper learns little from them. The streams carry the
measurement, including position, and that is the part with something to reveal.
Encrypting Control alone is a common arrangement and an incoherent one: it
guards who may reconfigure the device while leaving what the device reports in
the clear.

A device fitted to a vehicle bus carrying anything beyond powertrain telemetry
SHOULD require encryption on every characteristic. A modern bus carries far
more than the engine — location, identifiers, and door and lock activity among
them — and a device with access to it is handling personal data whatever it
was built to measure.

When pairing does occur, LE Secure Connections is REQUIRED. Just Works pairing
is acceptable, and an implementer should know what it does and does not
provide: LE Secure Connections protects against a passive listener even under
Just Works, but Just Works has no authentication step, so it does not protect
against an active man-in-the-middle. Encryption here is a defence against
eavesdropping, not against an attacker who is willing to interpose.

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

A minor version has exactly three places to put something new. They are listed
because the alternative — a general promise that new fields go in extension
records — was not true of this specification and could not be made true after
the fact. A conforming receiver rejects a payload whose length it does not
expect (§5.5, §6.2, §7), so a trailer that did not exist in 1.0 cannot be
introduced later: the first device to send one is rejected outright by every
client already deployed. Extensibility is a decision taken before 1.0 or not at
all.

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
| `can_list_page` | No — closed for major version 1 | One per CAN_LIST page |
| `can_subscription` | No — closed for major version 1 | One per table entry |
| `control_response` | No — closed for major version 1 | — |
| `time_sync` | No — closed for major version 1 | — |
| `link_params` | No — closed for major version 1 | On request |
| `gnss_aid_caps` | No — closed for major version 1 | — |
| `aid_begin_result` | No — closed for major version 1 | — |
| `aid_chunk` | No — closed for major version 1 | — |
| `aid_commit_result` | No — closed for major version 1 | — |
<!-- END GENERATED: extensibility -->

**Reserved space**, for flags and small values. Appendix A lists it. This is
where a new boolean or a small enumerated value goes.

**New control opcodes.** Control requests and responses are not fixed-size
records, so a minor version may add as many as it needs, with any payload. This
is the general-purpose extension point: anything a client can ask for, rather
than anything the device pushes, is extensible without limit. Multi-bus CAN
(§6.9) is intended to be closed this way.

A record marked closed above stays closed for the life of major version 1. A
field it does not carry today is a VTP/2 change, and §6.5 and §6.6 name two
already: a remote frame's requested length, and CAN FD's BRS and ESI.

The per-frame and per-sample records are closed deliberately rather than by
omission. A one-byte trailer on `can_record` costs 4 kB/s at 4000 frames per
second, on the one stream that can saturate a link — the same arithmetic §6.6
used to exclude BRS and ESI, which does not stop applying because the byte is
named differently.

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

## 13. Monitor characteristic — WRITE

Every other role carries measurement from the device to the client. Monitor runs
the other way: the client supplies values the device cannot compute, so that a
device with a display can show them.

Lap time is the example that justifies the role. A logger has no idea where the
start and finish line is — that is drawn on a map in the client — so a device
can only ever display a lap time that the client sends it.

A device implementing this role MUST set `capabilities` bit 3, which §4.1
requires it to set `control` alongside. The `monitor_values` characteristic is
present on every VTP/1 device whether or not the role is implemented (§4.1):
without the bit it is inert, and a write to it is answered with an ATT error
rather than silently accepted.

### 13.1 The device asks; the client supplies

The device declares which channels it wants. The client reads that declaration
with `MONITOR_LIST` (§9), evaluates the channels it can, and writes values to
`monitor_values`.

The declaration is **fixed for the duration of a connection**. A device that
needs a different set asks for everything it might display and chooses locally,
or reconnects. This is the same rule as §9.2's subscription table for the same
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

A device names a channel; it does not send an expression to be evaluated. The
protocol therefore needs no expression language, no shared namespace of variable
names, and no parser on either side, and a client cannot fail to understand a
request in any way except not implementing the channel.

Each channel has exactly one unit, fixed by this table. There is no unit
negotiation and no scale factor: `lap_time` is milliseconds everywhere, forever.

A client that does not implement a requested channel MUST report it absent
(§13.4) rather than omitting it. Absent is a state the device can render; silence
is indistinguishable from the client having crashed.

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
answers with the whole of it. §13.4 caps a device at 15 channels — the most
that fit in one complete client write at the minimum ATT MTU — and 15 channels
are 62 bytes, comfortably inside the 97 a response carries at that same MTU. A
page index could never be anything but zero.

This is where Monitor and §9.5's CAN table genuinely differ, and the reason is
worth stating: `can_subscription_slots` may be far larger than one response can
carry, so `CAN_LIST` must page and does. Monitor cannot need it, so it does not
have it.

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
direction, and it is the reason the flag exists: before the first lap of a
session there is no last lap time, and a device that displays 0.000 for it has
been told something false. The client MUST clear the bit rather than omit the
slot or send a placeholder.

**Every write MUST carry a value for every slot the device asked for.** A
Monitor write is a complete statement of what the client can currently supply,
not a set of changes to what it said before.

Complete writes cost almost nothing here — the whole set is one small write at
any plausible channel count — and they buy two things that deltas do not. A
write that is lost changes nothing permanently, because the next one restates
everything, so `seq` gaps need no recovery procedure and there is none to get
wrong. And the client never has to remember what it last sent, which is the
state that silently diverges when an app is backgrounded and resumed.

A slot MUST appear at most once in a write. A device MUST reject a write
containing a slot twice, because nothing in this specification says which of
the two wins and a device choosing either is choosing for every client.

**`count` MUST NOT be zero**, and a device MUST reject a write that carries no
values. An empty write is the one thing a complete statement cannot be: on a
device that asked for channels it names none of them, which is not "nothing
changed" but "I can supply nothing" said in a way that leaves every previous
value standing. A client with nothing to supply says so by writing every slot
with the `present` bit clear, which is a complete statement and expires
correctly (§13.5). A client with nothing to say does not write at all, and
§13.5's deadlines take care of the rest.

A device that asked for no channels (§13.5) has no complete write to receive,
so a client MUST NOT write to it at all.

A device MUST NOT ask for more channels than fit in a single write at the
minimum ATT MTU of §2: with a 4-byte header and 6 bytes per value that is
**15 channels**. Complete writes are only a workable rule if a complete write
always fits, and a device that asks for more has made the rule unsatisfiable
rather than made itself more capable.

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

A device MAY declare no channels at all — `total` and `count` of zero is the
state of a device that has not yet configured itself, or one whose display
currently needs nothing. A client MUST accept the empty declaration and MUST
NOT write values to a device that asked for none; every slot it could name is
one the device did not ask for, and §13.1 already says those are ignored.

The client MUST refresh before the deadline. A client SHOULD write only when
something it can supply has changed — but "nothing has changed" is not a reason
to let a value expire, so it MUST write anyway as the deadline approaches, and
that write costs one small packet at whatever interval the device chose.

`max_age` is per channel because the channels differ in kind. A `lap_time`
ticking up is wrong within a second of going stale; a `best_lap_time` stays
true until it is beaten, so it can carry a deadline measured in tens of seconds
rather than one. A single device-wide timeout would be set for the most
perishable channel and would then demand pointless traffic for the rest.

A device SHOULD choose a `max_age` several times its expected update interval.
It bounds how wrong a display may be, not how often a client must talk, and one
set close to the update rate turns an ordinary scheduling delay into a
flickering screen. For a channel that only changes occasionally, choose a large
deadline rather than none: `best_lap_time` at 25.5 seconds is the longest this
field can express, and that is still a bound.

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

A device implementing this role MUST set `capabilities` bit 8, which §4.1
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
| 2 | 1 | `u8` | `flags` | bitmask `aid_flags` |
| 3 | 1 | `u8` | `reserved_3` | Aiding metadata; **reserved — MUST be zero** |
| 4 | 4 | `u32` | `max_bytes` | `bytes`; Largest total_bytes this device will accept in one transfer |
| 8 | 8 | `i64` | `held_until` | `ms`; Unix epoch; the end of the validity window this device already holds (SPEC.md 14.2); valid when `validity` bit 0 (`held_until`) is set |
<!-- END GENERATED: gnss_aid_caps -->

<!-- BEGIN GENERATED: bitmask:aid_validity -->
| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `held_until` | held_until carries the end of a window the device already holds |
| 1+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:aid_validity -->

<!-- BEGIN GENERATED: bitmask:aid_flags -->
| Bit | Name | Meaning |
| --- | --- | --- |
| 0 | `persists` | Aiding survives a power cycle, so held_until still applies on the next connection |
| 1+ | *reserved* | MUST be zero on transmit; MUST be ignored on receive |
<!-- END GENERATED: bitmask:aid_flags -->

`max_bytes` is a device ceiling. A `GNSS_AID_BEGIN` whose `total_bytes` exceeds
it MUST be answered `bad_params`.

`held_until` describes what the device holds **now**. A client SHOULD NOT send
data whose validity the device already covers; predicted-orbit products run to
tens of kilobytes, and re-sending one the device is still holding costs a
phone's radio that much for nothing. With the `held_until` validity bit clear
the device holds nothing, or does not know what it holds; either way the client
sends.

`persists` says whether that window survives a power cycle. A client MUST NOT
infer from a `held_until` on one connection that the device will still hold it
on the next unless `persists` is set.

### 14.3 Opening a transfer, and filling it

`GNSS_AID_BEGIN` carries the format and the total byte count. Its response
detail is one `aid_begin_result` record:

<!-- BEGIN GENERATED: aid_begin_result -->
*The detail of a GNSS_AID_BEGIN response. Opens a transfer and fixes its chunking.*

Total: **4 bytes**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 1 | `u8` | `session` | Identifies this transfer; quoted in every chunk and at commit |
| 1 | 2 | `u16` | `chunk_bytes` | `bytes`; Payload bytes in every chunk but the last; byte offset is index x chunk_bytes (SPEC.md 14.3) |
| 3 | 1 | `u8` | `reserved_3` | Transfer metadata; **reserved — MUST be zero** |
<!-- END GENERATED: aid_begin_result -->

`chunk_bytes` MUST NOT be zero and MUST NOT exceed `ATT_MTU - 6` — three bytes
of ATT Write Command header and the three-byte chunk header below.

**A transfer MUST NOT require more than 65535 chunks.** `chunks` in
`GNSS_AID_COMMIT` and `first_missing` in §14.4 are both `u16`, so a transfer
needing more than that has a count no client can commit and a gap no device can
name. `total_bytes` is `u32` and nothing else bounds the pair, so the device
enforces it where both numbers are first known: a device MUST answer
`bad_params` to a `GNSS_AID_BEGIN` whose `total_bytes` would need more chunks
than that at the `chunk_bytes` it would have chosen.

**A device holds at most one transfer open.** A `GNSS_AID_BEGIN` arriving while
one is open MUST discard the open transfer and start a new one, and the new
`session` MUST differ from the discarded one so that a chunk still in flight
for the old transfer is rejected by §14.3's session rule rather than landing in
the new one.

Chunks are written to the `aiding` characteristic:

<!-- BEGIN GENERATED: aid_chunk -->
*One chunk of an aiding transfer, written without a response.*

Total: **3 bytes + `payload`**. All fields little-endian.

| Off | Size | Type | Field | Notes |
| --- | --- | --- | --- | --- |
| 0 | 1 | `u8` | `session` | Echoed from the GNSS_AID_BEGIN that opened this transfer |
| 1 | 2 | `u16` | `index` | 0-based; the payload belongs at index x chunk_bytes |
<!-- END GENERATED: aid_chunk -->

A chunk's payload belongs at byte offset `index × chunk_bytes`. **Every chunk
but the last MUST carry exactly `chunk_bytes`**, and the last MUST carry the
remainder of `total_bytes`. The mapping from index to offset is therefore
arithmetic, which is what makes resending part of a transfer possible at all: a
device that had to place variable-length chunks could not place chunk 7 without
having received 0 through 6, and §14.4's missing-chunk report would have
nothing to offer.

A device MUST ignore, without any response, a chunk that:

- names a `session` other than the open transfer's,
- carries an `index` at or beyond `⌈total_bytes ÷ chunk_bytes⌉`, or
- carries a payload of the wrong length for its index.

A client MAY write chunks in any order and MAY write the same chunk more than
once; a device MUST accept a repeat and MUST NOT treat it as an error.

**A device MUST NOT hand any part of a transfer to its receiver before
`GNSS_AID_COMMIT`.** A transfer is applied whole or not at all, which is what
makes the CRC in §14.4 worth checking and what stops a receiver being fed the
first half of something.

### 14.4 Closing it

`GNSS_AID_COMMIT` carries the session, the number of chunks the client wrote
and a CRC-32 over the reassembled `total_bytes` — **not** over the chunks, and
not over the chunk headers.

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

`chunks` is redundant with the transfer's own shape — the device already knows
`⌈total_bytes ÷ chunk_bytes⌉` from the `GNSS_AID_BEGIN` it answered — and it is
carried so that a disagreement is caught rather than acted on. **A device MUST
answer `bad_params` to a commit whose `chunks` is not that number**, and MUST
NOT apply it; the transfer stays open, so a client that miscounted may commit
again.

It cannot be reported as `incomplete`. A device that has every chunk has no
index it did not receive, so the `first_missing` it would have to send names a
chunk it holds — and a client obeying the paragraph above resends that chunk,
commits again with the same wrong count, and receives the same answer forever.
The refusal has to be a status, because the disagreement is about a parameter
rather than about the transfer.

**A result of `incomplete` leaves the transfer open.** The client writes the
chunks it is missing and commits again, and the exchange terminates because
`first_missing` strictly advances each time. Every other result closes the
transfer and frees the session; a client wanting to retry after `bad_crc` or
`rejected` MUST open a new one.

`GNSS_AID_ABORT` discards an open transfer and frees its session. A device MUST
answer `bad_params` to an abort or a commit naming a session it does not hold.

### 14.5 A refused transfer is not a refused request

`GNSS_AID_COMMIT` names an open session with well-formed parameters, so the
device applies it and answers `ok` — including when the transfer it reports on
was incomplete, failed its CRC or was refused by the receiver. Those outcomes
are in `result`, not in `status`.

They cannot be in `status`. §9 makes `detail` present if and only if `status` is
`ok`, so an `incomplete` expressed as a status would carry no `first_missing`
with it, and the client would have nothing to resend from — the report and the
refusal cannot be the same byte. The division is the one §9.1 already draws for
`GET_LINK_PARAMS`: `status` answers whether the device could act on the
request, and the detail carries what it found.

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

---

## Appendix A — Reserved space

Generated from `schema/vtp1.yaml`, so it cannot disagree with the bitmask and
record tables above.

<!-- BEGIN GENERATED: reserved_space -->
| Location | Reserved | Purpose |
| --- | --- | --- |
| `gps_fix.validity` | bits 12–31 | Validity for fields added in a later minor |
| `gps_fix.fix_flags` | bits 5–7 | Additional solution-quality flags |
| `info.capabilities` | bits 9–31 | Roles and features added in a later minor |
| `can_header.flags` | bits 1–7 | Additional batch-level CAN status |
| `imu_header.flags` | bits 3–7 | Additional sensor groups |
| `info.clock_flags` | bits 2–7 | Additional clock properties |
| `monitor_value.validity` | bits 1–7 | Validity for values added in a later minor |
| `link_params.validity` | bits 4–15 | Validity for link parameters added in a later minor |
| `gnss_aid_caps.flags` | bits 1–7 | Additional aiding properties |
| `gnss_aid_caps.validity` | bits 1–7 | Validity for aiding capabilities added in a later minor |
| `aid_commit_result.validity` | bits 1–7 | Validity for commit results added in a later minor |
| `info.reserved_20` | 1 byte | Was can_max_payload; derived from the capability bits since (SPEC.md 4.2) |
| `can_header.reserved` | 2 bytes | Low byte earmarked for a bus index (SPEC.md 6.9); high byte unassigned |
| `imu_header.reserved` | 2 bytes | In-band IMU metadata |
| `monitor_declaration.reserved` | 1 byte | Declaration metadata |
| `monitor_header.reserved` | 1 byte | Update metadata |
| `can_list_page.reserved` | 1 byte | Paging metadata |
| `gnss_aid_caps.reserved_3` | 1 byte | Aiding metadata |
| `aid_begin_result.reserved_3` | 1 byte | Transfer metadata |
| Extension types | `0x80`–`0xFF` | Vendor-private; this specification MUST NOT assign them (§5.5) |
<!-- END GENERATED: reserved_space -->
