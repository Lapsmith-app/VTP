# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versioning follows SPEC.md §11: major versions have separate service UUIDs;
minor versions are additive and never change the decode of an existing
conformance vector.

## [Unreleased]

### §15 rewritten: response-paced, grouped, divided

Pre-1.0 and with no third-party consumers, so the poll loop was fixed rather
than extended. What was three layered proposals — grouping behind a
capability bit, pacing behind a second bit and a second opcode, rate control
behind a third — is one rule on the opcode that already existed.

**Polling is response-paced (§15.4).** The device transmits the next group
when the previous request has been answered, or `OBD_RESPONSE_TIMEOUT_MS`
(100) has passed, and no sooner than `interval_ms` after the last
transmission. `interval_ms` is a **minimum spacing, not a period**, and 0
means the client imposes none — the car is then the only pacing there is,
which is safe precisely because the device waits for it. Zero is admissible
where a `periodic` subscription's `arg` could not be, because waiting for an
answer cannot generate traffic faster than the car produces it.

**`obd_min_interval_ms` is withdrawn** and Info bytes 22–23 are reserved
again. A device is plugged into a car it has never met, so a rate it
publishes as safe is a guess about a vehicle it cannot see. §15.1's audit
claim is now a discipline rather than a number: one request outstanding,
waits for its answer, never retries, transmits nothing the client did not ask
for. The cost — no rate readable from Info before anything is transmitted —
is stated in RATIONALE §11.5a rather than glossed.

**Grouping is part of the role (§15.4.1)**, not capability bit 11, which is
withdrawn and reserved again. Bit 7 of a PID byte groups it with the byte
that follows; a group is one Mode 01 request and costs the bus nothing,
because the request frame is padded to eight bytes whether it names one PID
or six. A group of one is the old behaviour, so mandating it costs a device
only the parse.

**Every group carries a `u16` minimum interval (§15.4.2).** A group is
transmitted no oftener than its own minimum, and 0 means none. Repetition
could already make a PID faster than the cycle and could never make one
slower; this closes that, and it is admissible where per-PID rates were not
because a minimum can only ever remove a request.

An interval and not a ratio, because under pacing the cycle time is the car's:
one pass in five is a different rate on every vehicle and drifts inside a
session, and a `u8` ratio cannot reach 0.1 Hz on a fast car at all. `fast = 0,
medium = 500, slow = 10000` says 2 Hz and 0.1 Hz and means it.

Measured against the reference peripheral, twelve PIDs on a car answering in
10 ms: 8.0 Hz each ungrouped, **19.8 Hz grouped**, with the schedule paced by
the car rather than by a number the client guessed.

### Reviewed as what it is, and slimmed

Every part of VTP/1 — including the two roles below, which landed while the
review ran — was asked whether it earns its place in a hobbyist DIY telemetry
protocol. The core passed unchanged: the shared clock, batching, validity
bits, `seq`/`dropped`, the fixed attribute table, and Monitor. What did not:

- **Control plane**: `CAN_LIST` and `GET_LINK_PARAMS` removed with their
  records; a conforming client already knows the table because it installed
  it, and link diagnostics belong on a bench (§12.1).
- **Subscriptions**: handles removed — `(id, mask)` is the subscription's
  name; `CAN_UNSUBSCRIBE` takes `id, mask`; status 7 is
  `unknown_subscription`. Modes cut to `every_frame` and `periodic`;
  per-identifier mode state exhaustion is shedding, not refusal (§9.3).
- **Content rules downgraded**: a well-formed payload carrying a forbidden
  value (GPS ranges, RTK contradictions, capability implications, a percent
  above 100) decodes everywhere; the device MUST NOT emit it, a client
  SHOULD flag it and MUST NOT repair it. Structural malformation still
  rejects. Each such rule is one decode vector plus one producer refusal,
  paired mechanically by the corpus gate.
- **Info**: `max_notify_bytes` removed (bytes 22–23 reserved); the
  negotiated ATT payload already says it.
- **Aiding, slimmed in review**: `GNSS_AID_ABORT` removed (a new BEGIN or a
  disconnect already discards), the commit's chunk count removed (the CRC
  backstops it), the `persists` flag removed (GNSS_AID_INFO is re-read every
  connection). The transfer token STAYED: a draft removed it on a
  one-ordered-bearer argument that EATT (Bluetooth 5.2) refutes —
  RATIONALE 10.6 records both halves.
- **Retired wire values stay unassigned**: capability bit 7, sub-modes 2–3,
  opcodes `0x05`, `0x14`, `0x31`. The generated masks derive the reserved
  set from the named bits, so a retired bit is reserved like the range
  above it.

The corpus baseline was regenerated (`check_baseline.py --accept`);
deliberately not a minor version, which v0.x exists to permit.

### Added

- **OBD-II polling** — capability bit 10 (`obd`, requires `can` and
  `control`), opcodes `0x60` `OBD_INFO` and `0x61` `OBD_POLL_SET`, records
  `obd_probe` + `obd_ecu`, `can_flags` bit 1 (`polling`). The first role
  whose device TRANSMITS on the vehicle bus, which is the reason it is a
  declared capability at all: without the bit, the protocol loses the
  ability to say whether a given device transmits. What may be transmitted
  is a closed enumeration (single-frame Mode 01 requests, one PID each
  before §15.4.1's grouping and at most six after,
  spaced, never retried, no flow control); responses arrive as ordinary
  `can_record`s — delivered on the probe's reported response identifiers
  while the poll set is non-empty, with the subscription table governing
  anything it matches first, so an accepted poll set is the whole of what
  a client does to receive the answers; supported-PID masks make the role
  declare-verify-use like everything else. Identifier validity on the
  probe is scoped to a probe that answered — a gated request_id is absent
  and cannot reject the response it rides in — and every completed probe
  replaces the probe result and clears the poll set, so a transmitter
  never outlives the result it was verified against. The `OBD_INFO`
  response reports a COMPLETED probe: the reference peripheral applies
  the request at once (9.6's order) and holds the indication until the
  last request's collection window has passed, the request staying the
  one outstanding (busy to anything written meanwhile). The two OBD
  capacities MUST be non-zero while bit 10 is set (`capacity_required` in
  the schema, a generated rules table beside the zero-when-clear one),
  with the decode-and-flag / encoder-refusal halves paired in the corpus
  like every content rule. Info's two freed
  reserved fields become the role's capacities (`obd_poll_slots` at offset
  20, `obd_min_interval_ms` at 22) per §11.2 — the wire bytes of every
  existing vector are unchanged, but the decode keys renamed, so the
  corpus baseline was re-accepted; the `reserved-bytes-nonzero` Info
  vector retired with the bytes it tested. SPEC.md §15; RATIONALE §11.
- **Power** — capability bit 8, `GET_POWER` (`0x50`), a four-byte
  `power_state`: `source` and `percent`, independently valid. Polled, not
  pushed. SPEC.md §9.7; RATIONALE §9.
- **GNSS aiding** — capability bit 9, a seventh characteristic written
  without response, opcodes `0x11`–`0x13`. One transfer open at a time,
  named by a token; fixed `chunk_bytes`; `first_missing` so loss is a
  number; CRC-32 stated exactly. SPEC.md §14; RATIONALE §10.
- **Harness** — `power`, `aiding` and `obd` checks; subscription checks
  hold exact slot accounting against Info, an observable governor choice
  between overlapping subscriptions, the equal-specificity tie-break in
  both install orders, and duplicate forwarding across batch boundaries.
  The OBD checks drive the poll loop live: an accepted poll set delivers
  the answers with nothing subscribed and on no identifier the probe did
  not report, the polling flag rides every batch and falls on the stop,
  and the empty poll set actually silences the transmitter. A poll the
  bus legally never answers is reported indeterminate, not failed — §15.4
  makes the gap the truth — and only independent evidence (a refused
  diagnostic re-probe, or answers that appear once the reported
  identifiers are subscribed) turns it into a failure. Every MUST/SHOULD
  is held by a seeded fault or an explicit excuse; 74 matrix faults, each
  caught by the check that claims it, plus two scenario seeds asserting
  the required verdicts on that legally-silent car.
  (`info.reserved_fields` retired with Info's last reserved bytes, which
  §15 assigned.)

### Fixed, in aggregate

Everything the review passes found — contradictory rules stated twice,
hand-copied tables that drifted from the schema, checks that could not fail,
encoder guards without tests and tests without guards — is folded into the
states described above. The per-finding record is in the pull requests.


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
- Conformance corpus: 79 vectors across 7 record types, including must-reject
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
