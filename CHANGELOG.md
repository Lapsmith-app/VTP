# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versioning follows SPEC.md §11: major versions have separate service UUIDs;
minor versions are additive and never change the decode of an existing
conformance vector.

## [Unreleased]

Draft. The wire format may change without notice until `1.0.0`, at which point
the compatibility guarantees in SPEC.md §11 take effect.

### Added
- Initial specification: GPS, CAN, IMU, Info and Control roles.
- Frozen UUID allocation (`schema/uuids.json`), including the `"VTP"` family
  prefix that lets a client recognise an unsupported major version.
- Machine-readable schema (`schema/vtp1.yaml`) as the source of truth, with
  generation of the spec tables, the C header and the conformance vectors.
- Conformance corpus: 43 vectors across 5 record types, including must-reject
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
