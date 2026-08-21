# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versioning follows SPEC.md §11: major versions have separate service UUIDs;
minor versions are additive and never change the decode of an existing
conformance vector.

## [Unreleased]

### Changed — wire format
- CAN frame semantics are specified (SPEC.md §6.4-§6.9). The bits were defined;
  what combinations of them mean was not.
  - **Identifier validity is enforced.** A standard frame carrying more than
    eleven bits, a frame with both the CAN FD and RTR bits set, and a remote
    frame with a payload are each rejected whole rather than repaired — a
    repaired frame is a plausible wrong value with a correct-looking identifier.
  - **Timestamps are taken at end of frame** (§6.7). A device cannot generally
    know a frame's time on air without its bit timing and stuff bits, so
    start-of-frame would be back-computed rather than measured. The consequence
    is stated: a 64-byte FD frame at 500 kbit/s is stamped about a millisecond
    after it began.
  - **Subscription modes forward the first matching frame** in every mode
    (§6.8), so a client installing a subscription need not wait for a second
    frame before it can display anything. `every_nth` with N of 0 is
    `bad_params`.

### Documented limits, deliberately
- **A remote frame's requested DLC is not carried** (§6.5). `len` is the payload
  length and the batch's length arithmetic depends on it being exactly that.
- **CAN FD's BRS and ESI are not carried** (§6.6). Both are per-frame, so they
  cost a byte on the highest-volume record in the protocol — 4 kB/s at 4000
  frames per second, on the one stream RATIONALE §4.1 identifies as able to
  saturate a link. Record sizes are frozen within a major version, so adding
  them later is a VTP/2 change. That cost is stated rather than discovered.
- **Major version 1 addresses one CAN bus** (§6.9). The low byte of
  `can_header.reserved` is earmarked for a bus index and MUST be zero until a
  minor version assigns it; per-bus subscription would need a new opcode, which
  a minor version may add.

### Fixed
- `tools/check_docs.py` only recognised a `SPEC` qualifier on section
  references, so a `RATIONALE §4.1` written inside SPEC.md resolved silently
  against the wrong document. It understands both now — and caught this while
  the CAN sections were being written.

### Changed — wire format
- Sequence and loss are specified (SPEC.md §8.2, §8.3). Both fields existed on
  all three streams; only `gps_fix` said what either meant.
  - **`seq` counts notifications on its own characteristic**, uniformly, +1
    each, wrapping. `gps_fix.seq` previously counted *fixes produced*, which no
    batch header can do — a CAN header cannot count frames the device never
    accepted, and an IMU header would jump by `count`. Only the notification is
    something all three streams have exactly one of. No information is lost:
    `dropped` already carries what the device discarded.
  - **`seq` restarts at 0 per connection**, so a client never has to
    distinguish a reconnection from a wrap and the protocol needs no session or
    boot identifier to do it for them.
  - **`dropped` counts items accepted and then discarded.** A CAN frame that
    matched no subscription was never accepted and MUST NOT be counted:
    conflating filtering that worked with capacity that was exceeded makes the
    field useless for the only thing it is for.
  - **`dropped` saturates at 65535 and MUST NOT wrap.** A wrapping drop counter
    reads 0 after exactly 65536 discards — perfect health at the moment the
    device is losing data fastest. That is a plausible wrong value, so §1.1
    spends the ceiling instead.

### Added
- `VtpDevice.simulate_loss()` in the software peripheral. A desktop device
  never loses anything, so a client's `dropped` handling would go untested
  until real hardware on a real track produced some.
- `VtpDevice.on_connect()`, making the per-connection reset explicit and
  testable: sequence numbers to zero, subscription table cleared.

### Changed — wire format
- The CAN control plane is specified rather than named (SPEC.md §9.2-§9.5).
  Building the software peripheral established that `CAN_LIST`,
  `CAN_SUBSCRIBE_MASK` and `LIST_CHANNELS` could not be implemented at all:
  they were listed with no response payload defined.
  - **Subscription handles.** An identifier stopped being a unique name for a
    subscription the moment masks existed. `CAN_SUBSCRIBE` and
    `CAN_SUBSCRIBE_MASK` return a handle; `CAN_UNSUBSCRIBE` takes one, and an
    unknown handle is answered with the new `unknown_handle` status.
    Re-installing the same `(id, mask)` updates in place and keeps its handle,
    so a client reprogramming on every connect cannot exhaust the table.
  - **`CAN_LIST` is paged**, returning a `can_list_page` record followed by
    `can_subscription` entries. At the minimum ATT MTU a response holds six
    entries, against a slot count that may be far larger, so a single-shot
    response was never implementable.
  - **Overlapping subscriptions have a rule** (§9.3): most specific mask, then
    lowest handle, and a frame is forwarded at most once. Both terms are
    visible through `CAN_LIST`.
  - **`rate_exceeded` is only required where it is decidable** (§9.4). For
    `every_frame` and `on_change` a device cannot know future bus traffic; it
    admits and sheds, reporting loss in `dropped`.
  - **Subscriptions do not survive disconnection**, so a client always finds a
    known state.
  - `TIME_SYNC` declares its response layout (`t_device:u64`).
- **`LIST_CHANNELS` removed.** It belonged to the Monitor role, which had a
  UUID and a capability bit but no characteristic format and no state machine.
  Monitor should return as a designed feature or not at all.
- Every control opcode now declares its response detail in the schema, and
  `tools/generate.py` refuses to generate if one does not. Leaving that to
  prose is what made three of them unimplementable.

### Added
- `reference/peripheral/`: a synthetic VTP/1 device. `vtp_device.py` holds one
  monotonic clock, the three roles derived from a common motion model,
  MTU-aware batching and the control plane, with **no Bluetooth dependency**;
  `serve.py` is a thin [bless](https://github.com/kevincar/bless) transport on
  top. The split lets `selftest.py` verify the device in CI on machines with no
  adapter, decoding every notification with the reference decoder — so the
  peripheral is checked by the same decoder that checks the corpus.

  It asserts what no single-payload vector can express: that the three roles
  share one clock and their timestamps interleave, that `seq` advances by one
  per fix, that a field with no validity bit reads absent rather than zero,
  that batches respect the negotiated MTU and the 655.35 ms `dt` window, and
  that control commands change behaviour rather than merely answering. Seven
  seeded device faults are all caught.

  This makes a client developable. It does not make VTP/1 proven on hardware:
  §8's clock discipline, §6.1's timing bounds and §2.1-§2.3 are properties of
  an MCU and a radio, not of a host scheduler.

### Changed — wire format
- `imu_header` grows from 16 to 20 bytes and `period` from `u16` to `u32`
  microseconds. A `u16` microsecond period cannot express any interval longer
  than 65535 µs, which put a **floor of 15.26 Hz on every conforming device** —
  a 10 Hz IMU was unrepresentable. Microseconds are kept rather than a finer
  unit so the field shares the device clock's units with GPS and CAN;
  cross-channel alignment is the point of the clock, and a second scale would
  mean a division at every comparison. The freed bytes give `imu_header` a
  2-byte `reserved` field, matching `can_header`.
- `imu_header.flags` bit 2 (`mag`) removed. It advertised a magnetometer while
  `imu_sample` carries no magnetometer fields, so a device setting it described
  data that had nowhere to go — a flag whose only possible effect was to mislead.
- SPEC.md §8 defines derived timestamps as computed modulo 2^64. The two
  reference decoders disagreed on any payload near the top of the range: C wrapped,
  Python did not. Neither was wrong on its own; the specification now says which.

### Fixed
- SPEC.md §7 claimed `period` was "exact for any ODR". It is not: an interval
  that is not a whole number of microseconds is misrepresented by up to 0.5 µs
  per sample. The bound is now stated instead of claimed away — the error
  accumulates only *within* a batch, since each notification re-anchors on its
  own measured `t_base`, so the worst case at 833 Hz across nineteen samples is
  about 9 µs, below the 10 µs resolution of a CAN frame timestamp.
- SPEC.md §7 offered to carry a sensor-to-case rotation "in an extension
  record". The IMU characteristic has no extension mechanism, so that was
  unimplementable. Mounting orientation is now stated as out of scope for
  major version 1.
- `vtp_encode_gps_fix` accepted `ext_len > 0` with `ext == NULL`, returned
  success, and left those bytes unwritten — the caller would transmit whatever
  the buffer held. `vtp_encode_can_batch` did the same for a payload length with
  a null payload. Both now refuse.
- `vtp_encode_gps_fix` did not check that `ext_count` agreed with the extension
  bytes supplied, so it could emit a record no conforming decoder would accept.
- Two `info` vectors contradicted themselves: the GPS-only board declared "every
  CAN capacity is zero" while reporting `can_max_payload = 8`, and the
  future-minor board reported 64-byte payloads without declaring `can_fd`.

### Added
- `tools/check_corpus.py`: derives from the schema what a vector would have to
  look like to exercise each rule, and fails when the corpus has none. It
  complements `tools/mutate.py` rather than duplicating it — this finds rules no
  vector exercises, that one finds code no rule reaches, and each caught holes
  the other could not.
- Structural validation of `schema/vtp1.yaml` inside `tools/generate.py`, so
  generation refuses to run on an incoherent source of truth. Declaring
  `gps_fix.lat` as `size: 3, type: i32` previously published a three-byte 32-bit
  integer in SPEC.md while the corpus, both references and the mutation sweep
  all stayed green: the tables read `size`, the codecs read `type`, and nothing
  compared them. Now checked: type width against declared size, offset overlap
  and gaps, field coverage against record size, duplicate names, values, bits
  and opcodes, and every enum, bitmask, `valid_bit` and `presence_bit`
  reference resolving.
- `imu_sample` declares its presence bits in the schema (`presence:` and
  `presence_bit:`) rather than only in prose, so the encoder gating, the
  decoder's absent set, the generated spec table and the corpus completeness
  check all derive from one statement of SPEC.md §7.
- `presence` and `bound` mutation operators, covering ternary presence gates and
  range checks such as `len > 64`. Neither was reachable by the existing textual
  `gate32(` pattern.

### Fixed
- **Both reference decoders reported absent IMU sensor groups as a measurement
  of zero**, contradicting SPEC.md §7 and the governing principle in §1.1. They
  now report a per-sample `absent` set, and the corpus asserts it. Found by
  external review.
- The IMU presence gates were entirely untested: every vector either set the
  flag or had zeroes behind a cleared one, so removing either gate changed no
  bytes and all 43 vectors passed. The `len > 64` bound on a CAN record was
  likewise unreachable by any legal vector. Both now have vectors.
- `conformance/run.py` treated a missing `absent` list as "not exercised" and
  still reported a pass. Reporting absence is the protocol's central rule, so
  omitting it is now a failure rather than a silent skip.
- `can_batch` and `imu_batch` had no must-reject vector shorter than their own
  batch header.

- Python reference encoder (`reference/python/vtp1_encode.py`), schema-driven
  like the decoder beside it and split into its own module for the same reason
  the C encoder is its own translation unit: a client needs only the decoder, a
  device needs only the encoder. It enforces rather than trusts — a field whose
  validity bit is clear, and an IMU triple whose presence flag is clear, are
  written as zero whatever the caller passed — while writing
  `can_header.reserved` through, since an encoder must not erase a field a
  later minor may have assigned.

  The Python reference now reports `roundtrip_hex`, so the corpus checks it as
  an encoder too: 35 of the 43 vectors verified byte-identical, where the runner
  previously reported "encoder not exercised". The two encoders were also
  compared directly across the whole corpus and agree on every byte — a real
  check, given one derives its offsets from the schema at runtime and the other
  compiles them in.

  This is the prerequisite for a software peripheral: a program presenting a
  VTP/1 GATT service over a host Bluetooth adapter, so a client can be
  developed against something before any firmware exists.

## [0.1.0] - 2026-08-21

First tagged baseline. Still draft: the wire format may change without notice
until `1.0.0`, at which point the compatibility guarantees in SPEC.md §11 take
effect. Nothing here is a compatibility promise — a `v0.x` tag exists so that
an implementer can say which version they built against, not so that they can
rely on it.

### Added
- Initial specification: GPS, CAN, IMU, Info and Control roles.
- Frozen UUID allocation (`schema/uuids.json`), including the `"VTP"` family
  prefix that lets a client recognise an unsupported major version.
- Machine-readable schema (`schema/vtp1.yaml`) as the source of truth, with
  generation of the spec tables, the C header and the conformance vectors.
- Conformance corpus: 67 vectors across 6 record types, including must-reject
  cases for truncated and over-long payloads.
- C99 reference decoder and encoder, no dependencies, separate translation
  units so a client links only the decoder and a device only the encoder.
- Schema-driven Python reference decoder.
- Transport requirements for link-layer payload, PHY and connection parameters
  (SPEC.md §2.1-§2.3): a device must extend the link-layer payload to match the
  ATT MTU it negotiates, should request the 2M PHY, and must function at
  whatever connection parameters the central grants rather than the ones it
  asked for. These bound the radio airtime a VTP device takes from other
  peripherals sharing the same central.
- `GET_LINK_PARAMS` (opcode `0x31`) and the `link_params` record (SPEC.md §9.1):
  the device reports its own view of the negotiated ATT MTU, link-layer payload,
  connection parameters and PHY. Every field is governed by a validity bit, and
  the `phy` enum has no zero member, so "the controller does not expose this"
  cannot be confused with LE 1M. This is the only way a client can verify the
  transport requirements of §2.1-§2.3, none of which are visible to an
  application through its own Bluetooth stack on at least one major platform.
- SPEC.md §12.1, distinguishing requirements the conformance corpus can test
  from integration requirements it structurally cannot.
- `tools/mutate.py`: a systematic mutation sweep over the C reference. Where CI
  already seeded two faults by hand — proving the corpus *can* fail, but saying
  nothing about coverage — this drops every encoder validity gate, reads every
  decoder field from a sibling's offset, and relaxes every exact-length check,
  requiring the corpus to notice each one. A surviving mutation is a hole in
  the corpus rather than a bug in the decoder.
- `tools/check_docs.py`: checks hand-written prose against the artefacts it
  describes — every `§x.y` reference resolves to a heading that exists
  (including from source comments), and stated corpus counts match the corpus.
  The generator's `--check` covers generated tables; this covers the sentences
  around them, which drift just as silently.
- Implementation-agnostic conformance runner, with two optional checks beyond
  the decode: an `absent` field set (making "absence is the bitmask's job, never
  a value" mechanically testable) and an encoder round-trip required to be
  byte-identical, or to normalise a deliberately non-canonical payload.

### Fixed during the draft
- Eight further holes in the corpus, all found by `tools/mutate.py` on its
  first run and none visible to review: the `gps_fix` encoder's `t_utc` and
  `position` gates were unexercised because the only non-canonical vector left
  both bits set; `link_params` carried a stale `peripheral_latency` of zero,
  which tests nothing; `info` and `imu_batch` had no over-long `must_reject`
  case, leaving their exact-length checks untested; and `info.gps_rate_hz`
  equalled `gps_max_rate_hz` in every vector, so a decoder could read either
  from the other's offset and pass. All fixed by adding vectors, not by
  loosening assertions.
- Three holes in the corpus, each found by mutation-testing rather than by
  review, and each fixed at the generator so it cannot recur case by case:
  unknown enum values were never asserted; a vector carried stale values behind
  cleared validity bits, violating the rule it was meant to demonstrate; and
  the encoder's gating rule had no coverage because every vector was already
  canonical.

### Not yet present
- Reference firmware. VTP/1 is unproven on hardware.
- A Python encoder.
- Any independent implementation.
