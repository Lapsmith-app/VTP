# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versioning follows SPEC.md §11: major versions have separate service UUIDs;
minor versions are additive and never change the decode of an existing
conformance vector.

## [Unreleased]

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
- Conformance corpus: 47 vectors across 5 record types, including must-reject
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
