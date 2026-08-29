# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versioning follows SPEC.md §11: major versions have separate service UUIDs;
minor versions are additive and never change the decode of an existing
conformance vector.

## [Unreleased]

### §6.8: a subscription's schedule belongs to the subscription

Reported by the first firmware implementation, from human and LLM review of
its own source. Six defects; none was reachable by a byte vector, because
every frame involved is well-formed and the defect is in *when* the frames
arrive. Three of them were the specification's fault, and this is what it now
says.

**The key is the `(subscription, identifier)` pair.** "Per matching
identifier, not per subscription" was written to forbid one interval shared
across a masked subscription's identifiers, and it reads as naming the whole
key. Keyed by identifier alone, a subscription's rate limit is destroyed the
moment another subscription matches one of its identifiers: removing the
narrower one lets the broader one forward immediately, though it was installed
throughout and its interval had not elapsed. A once-a-minute subscription
delivered three frames in twenty milliseconds. §6.8 now names the pair, and
says that a subscription §9.2 displaces from an identifier keeps its schedule
for it — §9.2 decides which subscription forwards a frame, not which ones have
stopped applying, and §9.2 now says so too.

**A re-install that changes nothing changes nothing.** §9.1 made an identical
re-install update `mode` and `arg` in place, §6.8 promised the first matching
frame after an install, and §9.4 told clients that retrying a request whose
response was lost is harmless. For a byte-identical retry the three did not
agree, and the difference is a frame inside the client's own rate limit with
nothing on the wire to explain it — a lost response and a delivered one are
identical at the client. §9.4's promise wins: an unchanged `mode` and `arg`
leave the schedule untouched. A re-install that changes either is a new
instruction and re-arms the first frame.

**A bounded device evicts displaced state before it sheds a live
subscription.** §6.8 permitted a bound on per-identifier state and required
shedding at it, without saying what to sacrifice — so a broad slow
subscription could fill the pool and a newly installed exact subscription be
shed forever, never receiving even its first frame, blocked by entries
belonging to a subscription that could not use them. The costs are not
symmetric: reclaiming a displaced entry costs one early frame if governance
returns to it, and refusing to costs a live subscription its entire output.
§6.8 states the order and says the early frame is conformant.

Also in §9.1, both from the same report: the identity is the `(id, mask)` pair
as the client wrote it and not `id & mask`, and a re-install MUST be answered
`ok` on a full table — it creates no subscription, so there is no slot for the
capacity check to refuse it.

No wire change: no field, enum value, UUID or conformance vector moves.
Reasoning in RATIONALE §8.4 and the contradictions section. The reference
peripheral now re-arms on a changed `mode` or `arg` and on nothing else, and
`reference/peripheral/selftest.py` covers the displacement sequence, the
identical retry, the pair identity, the transmission bits and the full-table
re-install.

### Harness: ten checks for the rules a byte vector cannot reach

The same report noted that the conformance harness had nothing for any of
this. It now has, and none of it needs a second CAN node — five ask the
control plane a question, and five watch what the device's own bus traffic
does under a table the harness reprograms:

| Check | Section |
| --- | --- |
| `can.update_in_place_when_full` | §9.1 — a re-install on a full table is `ok` |
| `can.transmission_bits_ignored` | §9.1 — bits 30 and 31 are not part of the identity |
| `can.identity_is_the_pair` | §9.1 — two subscriptions differing only in ignored id bits are two subscriptions |
| `can.unknown_mode_refused` | §6.8 — modes 2, 3 and above are `bad_params` and take no slot |
| `can.no_rate_admission` | §9.3 — a catch-all subscription is not refused on rate grounds |
| `can.periodic_first_then_rations` | §6.8 — the first matching frame, then the interval |
| `can.identical_reinstall_costs_nothing` | §9.4 — a byte-identical retry forwards no frame |
| `can.displaced_schedule_survives` | §6.8 — a displaced subscription keeps its rate limit |
| `can.format_bit_is_identity` | §9.1 — a subscription on the other frame format matches nothing |
| `can.dropped_excludes_declined` | §6.3 — `dropped` counts neither unmatched nor mode-declined frames |

Each has a seeded fault in `transport.FAULTS` that makes it fail, as
`harness/selftest.py` requires. `can.format_bit_is_identity` is the one that
caught a defect in the field: a controller that keeps the frame format in a
flags word rather than in the identifier (Zephyr's `can_frame.flags` among
them) clears bit 29 for every frame on the bus, so extended subscriptions
never match and a standard subscription on the same number delivers another
ECU's traffic.

The scheduling checks measure by bus-arrival timestamp rather than by arrival
window: a batch is flushed on the device's own schedule (§6.2), so frames
accepted before a control request are delivered after it, and counting by
window reads those as new frames.

### §9: owing a response ends at the send

Reported by the first implementation outside this repository, from code review
rather than a field failure. §9 used three words for one idea — a device
"owes" a response, a tag is reusable once its response has been "sent", a
request is refused when one is already "outstanding" — and they agree only
while a single response is in flight. §9 creates the case where they disagree:
the `busy` refusal is itself a response, so a device answering one request and
refusing another is holding two.

**Settled on the send.** A response is owed from the moment its request is
accepted until the device has handed it to the transport with nothing further
to do. The reason is that the client's boundary is the *arrival*: §9 tells a
client to write again as soon as the response reaches it, and ATT permits that
write before the client's confirmation has gone out. Send, arrival and
confirmation are three points in that order, so a device's completion point
has to fall no later than the client's — the send does, the confirmation does
not. A device owing until the confirmation would answer `busy` to a client
that waited exactly as long as §9 told it to, and the retry would meet the
same window. The tag-reuse sentence was therefore right as it stood; §9.4's
deliverability clause and the `busy` obligation now say the same thing in the
same words.

**A `busy` refusal is a response, and waits its turn like every other.** What a
device tracks is a count and not a flag, because a device answering one
request and refusing another owes both until both have gone out.

**One outstanding indication is a reason to hold a response, not to refuse a
request.** A device MUST be able to hold one response beyond the one in
flight, and that slot is not spare capacity: it is where a *conforming*
client's next request lands, having arrived after the previous response
reached it but before the confirmation did. Past that a device has no room and
MUST discard the request unanswered and unapplied rather than apply one it
cannot answer.

No wire change: no field, enum value, UUID or conformance vector moves, and
the `busy` description now says "still owed" rather than "already
outstanding". Reasoning in RATIONALE §8.7, which records that the first draft
of this change chose the confirmation and why that was wrong — the premise
(one outstanding indication per bearer) is true, and refusing rather than
holding does not follow from it.

The reference peripheral already behaved this way. `reference/peripheral/
transport_selftest.py` now drives the case over the real pump, where the send
is `update_value` returning True rather than an event the test supplies, and
`reference/peripheral/selftest.py` covers the admission rule and the two-held
bound directly. The harness loopback models owing as a count, serialises
deliveries one at a time as the link does, and caps what it will hold — six
back-to-back writes now produce two answers and two tasks rather than six.

`control.busy_when_outstanding` is unchanged, with the reporter's finding
recorded in it: ATT's one-request-per-bearer rule means `busy` is only
reachable between the Write Response and the moment the device sends its
answer, so the check Observes rather than verdicts against any device that
answers promptly, and a deeper pipeline would not change that.

**The harness now rejects a `busy` nobody asked for.** Reported by the same
implementation, which had this defect and passed four harness runs with it:
`busy` was asserted on in exactly one place and treated as a pass everywhere
else, so the rule settled above had no check behind it.
`control.no_busy_for_conforming_client` writes forty requests, each as soon as
the previous response has arrived and never more than one outstanding — which
is what §9 tells a client to do — and fails on any `busy`.
`control.no_unprovoked_busy` reads the whole run's control history back at the
end and fails on any `busy` answered to a request that did not overlap another,
which turns every request the harness already makes into a witness for the rule
at no new traffic. Which requests overlapped is read out of the write and
arrival timestamps the correlation layer already records, not declared by the
check that pipelined: `control.busy_when_outstanding` *intends* to pipeline,
and against a device fast enough to answer the first request before the second
is written it does not manage to — so a `busy` there refused a conforming write
like any other, and is reported rather than excused. That check now reaches its
Observe branch on the timing whatever status came back, instead of reading
`busy` as a pass it had not earned. A pass on the first is worth less than a failure: the window it aims
at closes when the host's stack emits its ATT confirmation, and CoreBluetooth
does that on its own schedule without telling the application, so a green run
says the device did not refuse a client writing that fast rather than that its
boundary is right. A failure is unambiguous. Same class of limit as
`control.busy_when_outstanding`'s Observe branch, and it wants a sniffer
rather than a better host.

The `owes_until_confirmed` fault seeds both: the loopback's decrement moves a
round trip past the delivery, which is the defect as reported. A device with it
for real refuses *every* client that writes on arrival, and every request this
harness makes is written on arrival — so the unnarrowed fault fails fourteen
checks and says only which one ran first. It is narrowed the way
`drops_a_response` is and by the same predicate: eligible only on a well-formed
`TIME_SYNC`, and spent on the first refusal it causes. That fixes the set of
checks that meet it at the two named above, with
`control.busy_when_outstanding` still passing, since its `busy` is a genuine
pipeline and correct there. Spending it on the *refusal* rather than on the
first eligible response is what keeps that true — a legitimately pipelined
`busy` must not consume it. Deleting the dedicated check does not silence the
fault: `control.time_sync` sends seven well-formed `TIME_SYNC`s back to back
and meets it next, which is checked rather than assumed.

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
