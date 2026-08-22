# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versioning follows SPEC.md §11: major versions have separate service UUIDs;
minor versions are additive and never change the decode of an existing
conformance vector.

## [Unreleased]

### The harness proved less than it claimed

Reported from the field: a device answering `MONITOR_LIST` in the superseded
paged format with no working `TIME_SYNC`, on a repository whose CI runs the
harness against a peripheral on every push. The device turned out to be a
process started before both changes — but the question it raised was the right
one, and one of the two defects really would have passed.

- **A missing `TIME_SYNC` was reported as a skip.** `control.time_sync`
  answered `unsupported_opcode` with `Skip("TIME_SYNC is not implemented")`,
  on a check declared `severity="MUST"`. §9 gives `TIME_SYNC` no owning
  capability: it is about the clock, which every device has, and reaching it at
  all means Control is live, so there is no device for which that answer is
  correct. `GET_LINK_PARAMS` — the other unowned opcode, and only a SHOULD —
  already treated the identical status as a failure. Now both do.

- **`transport.FAULTS` decided what "detects every defect it claims" meant.**
  The selftest asserted that every fault had a check named against it, which
  holds the fault table to account and nothing else — and the fault table is
  written by whoever wrote the checks. Nothing asserted the converse, so
  **41 of the 66 MUST and SHOULD checks had never once been observed to fail**,
  among them the one covering §13.3's declaration format.

  The selftest now asserts both directions. A MUST or SHOULD must have a seeded
  fault or an entry in `NOT_SEEDED` stating why none is possible; the two lists
  must be exhaustive and disjoint. Twenty new faults were written to satisfy
  it, taking coverage from 25 to 45 — including `monitor_paged_declaration`,
  the reported defect, and `timesync_unsupported`. The 21 remaining entries are
  debts with reasons attached, not dispensations.

  An excuse is a claim about the whole fault suite rather than any one run, so
  it is checked where the suite runs: a fault that makes an excused check fail
  proves the excuse false, whatever check that fault was aimed at. This found
  four more the moment it existed — `info.rate_ceiling`, `control.rate_ceiling`,
  `can.list_beyond_end` and `gatt.inert_control_rejects_writes` were each
  broken by a fault written for something else — and all four now have faults
  of their own instead of a reason. A fault that breaks the conversation rather
  than a rule (`no_tag_echo` leaves nothing correlatable, so every check
  awaiting a response fails) is listed in `CASCADING` and exempt, because
  "failed while the envelope was broken" is not evidence a check works.

- **A skip said nothing, and nothing was watching which ones.** A check that
  quietly stops reaching what it tests — a renamed state key, a capability
  probe that stopped matching, a refusal newly read as "not applicable" — looks
  exactly like a passing run. The clean run is now held to an `EXPECTED_SKIPS`
  baseline, and the reports name how many MUSTs went unverified beside the
  counts rather than only in Not verified. `run.unverified_musts` joins
  `conforms` in the JSON, because a machine has the same way of misreading a
  run that verified nothing.

- **An installed harness carries a snapshot, and nothing compared it.**
  `pyproject.toml` force-includes the schema, the reference decoder and the
  software peripheral into the wheel, so `--loopback` works from an install. A
  wheel built from a stale tree therefore tests last week's peripheral against
  last week's rulebook, agrees with itself completely, and reports green — the
  one way every check here can be right and a developer running the published
  tool still be told a superseded device conforms. `tools/check_package.py`
  compares every bundled file against the source, reading the file list from
  the force-include block rather than restating it; CI builds the wheel, runs
  that, and then runs the packaged harness against the packaged peripheral.

Both new gates have a CI step that breaks them on purpose and requires the
failure, for the same reason the mutation sweep does.

### Review of PR #24, third pass

- **CI was red and my own verification hid it.** `encode_selftest.c` still
  assigned `p.total`, removed with Monitor paging. The sanitised build had not
  compiled since that commit, and I had "checked" it as
  `make -C reference/c san 2>&1 | tail -1` — which reports the exit status of
  `tail`, so `set -e` saw success and the last harmless line of output looked
  like a pass.

  Fixed, and `tools/ci.sh` now runs every check under `set -euo pipefail` so
  nobody has to remember that. It is the same defect this repository keeps
  finding in its own tooling — a check that cannot fail — committed in the act
  of checking.

- **The CAN payload rule contradicted itself and nothing enforced it.** §4.1
  said a device with no CAN reports `can_max_payload` 0 *and* that a device
  without `can_fd` reports 8; a no-CAN device satisfies both premises. Neither
  codec checked any of it, both accepted Classic CAN with 64 and CAN FD with 8,
  and one generated producer case expected an FD device reporting 0.

  The field is gone. Every value it could hold was already fixed by the
  capability bits — 0 with no CAN, 64 with `can_fd`, 8 otherwise — so it was
  a second statement of one fact that two implementations could publish
  disagreeing. §4.2 derives it in three rows, and byte 20 of Info is reserved.

  That change also found a third defect: `encode_info` never zeroed reserved
  fields, because Info had none when it was written. `_zero_reserved` now runs
  inside `_pack` alongside the bitmask normalisation, so a record gaining a
  reserved field is covered without anyone remembering.

- **Devices performed roles they declared absent.** A `VtpDevice` configured
  with only `CAP_CONTROL` emitted GPS and IMU notifications and answered `ok`
  to `CAN_SUBSCRIBE`, `CAN_RESET`, `GPS_SET_RATE`, `IMU_SET_RATE` and
  `MONITOR_LIST`. The capability matrix said what a device MAY do; nothing made
  any of it so.

  **Every opcode now declares the capability that owns it**, in the schema and
  in §9's generated table. A device without the bit answers `unsupported_opcode`
  — and answers it **before parsing parameters**, which §9 now states, because
  `unsupported_opcode` and `bad_params` mean different things to a client
  ("never on this device" against "try better arguments") and a client that
  gets them the wrong way round either retries forever or abandons a device
  that would have worked. The same order applies one level down: an
  `on_change` subscription on a device with no CAN is `unsupported_opcode`, not
  `bad_params`, because the opcode was never available to carry the mode.

  `poll()` produces a stream only when its bit is set, and `monitor_values`
  rejects writes without the Monitor bit. `TIME_SYNC` and `GET_LINK_PARAMS`
  have no owning capability: they are about the link and the clock, which every
  device has.

### Fixed — smaller

- A device asking for more than 15 Monitor channels is refused at construction
  rather than at its first `MONITOR_LIST`, where the traceback would name the
  wrong thing.
- Every macOS launch command uses `open -n`. The README explained in one place
  that plain `open` activates a running copy and silently discards the
  arguments, then used plain `open` in four others — including the new
  smoke-test instructions, which also lacked the app-bundle step macOS needs.

### Simplifications — spending complexity where it earns its keep

Four things this protocol carried because they seemed prudent, each of which
cost every implementer something and bought a hobbyist nothing.

- **One outstanding Control request.** A client writes, waits for the
  indication, and writes again. Gone with the old rule are: the "at least four
  outstanding" floor, the ordering guarantee across pipelined requests, and the
  reasoning about queue depth in §9.6.

  `busy` survives, and deliberately. It is now what a device says to a client
  that pipelines anyway — without it, such a device must choose between silence
  and applying a request it cannot answer, and §9.6 forbids both.

  The `duplicate-tag` refusal is gone entirely, and this is the part worth
  reading. One-outstanding does not *detect* tag ambiguity better; it removes
  the state that made it possible. A second request written before the first is
  answered is refused whatever tag it carries, and one written afterwards has
  nothing to collide with. **A device needs no table of outstanding tags at
  all** — it echoes the tag and forgets it.

- **Empty CAN batches are forbidden.** `t_base` is defined as the bus-arrival
  time of record 0, so a batch with no record 0 timestamps a frame that does
  not exist — precisely the argument that already forbade an empty IMU batch.
  Last release justified the asymmetry by saying a CAN `t_base` "describes a
  bus that was observed and found quiet"; the field's own definition says
  otherwise, and one rule for both batch types is the honest reading. A quiet
  bus is reported by sending nothing.

- **Monitor is not paged, and no longer pretends to be.** §13.4 caps a device
  at 15 channels — the most that fit in one complete client write at the
  minimum ATT MTU — and 15 channels are 62 bytes against the 97 a response
  carries at that same MTU. So `total` could never differ from `count`, `index`
  could never be anything but 0, and `MONITOR_LIST`'s `start` could never be
  anything but 0 either.

  `monitor_page` is now `monitor_declaration`, two bytes instead of six, and
  `MONITOR_LIST` takes no parameters. §9.5's CAN table keeps its paging, and
  the difference is now stated rather than left to look like an oversight:
  `can_subscription_slots` may be far larger than one response can carry, so
  `CAN_LIST` must page and does. Monitor cannot need it.

- **Empty Monitor writes are forbidden** (carried over from the previous
  round's fix, and the same shape as the two above): §13.4 makes every write a
  complete statement of what the client can supply, and a write naming no slots
  is the one thing a complete statement cannot be.

Each of these makes a conforming implementation smaller. None of them makes a
device less capable — every one removes a mechanism whose reachable state space
was empty.

### Review of PR #24, second pass — interoperability

- **C and Python produced different wire bytes.** `can_header.flags`,
  `imu_header.flags` and `info.clock_flags` became schema bitmasks in the last
  round, and the Python encoder picked them up immediately because it applies
  the reserved-bit rule generically by walking the schema. The C encoder writes
  it out per field — it is a separate translation unit with no reflection — and
  three fields were never wired. Crafted input produced `0xfe/0xfb/0xfc` from C
  against `0x00/0x03/0x00` from Python: two conforming implementations
  disagreeing about the bytes, which is the one defect class this repository
  exists to prevent.

  The masks are added, and the corpus is changed so it cannot happen again.
  **`conformance/produce.py` now carries a reserved-bit case for every bitmask
  field in the schema, generated from the schema** — eight where there were
  four hand-picked ones. A bitmask field added later gets a case automatically,
  and each sets the *top* reserved bit, because an encoder masking at the wrong
  width passes on the lowest and fails on the highest. Each of the three fixed
  masks was verified to fail the suite when removed again.

- **CCCDs were excluded from the fixed attribute table.** §4.1 required every
  characteristic and its Notify/Indicate properties, then made the CCCD
  conditional on the capability bit — and a CCCD is an attribute, so removing
  one changes the table a central has cached, which is the whole reason the
  table is fixed. Every CCCD is now always present. A client enables the ones
  whose bit is set; a device MUST accept a CCCD write on an inert stream (it
  costs a two-byte descriptor) and then simply never notifies.

- **The three optional CAN bits defined nothing.** §4.1 said only that they
  require `can`. Each now has a rule for when it is clear, in a table:
  `can_fd` clear means never emitting an FD record and `can_max_payload` of 8
  (64 when set); `masked_subscriptions` clear means `CAN_SUBSCRIBE_MASK`
  answers `unsupported_opcode`; `on_change_subscriptions` clear means an
  `on_change` subscription is refused with `bad_params` rather than silently
  becoming `every_frame` — the difference between a channel that updates on an
  event and one that floods, which the client would have no way to detect.

  `VtpDevice` takes a capability set so both halves of each rule can be
  demonstrated, and `selftest.py` exercises the cleared half. That immediately
  found a second defect: `info()` reported `gps_rate_hz`, `imu_rate_hz` and the
  CAN capacities unconditionally, so a device declaring no GPS still published
  a GPS rate. §4.1's capacity rule had been in the encoder since the last round
  and had never run against a build that declared anything less than
  everything; the encoder refused it the moment one existed.

- **The empty Monitor write contradicted complete snapshots.** §13.4 makes
  every write a complete statement of what the client can currently supply,
  while `monitor/empty-update` asserted that `count` of zero MUST be accepted —
  and the reference peripheral rejected it. `count` of zero is now forbidden
  and the vector is a rejection. A client with nothing to supply writes every
  slot with the `present` bit clear, which is a complete statement and expires
  correctly; a client with nothing to say does not write at all.

### Fixed — the radio smoke test

- **BlueZ reports `mtu_size` of 23** through this bleak property whatever the
  link negotiated — it is the ATT default, not a measurement — so the floor
  check failed every healthy Linux link. A value of exactly 23 is now reported
  as "not measured on this backend" rather than as a failure; the
  notification-size checks against the published ceiling still run, and they
  are the ones that matter.

- **The documented commands mixed two working directories.** Everything is run
  from `reference/peripheral` now, so the requirements install and the script
  agree about where they are.

- **Pairing is handled explicitly.** `serve.py` requires an encrypted link by
  default (§10). macOS pairs on demand; BlueZ and WinRT answer *Insufficient
  Authentication* and bleak raises. The smoke test recognises that on the three
  operations where it surfaces and prints the pairing command for the platform
  rather than reporting a protocol fault — and says so specially when it is
  *Info* that is encrypted, which §10.2 says to avoid precisely because a
  client that cannot pair then cannot identify what it found.

### Review of PR #24 — checks that gave misleading results

Four of these sat behind a green CI run, which is the reason they matter more
than their size suggests.

- **Producer conformance could certify broken output.** The C adapter never
  compared the payload supplied against the declared CAN `len`, so `len` of 8
  behind one byte silently padded seven zeroes onto a bus signal and `len` of 0
  behind one byte silently discarded it — both answered `ok`, while the Python
  encoder refused both. The adapter reshaping its input is the exact defect the
  producer suite exists to find, one layer further out. Two mismatch vectors
  now pin both directions.

- **`conformance/produce.py` ignored the implementation's exit status.** A
  wrapper printing all correct answers and then exiting 7 was reported as a
  pass. The runner deliberately tolerates *short* output, so a crash is
  attributed to the case it happened on rather than invalidating the run, and
  that tolerance had quietly swallowed the exit status too. They are separate
  questions and both are asked now. (`run.py` already checked; `produce.py`
  did not.)

- **The real-radio smoke test could false-pass and false-fail.** It collected
  GPS, CAN and IMU one after another and then required their device-clock
  windows to overlap, which a healthy device cannot do — three streams gathered
  in series have three disjoint windows. All three are now subscribed together,
  gathered once, and stopped. Conversely an empty stream was only a note, so a
  device that connected, answered Info and sent nothing at all passed. GPS and
  IMU silence is now a failure when Info reports a non-zero current rate; CAN
  silence stays a note, because a real bus can be quiet — but the
  `CAN_SUBSCRIBE` that gates it is checked, where it used to be written and
  slept on. No data on any stream is a failure outright.

- **Monitor freshness still had two rules and a third to reconcile them.**
  `max_age` of zero meant "no deadline of its own", and a derived device-wide
  *liveness bound* — the largest deadline declared — then expired those
  channels anyway. The canonical four-channel vector satisfied none of it, with
  every channel at zero. **Every declared channel now MUST carry a non-zero
  `max_age`**, and the liveness bound is gone. A channel that changes rarely
  takes the 25.5 s ceiling rather than an exemption. Both decoders reject a
  zero deadline, both encoders refuse to emit one.

- **The connection-race fix claimed more than it delivered.** `serve.py` treated
  bless's `is_connected()` as a physical link edge. In pinned bless 0.3.0 that
  method returns `len(_central_subscriptions) > 0` — "at least one central is
  subscribed" — because a CoreBluetooth peripheral is never told about a
  connect or a disconnect at all; the delegate has `didSubscribe` and
  `didUnsubscribe` and no connect/disconnect pair.

  The behaviour is kept and the claim is corrected, because the two possible
  mistakes are not equal: resetting on a resubscribe costs a CAN table and a
  `seq` restart the client must already tolerate and can see, while failing to
  reset on a real reconnection hands the next connection the previous one's
  state in a way §8.2 and §9.2 exist to prevent and no client can detect. The
  tracker now carries the central's identity so the log can say which it
  probably was, `gattsim.py` can reproduce the backend's semantics
  (`bless_semantics=True`), and `transport_selftest.py` pins the behaviour.
  `reference/peripheral/README.md` has the table of what this backend can and
  cannot tell you.

- **Inert characteristics were too expensive.** §4.1 required a GPS-only device
  to answer `unsupported_opcode` on Control, which means parsing opcodes and
  implementing indications for a role it does not have. An inert characteristic
  now **rejects writes with an ATT error** and implements nothing else, and the
  CCCD requirements are conditional on the capability bit. A GPS-only build is a
  service declaration, four inert attributes and one notify path.

  The same section forbade properties beyond those listed and then permitted a
  readable `gps` two sentences later. A device MUST expose at least the listed
  properties and MAY expose more; a client MUST NOT rely on any that are not
  listed.

### Fixed — smaller

- **Appendix A is generated.** It was hand-written and had drifted: it listed
  `fix_flags` bits 4–7 as reserved after bit 4 was assigned to
  `solution_epoch`. A table restating what the schema already says is a table
  that drifts.

- **`can_header.flags`, `imu_header.flags` and `info.clock_flags` are proper
  schema bitmasks.** They were plain `u8` fields with a prose description, so
  nothing derived their reserved ranges: Appendix A listed them by hand and
  neither encoder masked them, which is the one rule SPEC.md §2 states about
  reserved bits. Both encoders now zero them from the generated masks.

- **The RTK combinations are enforced, not just described.** `rtk_float` and
  `rtk_fixed` are mutually exclusive and either implies `differential`; both
  decoders reject a fix that breaks either rule and both encoders refuse to
  produce one. The prose said "treat the pair as unknown" while the codecs
  rejected — the specification now says reject, consistently with every other
  self-contradictory record.

- **`reference/peripheral/requirements.txt` includes the root requirements.**
  It named only `bless`, so the documented standalone install failed on
  `import yaml` at the first line that mattered — and the peripheral's own
  README told people to run exactly that. Same for the new client requirements.

- **A length vector stopped isolating its own rule.** Requiring a non-zero
  `max_age` gave `monitor/long-page` a second reason to reject, which masked
  the trailing-bytes check from the mutation sweep. Caught by the sweep, which
  is what it is for; the vector now carries a valid deadline so only its length
  is wrong.

### Changed

- **SPEC.md is a specification again.** The postmortems — "this used to say
  X", "the runner enforced a rule the specification did not state" — are the
  reasoning behind a rule, not the rule, and they belong where the reasoning
  lives. They have moved to RATIONALE.md, which gains a section on the
  contradictions this review closed and one on what the reference peripheral's
  backend cannot observe. The history stays here in the changelog.

### The consistency pass — third review

The reviewer's second pass was made against a production standards-body bar and
then explicitly recalibrated to the right one: *can two reasonably competent
hobbyists implement this independently, connect successfully, and understand
failures without reading the author's mind?* This release is that list, plus
the parts of the earlier one that were already done and are worth keeping.

Contradictions come first, because a contradiction is the only defect here that
can produce two conforming implementations that cannot talk to each other.

### Fixed — contradictions that could produce incompatible implementations

- **Nothing said whether CAN or Monitor requires Control.** The specification
  defined every capability bit independently, while `conformance/run.py` had a
  hard-coded table making `can` and `monitor` imply `control` — the runner
  enforcing a rule the specification did not state. Canonical Info vectors
  meanwhile blessed a CAN device with no Control characteristic, which no
  client could install a subscription on. `conformance/README.md` compounded it
  by calling Control bit 3; it is bit 4.

  **SPEC.md §4.1 is now the one place any of this is stated**, generated from
  `schema/vtp1.yaml`: capability implications, the attribute table, GATT
  properties, CCCD requirements, write type, direction, and which capacity
  fields must be zero behind a cleared bit. `can` and `monitor` require
  `control`; `can_fd`, `masked_subscriptions` and `on_change_subscriptions`
  require `can`. Both reference decoders reject an Info that breaks the matrix,
  both encoders refuse to produce one, and `run.py` reads the implications from
  the schema instead of asserting its own.

  **The attribute table is fixed.** Every VTP/1 device exposes every
  characteristic; a role it does not implement is inert rather than absent. The
  alternative fails for a mundane reason: central stacks cache the attribute
  table across connections, so a device whose table changes hands the client a
  stale handle, and the symptom is a read of the wrong characteristic rather
  than a missing one.

- **`max_age` of zero meant two things.** The generated field description said
  "0 never expires"; §13.5 said a zero `max_age` means no deadline *of its own*
  and that the device's liveness bound still applies. The prose is the half
  with the argument behind it, and the schema now matches it.

- **A zero-channel Monitor declaration was legal and forbidden at once.** The
  corpus carried one; §13.5 required a non-zero `max_age` on at least one
  channel, which a device with no channels cannot satisfy. §13.5 now says a
  device MAY declare no channels, that such a device has no liveness bound, and
  what a client does about it.

- **Device Information was a MUST in §2 and a SHOULD in §3.4.** An implementer
  reading one built it and an implementer reading the other did not, and both
  were conforming. It is a SHOULD, specified in §3.4, and §2 now says so.

- **Rate setting was undefined in four ways.** New §9.8 states them: `hz` of 0
  stops the stream and is not an error; a rate the device does not support is
  `bad_params` and MUST NOT be silently rounded to a neighbour; a rate above
  the published ceiling is `rate_exceeded`; the applied rate is read back from
  Info rather than returned in the response; and the change takes effect within
  one notification, with no batch spanning it.

  There is deliberately no way to enumerate supported rates. Asking and finding
  out is one round trip on a link the client already has, and a discovery
  mechanism would be a list format and a second thing to keep in step with
  Info.

- **`rate_exceeded` still described CAN**, whose rate refusal §9.4 forbids
  outright. It names the two rate setters now.

- **`TIME_SYNC` declared a parameter.** `params` in the schema held a literal
  em-dash — the *display* form of "no parameters" written into the source of
  truth — so the schema said the opcode took one parameter whose name and type
  were both `-` while §9 said it was parameterless. The generator now refuses
  the dash outright.

- **`rtk_float` and `rtk_fixed` could both be set.** The natural client reading
  of that pair is "fixed wins", which upgrades an accuracy claim on the
  strength of a bug. They are mutually exclusive, both-set decodes as neither,
  and either implies `differential`.

- **An IMU batch could carry no samples.** `t_base` is defined as the
  acquisition time of sample 0, so an empty batch timestamps a sample that does
  not exist. `count` of zero is now rejected by both decoders and refused by
  both encoders. §6's CAN batch still permits it, and the difference is stated:
  a CAN `t_base` describes an observed bus, an IMU `t_base` describes a sample.

- **A FIFO discontinuity silently corrupted every later timestamp.** Samples
  are derived as `t_base + i × period`, so a gap mid-batch shifts everything
  after it — silently, and increasingly. §7 now requires a device to end the
  batch at the discontinuity and reanchor, counting the loss in `dropped`.

### Fixed — the C reference encoder

- **Four malformed-input crashes.** `vtp_encode_can_batch` read `frames[0].dt`
  before checking `frames`; `vtp_encode_monitor_list` and
  `vtp_encode_monitor_update` ran their duplicate-slot sweeps before checking
  their arrays; `vtp_encode_imu_batch` reached a sample only through a set
  presence flag, so one malformed call crashed or quietly emitted a batch of
  zeroed samples depending on one bit of the header. All reproduced under
  ASan/UBSan first.

- **A refusal left the caller's buffer modified.** `vtp_encode_can_batch`
  validated the arbitration identifier inside its write loop, after the header
  had gone into the buffer, contradicting the file's own documented "nothing is
  written on -1" — and leaving the previous notification's bytes readable
  behind a call the caller believes produced nothing.

### Fixed — the reference peripheral

- **A connection's first Control request could be applied and then erased.**
  The pump polled `is_connected()` once a tick and ran the connect edge from
  what it found. A GATT write is not polled: connect, enable indications, write
  `CAN_SUBSCRIBE` can all land before the next poll. The request was admitted,
  applied and queued — and then the pump noticed the connection it had already
  been serving and cleared the queue and the device state out from under it.
  The client's subscription had taken effect and was never answered, so it
  retried a request that was already installed.

  This is the ordinary path: no stall, no reconnection, nothing refused. The
  transport self-test previously stepped the pump five ticks before writing and
  described the loss as correct. The connection edge is now taken by whichever
  comes first — a GATT callback, which is proof the link exists, or the poll —
  and the test asserts the request survives.

- **999 m was displayed as `999 km`.** The unit is part of the static cell
  label and cannot change per value, but the formatter switched to bare metres
  below 1 km. A thousand-fold error, rendered confidently, on the one screen a
  driver reads at speed. Always kilometres to three places now.

- **`peripheral_latency` of 0 read as "unknown".** The grouped-validity check
  tested truthiness rather than presence, so the whole connection-parameter
  group reported absent for the value §2 says a device SHOULD request while
  streaming.

- **Negotiated link state outlived its link.** The MTU and PHY a central
  negotiated stayed in Info and `GET_LINK_PARAMS` until something replaced
  them, so the next connection read the previous one's numbers back with the
  validity bits set — which assert they are measurements of the link being
  asked about.

### Changed

- **`max_notify_bytes` is a device ceiling, not the negotiated ATT payload**
  (new §4.2). The two readings look interchangeable and are not: a client reads
  Info as its first act after connecting, and a peripheral commonly does not
  learn the negotiated maximum until a central subscribes, which is strictly
  later. Defined as the live value it was a field whose correct answer did not
  exist yet at the only moment anyone read it. Defined as a ceiling it always
  has one, the device never exceeds it, and the negotiated value stays
  available from `GET_LINK_PARAMS` — a request made *after* subscribing.

- **`dropped` is explicitly a best-effort diagnostic** (§8.3). It exists to
  separate "my link is bad" from "the device is overrun" and to put a number on
  the second; it is not an audit trail and MUST NOT be used to reconcile
  counts. A device MAY report a discard in the next notification rather than
  the one it strictly belonged to. Attributing every lost item to exactly one
  notification would mean owning the counter transactionally across encoding,
  transmit-queue refusal and supersession — three places a firmware author
  would have to get right, to make a diagnostic exact. `seq` is the field with
  the exact guarantee, and it is exact because it is cheap to be.

- **Reserved bits of a bitmask are normalised on transmit.** SPEC.md §2 was
  already applied to whole reserved *fields*; the reserved *portion* of a
  bitmask had no expression anywhere, so a capabilities word with bit 19 set,
  or a GPS validity word with bit 30 set, was transmitted verbatim. Those bits
  are the only ones on the wire a later minor may redefine. The masks are
  generated from the schema, and the three vectors that carry a reserved bit
  are now non-canonical: a decoder must ignore the bit, an encoder must
  normalise it away, and each vector asserts both.

### Added

- **`conformance/produce.py` — producer conformance, language-neutral.** It
  replaces `tools/check_encoders.py`, which imported the Python encoder as a
  module. A green producer run was therefore a statement about one of this
  repository's two reference encoders, and the C encoder's four crashes and
  contract violation sat behind a green run for as long as they existed,
  because none of them was ever called.

  The runner drives a subprocess over a text contract, exactly as the decode
  runner does, with adapters for both references (`reference/c/vtp1_producer`,
  `reference/python/vtp1_produce.py`). It takes `--roles` and reads the same
  role table. **A crash is not a refusal**: an implementation that dies partway
  through has answered nothing for the case it died on, and every unanswered
  case is a failure rather than an assumed refusal.

  Cases that must encode may now pin their bytes with `expect_hex`, generated
  from the schema's field offsets rather than from either encoder, so two
  implementations agreeing on it are agreeing with the source of truth.

- **`reference/c/encode_selftest.c` — the C API contract, under ASan/UBSan.**
  Every producer case travels as JSON, so every array it describes exists; two
  of the header's promises are therefore unreachable from there and both were
  broken. `make -C reference/c san` runs this plus the whole suite sanitised.

- **`reference/peripheral/smoketest.py` — a real client, over a real radio.**
  The one thing this repository could not test, and the one gap the README now
  names outright. It discovers by service UUID, checks Info against §4.1,
  checks the negotiated MTU against §2's floor and every notification against
  the published ceiling, writes `TIME_SYNC` and waits for a real indication,
  decodes all three streams with the reference decoder, checks they share one
  device clock, and reconnects to check §8.2's per-connection restart.

  Its decode-and-inspect half runs in `selftest.py` against the software
  device's own output, with a deliberately failing case, so the script pointed
  at unfamiliar hardware is known to work on known-good input. **Its BLE half
  has never met an adapter.**

- **Schema validation before generation.** `protocol.endianness`, version and
  MTU ranges, opcode values and the `name:type` parameter grammar, and the
  whole §4.1 profile block: unknown capabilities, CCCDs declared without the
  matching property, writable characteristics with no write type, an allocated
  UUID with no profile row. It caught the `TIME_SYNC` em-dash on its first run.

- **`tools/check_docs.py` checks the producer count** as well as the vector
  count. The producer corpus was a second corpus with a second stated size and
  nothing checking it.

### Not done, and deliberately

- **The real-radio smoke test has not been run.** No adapter was available. The
  script and the two-machine procedure exist; the README status table says
  plainly that nothing here has been over the air.

- **No second independent implementation.** Still the honest measure of a
  protocol's maturity, and still absent — but for a hobbyist protocol that is a
  reason to publish and find out rather than a reason to wait.

- **The specification patent gap is recorded, not resolved.** It needs a
  lawyer, and it should not stop anyone experimenting in the meantime.

## Earlier unreleased work

### Fixed — the producer direction
Milestone 3 of the second review. Every defect reproduced before it was fixed.

- **An out-of-range CAN identifier was masked, not refused.** `0x3FFFFFFF`
  became `0x1FFFFFFF` and `-1` became `0x1FFFFFFF` too, so two different
  mistakes produced one frame the caller never asked for — on the field a
  client uses to decide what the payload means. Both encoders now refuse. The
  format is carried by `extended`, not by high bits of `id`.

- **Reserved fields were transmitted rather than zeroed.** SPEC.md §2 requires
  them to be zero **on transmit**, and both encoders wrote the caller's value
  through. The reasoning recorded at the time — that a later minor might have
  been assigned those bytes, and erasing them would be worse — does not hold
  for a 1.0 encoder: a build that knows what the bytes mean is a build that
  names them. Until then, writing them through let a caller put arbitrary
  content into a field every conforming receiver is required to ignore.

  `reserved-nonzero` is now deliberately non-canonical, and is the only vector
  asserting both halves of the rule at once: a decoder must ignore the bytes,
  and an encoder must normalise them away.

- **Monitor encoders emitted declarations their own decoders reject** — a
  repeated slot, and a `total` beyond what fits in one complete write. Both
  were already decoder rules, so the corpus could see the result and never the
  cause.

### Added
- **A producer conformance corpus** (`conformance/encoders.json`,
  `tools/check_encoders.py`). Everything else here tests decoding, and what it
  said about encoders came entirely from round-tripping payloads that had
  already decoded — which asks whether an encoder reproduces something valid
  and never whether it declines something invalid.

  That gap is where the identifier-masking bug lived, and no byte vector can
  reach it: the point is that the wrong bytes are never produced. Fourteen
  cases carry structured input instead, twelve that MUST be refused and two
  that MUST encode, so a harness that refuses everything cannot pass either.
  Verified to fail when the identifier check is removed.

- **`control` is a role of its own, and `core` is only Info.** SPEC.md §12 asks
  an implementation to pass the vectors for the roles it declares, and the
  Control characteristic is a capability (§4 bit 3) rather than a requirement —
  but `core` carried the control-plane records, so `--roles gps` demanded a
  Control characteristic from a GPS-only device the specification permits not
  to have one. Its only options were to fail conformance for records it had
  never claimed, or to implement things it does not support.

  A GPS-only device now runs 28 vectors rather than 47. `can` and `monitor`
  imply `control`, because their tables are reached through it.

### Fixed — the reference peripheral
Milestone 2 of the second review. Every defect was reproduced before it was
touched.

- **A sequence number is now assigned when a notification is delivered, not
  when it is encoded.** SPEC.md §8.2 counts notifications *sent*, which makes
  seq a fact about delivery, and owning it at encode time produced two bugs in
  succession: a notification nobody had subscribed to burned a number and never
  returned it, so the first one actually delivered carried **2**; and returning
  a number on refusal handed back one a later notification had already taken,
  so a superseded batch produced the delivered sequence **1, 1, 2, 3**. The
  second was introduced fixing the first, which is the clearest possible sign
  the number was being owned in the wrong place.

  A payload is encoded with a placeholder, `stamp_seq` writes the pending
  number into it, and `commit_seq` advances the counter only once the stack has
  taken it. A refusal consumes nothing, so there is nothing to give back and
  `_return_seq` is gone.

- **The connection edge is handled before anything is sent.** It ran after the
  control drain and the telemetry pump, so a notification built for the
  previous central could be handed to a new one in the same tick that reset the
  device. Both edges now go through one `_reset_transport_state()`, so neither
  can clear half the per-link state and leave the rest.

- **A Monitor write must carry every slot the device asked for.** §13.4 has
  required a complete snapshot since it was written; the peripheral merged a
  subset into its previous state, so an omitted slot kept both its value *and*
  its timestamp and stayed on screen looking current while the client had
  stopped saying anything about it — the exact failure the snapshot rule
  exists to prevent.

- **A duplicate tag no longer bypasses the control queue's capacity.** The tag
  was tested before the depth, so one reused tag held a thousand responses
  against a declared depth of four. A duplicate-tag refusal is still a response
  and still needs somewhere to sit.

- **`CAN_RESET` and `GET_LINK_PARAMS` reject trailing parameters.** A malformed
  `CAN_RESET` cleared the subscription table anyway.

- **A grouped validity bit is set only when every field it governs is known**
  (§9.1). Told a TX PHY and nothing else, the device reported the same value as
  the RX PHY with a validity bit asserting it was a measurement.

### Added
- **A fake GATT link and a transport conformance suite**
  (`gattsim.py`, `transport_selftest.py`). Every bug above reached a real phone
  before anyone noticed, because none is reachable by a conformance vector and
  none is in the device model. The suite drives the **real** pump — a
  reimplementation would be a second state machine, and these were all bugs in
  the ordering of the first — against a fake that models the four things which
  decide its behaviour: connection state, CCCD subscription, backpressure, and
  what reached the wire.

  It covers the first delivered notification carrying 0, contiguous unique
  numbers through a stall and recovery, a reconnection restarting from 0 and
  receiving nothing built before the drop, and a refused control response being
  retried rather than dropped. Verified to fail when the sequence commit is
  moved back ahead of delivery.

- **A known limitation, recorded rather than fixed**: connection state is
  discovered by polling, so a central that drops and reconnects between two
  polls produces no edge at all and the device never resets for it. At 200 Hz
  that window is 5 ms and no BLE stack completes a reconnection inside it, so
  it is unreachable on hardware — but it is a property of how the state is
  discovered rather than of the timings that make it safe.

### Fixed — the checks themselves
Three device selftest assertions were passing without testing anything once the
rules around them changed, and are now written so they cannot: a monitor write
that had become incomplete was rejected while the check after it still passed
on the *previous* display; a duplicate-tag check tested a tag that had already
left the queue; and a stray-slot check wrote only the stray slot, which is now
incomplete for a different reason.

### Changed — wire format
Normative closure, from a second external review. **Four existing conformance
vectors changed meaning**, which `tools/check_baseline.py` caught and which was
accepted deliberately: this is not a minor version. Pre-1.0 that is ordinary,
and it is the first time the baseline added in the previous release earned its
place.

- **`TIME_SYNC` is parameterless, and its timestamps have units.** The request
  carried the host's UTC time in milliseconds; the response carries device
  microseconds; and §9.7's equations subtracted one from the other. A
  millisecond count since 1970 cannot be subtracted from a microsecond count
  since the device booted, so the exchange as specified was not implementable —
  and the reference peripheral validated the field's length and then discarded
  it, which was the only thing it could do.

  All four timestamps are now **microseconds on a monotonic clock**: *t₁* and
  *t₄* on the client's, `t_device_rx` and `t_device_tx` on the device's.
  Neither is UTC and neither has to be; mapping the client's clock to wall time
  is a host concern, and a fix's `t_utc` remains the separate mechanism for
  relating the *device* to wall time. **`offset` is stated as device minus
  client**, because the sign is the half an implementer cannot check against
  reality — get it backwards and every timestamp is wrong by twice the offset
  and looks entirely ordinary.

- **§5.6's GPS rule is conditional, not two contradictory MUSTs.** It required
  `t_device` to be the solution epoch *and* required a device that cannot
  determine the epoch to stamp arrival. Both were unconditional. `fix_flags`
  bit 4 now decides: set means solution epoch, clear means arrival time, and
  `t_utc` refers to the solution epoch either way. The flag exists precisely
  because the requirement cannot be unconditional.

- **Record 0's `dt` MUST be zero, and is now enforced.** §6.1 has always
  defined `t_base` as record 0's arrival time, which makes its offset zero by
  definition — and four vectors carried first offsets of 5, 1, 20 and 2 while
  both reference decoders accepted them. That is how a definitional rule
  survives as prose without ever becoming a rule. `t-base-near-wrap` gained a
  second record to keep testing the wrap it was written for.

- **Rate admission is gone (§9.4).** A device MUST NOT refuse a CAN
  subscription on rate grounds; it admits and sheds. The prediction could not
  be made in three separate ways: never for `every_frame` or `on_change`, which
  was acknowledged from the start; `every_nth` with N of 1 selects exactly what
  `every_frame` selects, so the same subscription was accepted or refused
  according to spelling; and a masked subscription schedules per matching
  identifier (§6.8), so a `periodic` mask over ten identifiers produces ten
  times the rate its `arg` names. The rule was removed rather than patched a
  third time. `rate_exceeded` survives for `GPS_SET_RATE` and `IMU_SET_RATE`,
  where the limit is the device's own and the answer is a fact.

- **`max_age` of zero no longer means immortal (§13.5).** It means the value
  has no deadline of its own. A device MUST declare a non-zero `max_age` on at
  least one channel, and the largest it declares is a **liveness bound**: when
  no write at all arrives within that, every value goes unavailable including
  the zero-`max_age` ones. "Never expires" otherwise defeated the rule it sat
  beside — if the client is gone, a best lap from a session that has ended is
  as wrong as a stopped lap timer, and only less obviously so.

- **A grouped validity bit MUST NOT be set unless every field it governs is
  known (§9.1).** `conn_params` covers three values and `phy` covers two, and a
  device knowing one of a pair has not learned the other. Half a group is the
  same state as none of it, and a clear bit is the honest encoding of it.

### Added
- **A compatibility baseline (`conformance/baseline.json`).** SPEC.md §12
  promises that an existing vector never changes meaning within a major
  version, and nothing enforced it. The corpus is *generated*, so a schema edit
  can quietly change what an existing case expects and every other check in
  this repository still passes — the generator, both decoders and the runner
  would simply agree with each other about the new answer. The corpus is
  compared against itself; a baseline is the only thing that compares it
  against what it used to say.

  `tools/check_baseline.py` hashes what an implementation must *do* — bytes,
  expected decode, reject reason — and deliberately not the prose, so
  descriptions can be improved without tripping it and a trip always means
  something real. CI runs `--check`, which never writes and fails both on a
  changed case and on a case the baseline does not yet cover: a mode that
  records additions would pass in CI by discarding the write, leaving the
  baseline protecting only the cases it started with while appearing to cover
  them all.

  Verified against all three failure modes: an altered expectation and a
  removed case both fail; a reworded description does not.

- **Pinned dependency manifests.** CI ran `pip install pyyaml` unpinned. The
  YAML parser sits upstream of every generated artefact in the repository — the
  spec tables, the C header, the corpus — so a silent change in how it orders
  mappings or coerces scalars lands in output that CI then compares against
  itself and finds consistent. `requirements.txt` pins it; CI installs from it.
  `reference/peripheral/requirements.txt` covers the software peripheral, which
  nothing else depends on.

### Documented
- **The licence split has an unresolved patent problem, and now says so.**
  README already explained that Apache-2.0 was chosen over MIT for its express
  patent grant, because "a protocol specification without one is a
  specification a commercial vendor's legal review stops at". That reasoning
  argues for a grant on the *specification* — and the specification is the half
  that does not have one. CC BY 4.0 grants copyright permissions and expressly
  withholds patent rights, so the exposure sits exactly where the licence is
  silent, while `reference/`, which nobody has to ship, carries Apache-2.0 §3.

  `schema/` compounds it: listed as specification text under CC BY, it is also
  the source that generates a C header and the corpus, both under Apache-2.0.

  Recorded rather than resolved. The options — Apache-2.0 throughout, moving
  `schema/` under it, or a separate patent non-assertion covenant — are a
  question for a lawyer, and the note exists so nobody mistakes the current
  split for a settled decision.

### Added
- **§7.1 — IMU axes and signs.** The sensor frame is the device's own and
  vehicle alignment stays the client's job, but how to *read* the numbers was
  never stated and a client cannot infer any of it. The frame is right-handed;
  the gyroscope follows the right-hand rule; and the accelerometer reports
  **specific force**, so a device at rest reads **+1000 mg on whichever axis
  points up**.

  That last one is the one that matters. Both signs are in use in the wild —
  some parts and libraries report the gravity vector, the exact negative — and
  a client that assumes the wrong one sees a car braking when it is
  accelerating. The mistake survives every plausible sanity check, because the
  magnitudes are right.

  The peripheral had `"az": 1000` with the comment `# 1 g down, level car`
  beside it: the value correct under one convention and the comment describing
  the other, in the reference device, which is as good an illustration as the
  ambiguity deserves.

- **§7.2 — saturation.** `i16` at these scales gives ±32.767 g and ±1638.35
  °/s, and a real part rails well before either. A reading at the rail is "at
  least this much", not "this much". `imu_header.flags` bit 2 is now assigned:
  set when any sample in the batch is at or beyond its sensor's range, and a
  client MUST treat such a batch as a lower bound and SHOULD NOT integrate it.

  Per batch rather than per sample because `imu_sample` is deliberately closed
  (§11.3) and twelve bytes at 833 Hz is the one stream where a per-sample byte
  costs real airtime. Saturation is explicitly **not** absence: the presence
  bit stays set, because the sensor is fitted and did report — what is in doubt
  is the magnitude, not whether there is one.

- **§5.4 — coordinate ranges.** Where its validity bit is set, `lat` MUST be
  within ±90°, `lon` within ±180°, and `head_mot` within 0° to 360° exclusive.
  Both decoders rejected none of these; a latitude of 91° decoded happily.
  Rejected rather than clamped, under §1.1: 91° is not a place a clamp could
  move closer to, it is a corrupted field — and every other field in the record
  came from the same bytes. Clamping to 90° puts the vehicle at the pole and
  lets the client draw it there.

  The rule applies only where a validity bit claims the field means something.
  A vector pins that boundary: an out-of-range latitude with the position bit
  clear MUST still decode, because §5.1 puts the duty to write zero on the
  device rather than the duty to police it on the receiver.

### Fixed
- **`period` zero was accepted by both decoders and emitted by both encoders**,
  though §7 has always forbidden it. Zero says every sample in the batch was
  taken at the same instant, which describes no measurement, and a client
  recovering a rate from it divides by zero.

- **The two references disagreed about saturation.** The Python decoder
  reported a `saturated` field the C one did not, and the runner only checks
  keys the vector names — so the asymmetry passed. Both report it now and the
  corpus checks it, which is the same class of gap `tools/mutate.py` was
  hardened against.

- **The datum was already stated** by the documentation pass, so §5's WGS-84
  requirement needed nothing here.

### Changed — wire format
- **A Monitor value now expires (§13.5).** The rule had been the opposite:
  there was no minimum rate and a device MUST NOT infer that an un-updated
  value had gone stale. The reasoning — a client sends nothing precisely when
  nothing has changed — is true, and is not the failure that matters.

  The failure that matters is a client that stopped sending because it
  crashed, was backgrounded or wedged. The link is still up, so the device
  sees nothing wrong; silence is the only symptom it ever gets. Under the old
  rule such a device displayed a lap time from four minutes ago, indefinitely,
  and the driver reading it had no way to tell. A stale value shown as current
  is a plausible wrong value, and the display is the worst place in this
  protocol to allow one.

  `monitor_channel.reserved` becomes `max_age`, in 100 ms units, taken from the
  byte Appendix A had reserved for per-channel metadata. A device MUST render a
  value as unavailable once `max_age` has passed, exactly as it renders one
  whose `present` bit is clear; 0 never expires. Per channel because the
  channels differ in kind — a `lap_time` ticking up is wrong within a second of
  going stale, a `best_lap_time` stays true until it is beaten — and a single
  device-wide timeout would be set for the most perishable and then demand
  pointless traffic for the rest.

- **Every Monitor write MUST now carry every slot the device asked for
  (§13.4).** A write is a complete statement of what the client can supply, not
  a set of changes to what it said before. Complete writes cost almost nothing
  at any plausible channel count and buy two things deltas do not: a lost write
  changes nothing permanently, so `seq` gaps need no recovery procedure and
  there is none to get wrong; and the client never has to remember what it last
  sent, which is the state that diverges silently when an app is backgrounded
  and resumed.

- **A slot MUST appear at most once in a write, and once in a declaration.**
  Nothing said which of two values for one slot wins, so a device choosing
  either was choosing on every client's behalf. Both decoders reject it.

- **A device MUST NOT ask for more than 15 channels (§13.4)** — as many values
  as fit beside a header in one write at §2's minimum ATT MTU. Complete writes
  are only a workable rule if a complete write always fits, and a device asking
  for more has made the rule unsatisfiable rather than made itself more
  capable. The constant is derived from the record sizes in both references
  rather than restated.

### Fixed
- **The peripheral held Monitor values forever.** It now stamps each write and
  reports a value past its channel's `max_age` as not present, so the debug
  panel goes blank rather than lying — the behaviour a real device's screen
  must have. Its declaration carries a deadline per channel: 1 s for speed, 2 s
  for the ticking lap timers, never for a best lap or lap number.

### Changed — wire format
- **`TIME_SYNC` now answers with two readings of the device clock (§9.7).**
  It had answered with one, and one timestamp cannot bound its own error. A
  client knows when it wrote and when it heard back, but with a single device
  reading it cannot separate the outbound delay from the inbound one, so its
  estimate of the device clock is uncertain by the whole round trip — tens of
  milliseconds over a link with a 30 ms connection interval, in an exchange
  whose purpose is to align a microsecond clock.

  The response is now a `time_sync` record carrying `t_device_rx` and
  `t_device_tx`. With those the client computes the offset and, more usefully,
  a `delay` it can act on:

      offset ≈ ((t_device_rx − t₁) + (t_device_tx − t₄)) ÷ 2
      delay  ≈ (t₄ − t₁) − (t_device_tx − t_device_rx)

  This is NTP's exchange, for NTP's reason. §9.7 also states what it does not
  fix: the response is queued for the next connection event, so `t_device_tx`
  is when the device prepared the answer rather than when the radio sent it,
  and BLE's queuing asymmetry survives. A client SHOULD sync several times and
  keep the smallest `delay`.

  A device MUST take `t_device_rx` when the write arrives, not when it starts
  composing the reply — the gap between those is exactly the processing time
  this exchange exists to remove, and a device reading its clock once and
  reporting it twice looks on the wire like one that answered instantly.

- **`fix_flags` bit 4 (`solution_epoch`) is assigned** from the space Appendix
  A reserved.

### Added
- **§5.6 — when a fix is timestamped.** `t_device` MUST be the epoch of the
  solution, not the instant the fix reached the device. A GNSS receiver
  computes a solution for a specific moment and delivers it tens to hundreds of
  milliseconds later; a device stamping delivery reports a position that was
  true at one time with a timestamp naming another, and puts GPS systematically
  late against CAN and IMU. That offset removes exactly the cross-channel
  alignment §8.1's shared clock exists to provide, while every number continues
  to look plausible.

  Whether a receiver exposes the epoch is a property of the hardware, so a
  device that cannot determine it clears `fix_flags` bit 4 and stamps arrival.
  The flag is §1.1 applied to time: the honest answer is a measured epoch or an
  admission, never a delivery time presented as a measurement.

- **§7 — when an IMU sample is timestamped.** `t_base` MUST be the acquisition
  time of sample 0, not the moment the device drained the FIFO. At 833 Hz a
  sixteen-deep FIFO is nearly twenty milliseconds, and the error moves with the
  buffer's occupancy so a client cannot calibrate it away. Unlike a GNSS epoch
  this takes no flag and allows no exception: the device sets the sampling
  schedule, so sample 0's time is the drain time less the samples behind it.

  CAN had had this rule since §6.7 and the other two streams had nothing
  equivalent — the two where acquisition-to-report latency is largest.

- **`time_sync` is a conformance record**, with vectors for the ordinary case,
  a device answering within one tick, the u64 ceiling, and the two malformed
  cases. `t_device_tx` before `t_device_rx` is rejected by both decoders and
  refused by both encoders: a negative round trip halved into an offset is a
  confidently wrong clock rather than an obviously broken one.

### Fixed
- **The peripheral's own TIME_SYNC check asserted the response was 11 bytes**,
  the single-timestamp form. It now takes the length from the schema, so the
  record cannot change under it again.

### Fixed — the harness
Four ways the verification tooling could pass without verifying anything. The
pattern is worth naming, because this is the fourth time in this repository's
short life that a check has silently become a no-op: a stale build, a missing
runner, a stale `__pycache__`, and a selftest assertion written to match the
bug. A check that cannot fail is not a check.

- **A mutation sweep that could not build reported success.** Breaking a
  header made all 79 mutations of one operator fail to compile, and
  `tools/mutate.py` printed "0 caught, 0 survived, 79 uncompilable" and
  **exited 0**. CI runs this step, so a broken build turned the strongest
  check in the repository into a green tick. Build failures now fail the run,
  and a new CI step breaks the build on purpose to prove the sweep still
  refuses to pass.

- **An operator that matched nothing reported success.** `bound` targeted the
  `plen > 64` check removed with the CAN FD length rules, so it generated zero
  mutations and said so in one line of output nobody reads. It now targets the
  §6.10 bounds that replaced it — the Classic limit and the FD ladder, two
  mutations, both caught — and an operator generating nothing fails the run.

- **The runner could not tell `true` from `1`.** In Python `True == 1`, so a
  decoder emitting `1` for a boolean field compared equal and passed; likewise
  `5.0` for an integer. The runner exists to check what an implementation put
  on stdout, and JSON distinguishes these even where Python does not.

- **The runner had no notion of a role.** SPEC.md §12 asks an implementation
  to pass the vectors for the roles it declares, and the runner handed every
  vector to everything — so a GPS-only decoder failed conformance for records
  it had never claimed. `--roles gps,can` runs a subset with `core` always
  included, and the summary names the scope so a partial pass cannot read as a
  full one.

### Fixed
- **The Python GPS encoder ignored `ext_count`.** "`ext` must match
  `ext_count`" was a docstring rather than a check, so it produced a fix
  declaring three extensions and carrying none — a record its own decoder
  rejects as `ext-truncated`. The C encoder had validated this all along, so
  the two references disagreed about what they would emit, which is exactly
  the asymmetry a corpus of decode vectors cannot see. The extensions are now
  walked, since §5.5 defines them as `[type][len][value]` and walking them is
  the only way to know the count is right.

### Fixed — device behaviour
- **The first notification of every connection carried `seq` 1, not 0.**
  `_next_seq` pre-incremented, so a client counting from 0 saw a
  one-notification gap on every stream of every connection before anything had
  been lost. §8.2 is now stated as a property of the notification — the first
  carries 0, the second 1 — rather than of the counter, because the counter
  phrasing is what permitted the bug: "restarts at 0" can be read as zeroing
  the counter and then taking the next value.

  The peripheral's own conformance check had been written to match, asserting
  the first notification carried 1, so the test agreed with the bug it existed
  to catch. That is the more useful half of this entry.

- **A wrapped subscription handle silently overwrote a live one.** The counter
  cycled 1..65535 without checking what was installed, so on wrapping, the
  entry a client knew as handle 1 became a different subscription and
  `CAN_UNSUBSCRIBE(1)` removed something the client had never installed —
  §9.2's one prohibition on handles. Allocation now skips occupied handles,
  which terminates because at most `can_subscription_slots` of 65535 numbers
  are ever in use.

- **A refused notification consumed a sequence number and discarded the
  backlog it was reporting.** Building a batch zeroes `dropped`; if the
  transport then refused it, `record_refused` credited back only the records
  that batch carried. A device that had already lost 500 frames went on to
  report the one it lost next, and 500 vanished from the accounting. It also
  kept the `seq` it had consumed, so the client saw a gap — which §8.2 defines
  as loss in transit — for a notification that never went out. Both are now
  returned: the count, and the number.

- **The device never learned the negotiated ATT MTU.** Batch sizing came
  entirely from `--mtu`, so a device told 247 on a link that negotiated 185
  built notifications the link could not carry. `set_link_params` was defined
  and never called, which also meant `GET_LINK_PARAMS` reported nothing at
  all. The transport now reads `maximumUpdateValueLength` from the subscribing
  central, resizes batches, reports the value through §9.1, and warns when the
  link is below the minimum §2 requires.

- **The link edge was detected at the display refresh rate.** The connect and
  disconnect handling sat inside the `ticks % every` block, so it ran at 10 Hz
  and up to 100 ms late — tied to a setting with nothing to do with it. In
  that window the device numbered notifications from the previous connection
  and then reset, so a client saw `seq` run 1500, 1501, 0: not a gap, which
  §8.2 defines, but a jump backwards, which it does not. Checked every tick
  now.

### Changed — wire format
- **§10 no longer requires any device to encrypt anything, and requires every
  client to support a device that does.** The requirement had been on Control
  alone, which protects the wrong half: Control carries commands, from which an
  eavesdropper learns little, while the streams carry the measurement —
  position included — and were left in the clear. It guarded who may
  reconfigure a device while leaving what the device reports readable by anyone
  in range.

  Requiring encryption costs a device author real work: bond storage, a bond
  table that fills, and a mismatch after reflashing that presents as a broken
  device. That cost lands hardest on the small implementations this protocol
  needs, in exchange — under the old split — for protecting the part with
  nothing to reveal. Supporting encryption costs a client almost nothing, since
  every major central stack turns `Insufficient Encryption` into a pairing
  attempt on its own.

  So the obligation moved to the side that can bear it. A device MAY protect
  any characteristic, all of them, or none; a client MUST cope with each. A
  device that protects anything SHOULD protect the streams and not only
  Control, and one on a bus carrying more than powertrain telemetry SHOULD
  protect everything — but both are now SHOULD, not MUST.

  §10 also states plainly what Just Works pairing buys, which it did not
  before: protection from a passive listener, and none from an active
  man-in-the-middle.

- **§10's requirement could not have been implemented as written in any case.**
  It required both that Control require an encrypted link *and* that a device
  reject unencrypted writes with status `needs_encryption`. Those are mutually
  exclusive: a characteristic carrying the GATT encryption permission has its
  unencrypted writes answered by the ATT layer, so nothing reaches application
  code to generate a reply from, and a device that can reply has not set the
  permission.

  §10.1 now requires the GATT permission of any device that chooses to encrypt.
  Status `needs_encryption` (6) stays allocated and MUST NOT be reused, but a
  conforming device has no occasion to send it.

- **Info SHOULD be readable on an unencrypted link whatever a device
  protects (§10.2).** A client that cannot pair, or has not yet, can then still
  identify what it has found and say so, rather than reporting a device that is
  present, advertising a VTP service and apparently broken.

- **`detail` is present if and only if `status` is `ok` (§9).** A refused
  request is three bytes. Nothing had said so, and the peripheral already
  behaved this way, so a client reading the five bytes a successful
  `CAN_SUBSCRIBE` returns would have taken a handle from a request that failed.
  The alternative — a fixed-width response zero-filled on failure — hands that
  client a well-formed handle 0 instead, which is the plausible-wrong-value
  failure §1.1 exists to prevent.

### Added
- **The request lifecycle (§9.6).** Three rules that were previously left to be
  discovered:

  A client MUST enable indications on Control before its first write. A device
  MUST NOT apply a request it cannot answer. Every opcode in the specification
  is safe to retry, and the table saying why is now in the specification rather
  than in each implementer's head.

  The second is the load-bearing one, and it is the one an implementation is
  most likely to get wrong, because applying first and answering second is the
  natural order to write the code in. This exact failure was observed against a
  real client before it was specified: the device applied a subscription,
  dropped the refusal it owed for a later one, and the client timed out and
  dropped the link while the device believed itself correctly configured.

- **A queue depth, and a rule for reusing tags (§9).** A device MUST accept at
  least four outstanding requests and MUST answer `busy` rather than silently
  discarding one it has no room for. A client MUST NOT reuse a tag while a
  request bearing it is outstanding, and a device MUST answer `bad_params` to
  one that does — correlation is the tag's only job, and two outstanding
  requests sharing one produce two responses a client cannot tell apart.

  Four is a fixed floor rather than a value advertised in Info: a negotiated
  depth would cost a field in a record that can never grow again (§11.2) to
  solve what a constant solves.

- **`control_response` is now a conformance record.** The response envelope was
  the one wire format in VTP/1 with no coverage at all, which is why the rule
  above could be stated but not enforced. Both reference implementations decode
  and re-encode it, and nine vectors cover the conditional detail, an unknown
  status, an unknown opcode's opaque detail, and both malformed cases. Removing
  the detail-on-error check now fails a vector.

- **§3.4 — the Device Information Service (`0x180A`) is now a SHOULD**:
  manufacturer, model and firmware revision at least. Nothing in VTP/1 reads
  it, which is the point — it is where every generic Bluetooth tool already
  looks, so it answers "which firmware is on the logger that is misbehaving"
  without the asker knowing anything about this protocol. The peripheral
  exposes it.

### Fixed
- **The peripheral applied control requests it could not answer, and queued
  responses without bound.** Both halves of §9.6, and the second was a way for
  a client to make the device allocate without limit. Admission is now decided
  ahead of dispatch by a queue that holds no Bluetooth state, so the rules are
  covered by the selftest without a radio.

- **The peripheral can now present each of the three encryption postures §10
  permits**, because the interesting question is not whether a device encrypts
  but whether a *client* still works against one that does. `--encrypt all`
  (the default) protects everything but Info, `control` reproduces the
  incoherent arrangement §10.2 warns about, and `none` protects nothing. A
  client that passes all three supports encryption without requiring it, which
  is what §10 asks of it.

  Verified against LapSmith on iOS at the `control` posture: it raised a
  pairing prompt, paired, enabled indications on Control and installed three
  subscriptions. Just Works pairing initiated against a Mac in the peripheral
  role does work, which was the platform risk worth settling.

### Changed — wire format
- **A CAN subscription now identifies a frame by its format as well as its
  identifier.** Matching ran over `0x1FFFFFFF`, the twenty-nine arbitration
  bits. Bit 29 — the standard/extended flag — fell outside it, and the
  peripheral masked the bit off the frame before comparing, so an exact
  subscription to standard `0x1A0` also matched extended `0x1A0`. Those are two
  different frames from possibly two different ECUs, so a client received a
  payload it had not asked for behind an identifier that looked exactly right:
  the failure §1.1 exists to prevent.

  Matching now runs over bits 0–29, and `CAN_SUBSCRIBE` is `CAN_SUBSCRIBE_MASK`
  with a mask of `0x3FFFFFFF` (§9.2). Bits 30 and 31 — CAN FD and RTR —
  describe how a frame was transmitted rather than which frame it is, and take
  no part. A client that genuinely wants both formats clears bit 29 in its own
  mask, which the device can honour precisely because it can see it was asked.

- **Payload lengths a CAN bus cannot carry are now rejected (§6.10).** `len`
  was bounded at 0..64 for every frame, but the legal lengths are not
  contiguous. A Classic frame carries 0..8; a CAN FD frame carries a length its
  four-bit DLC can express, which above eight is the ladder 12, 16, 20, 24, 32,
  48, 64. Both decoders accepted a nine-byte Classic frame and a nine-byte FD
  frame, neither of which any controller can produce. A length off the ladder
  means the reader and the writer disagree about where the record ends — so
  every byte after it is suspect, including the next frame's identifier — and
  the batch is rejected rather than repaired.

  These rules subsume the old 0..64 bound in every branch, so it has been
  removed. A redundant check is worse than none: it can be deleted without any
  vector noticing, which is exactly what `tools/mutate.py` reported.

### Changed
- **A device MUST keep subscription mode state per matching identifier
  (§6.8).** The specification said nothing about masked subscriptions, and the
  peripheral kept one set of state per subscription. A mask covering `0x0C0`,
  `0x1A0` and `0x2E0` therefore delivered `0x0C0` alone: the first frame of
  each tick consumed the interval and the other two were suppressed. All three
  modes fail differently under shared state — `on_change` worst, comparing one
  identifier's payload against another's, where "changed" carries no meaning at
  all — and every one of those failures looks like a quiet bus rather than a
  bug.

- **Both encoders now refuse to emit anything their own decoders reject.**
  Over-long standard identifiers, CAN FD with RTR, remote frames carrying a
  payload and impossible lengths were all encodable. A device that ships one
  has produced a notification no conforming client can read, and it finds out
  from the field rather than from a test.

### Fixed
- **The corpus could not detect a decoder that ignored trailing bytes on a CAN
  batch.** Relaxing the trailing-byte check from `off != len` to `off > len`
  passed all 79 vectors. `tools/check_corpus.py` reported full coverage because
  it sorted must-reject vectors into two buckets — shorter than the base
  record, and "at or beyond" it — which lumped a truncated batch together with
  one carrying surplus bytes. Those are opposite faults caught by opposite
  comparisons, and `count-exceeds-payload` was satisfying the requirement for
  both.

  The checker now classifies each must-reject vector against its own declared
  length, in three buckets rather than two. It immediately found the same gap
  in three more records: `gps_fix`, `imu_batch` and `monitor_list` had no
  vector whose payload stops short of what its header declares, so a decoder
  that trusted `count` without checking the buffer passed. Seven vectors added
  in all; `off > len` now fails.

- **`tools/check_docs.py` held released changelog entries to the current corpus
  size.** A released entry records what that release contained, and rewriting
  it to match today's count would falsify history. Only `[Unreleased]` is now
  checked against the corpus on disk.
### Clarified
- **The specification says who the "device", the "client" and a "receiver" are,
  and what shape each stream has, before it uses any of them** (SPEC.md §1.2,
  §1.3). The three words were used consistently throughout and defined nowhere,
  which is a cost paid by every first-time reader and by nobody who already
  knew. §1.3 puts the payload shape of every characteristic in one table — one
  fix per notification, a batch header plus records for CAN and IMU — so the
  framing is known before the field tables that assume it, along with the three
  properties (`seq`, `dropped`, one device clock) that §8 specifies for all
  three streams at once.
- **Field descriptions spell out the abbreviations.** `vel_n`, `h_acc`,
  `s_acc`, `p_dop` and `num_sv` were legible only to a reader who already knew
  a GNSS receiver's vocabulary. The description now also leads the Notes column
  instead of trailing a validity clause, so what a field *is* arrives before
  when it is valid.
- **A worked example for scaled and signed fields** (§5). Latitude and
  longitude carry the hemisphere in the sign of a two's-complement integer,
  which is where client implementations have historically gone wrong. The
  example decodes a southern and a western coordinate, gives their bytes, and
  states what an unsigned read produces instead.
- **A worked example of a CAN batch** (§6, §6.1): the byte layout of a
  three-frame notification, and a batch whose `dt` values are decoded to
  arrival times. `t_base` is now stated to be an absolute reading of the device
  clock and `dt` an offset from the `t_base` in the same notification — neither
  accumulates, which is a reading the previous text permitted.
- **"Shedding load" is defined where the flag is** (§6.3) rather than three
  sections away, and the IMU's `period` is explained against the CAN record's
  `dt` (§7): a bus frame arrives when the bus decides and a sample when the
  device asks, so one carries a per-item offset and the other one interval.
- **Microseconds are written `µs` in the generated tables**, matching the prose,
  which always did.
- **The datum is stated** (SPEC.md §5). `lat`, `lon` and `alt_ellipsoid` MUST
  be referenced to WGS-84, which every implementation was already assuming and
  none could check. A coordinate in an unstated datum is metres of silent error
  against a map the device knows nothing about — a plausible wrong value in the
  one place a client cannot detect one. `alt_msl` is explicitly *not* pinned to
  a geoid model, since the receiver's model is the receiver's business and the
  difference between the two altitude fields is the separation.
- **`head_mot` and the velocity triple have a stated frame** (§5.4, retitled
  "Reference frames and derived quantities"). Heading is clockwise from true
  north, never magnetic and never a grid bearing, and the triple is a local
  north-east-down frame at the reported position, so a climbing vehicle reports
  a negative `vel_d`. Both were implied by §5.4's `atan2(vel_e, vel_n)` and
  neither was said.
- **§9.1 is where §9.1 belongs.** Link parameters were numbered first and
  printed last, after §9.5. The text is unchanged and so is every reference to
  it; only the reading order is fixed.
- **Why the CAN subscription table is addressed by mask** (RATIONALE §6). The
  usual reason for masks — too few hardware acceptance filters — does not apply
  to a device that unpacks every frame anyway, and nothing said what the actual
  reasons are: one slot covering a family of identifiers against a declared
  `can_subscription_slots`, a mask of zero surveying a bus whose identifiers
  are not yet known, and the specificity ordering of §9.3 letting those two
  compose. The cost is stated with them.

### Changed — device behaviour
- **A frame that a subscription mode did not select is not `dropped`**
  (SPEC.md §8.3, §6.3). `dropped` already excluded frames matching no
  subscription; it said nothing about a frame that matched a `periodic`,
  `on_change` or `every_nth` subscription and was not forwarded because the
  mode said not to. Both are filtering working as instructed, and counting the
  second would send a client hunting a capacity fault that does not exist. The
  reference peripheral already behaved this way — only the specification was
  silent, which is the kind of gap that produces two conforming devices whose
  drop counters cannot be compared.

### Changed
- **The peripheral's synthetic vehicle now varies.** It ran at constant speed on
  a circle, which made every CAN value a constant: engine speed, road speed,
  throttle and lateral g never moved. A client decoding a channel correctly and
  one reading a fixed byte offset wrongly produced identical screens, so the
  bus tested nothing.

  Speed is now `v(t) = 30 + 12·sin(2πt/20)` m/s, position is its exact integral
  and longitudinal acceleration its exact derivative — so the road speed on the
  CAN bus is the derivative of the GPS track and the IMU's X axis is the
  derivative of that. Cross-channel agreement is the property VTP/1 exists to
  provide, so a test device that faked it would be testing the wrong thing.

  The bus carries three identifiers with documented layouts
  (`reference/peripheral/README.md`): `0x0C0` engine at 50 Hz, `0x1A0` driver
  inputs at 20 Hz, `0x2E0` chassis at 10 Hz. RPM sawtooths across four gears,
  which a wrongly decoded channel does not.

### Fixed
- **A refused control response was dropped, which timed a client out.** The
  device took a `CAN_SUBSCRIBE`, applied it, answered `ok` — and the indication
  carrying that answer was refused by the transport and discarded. The client
  waited on its tag, gave up and dropped the link, and the two ends disagreed
  about the subscription table in the meantime.

  A notification and a control response are not the same kind of thing. §8.3
  discards what cannot be delivered and reports it; §9 says a device MUST
  respond to every request, and there is no discard option. Responses are now
  queued, retried until they land, and sent before notifications each
  iteration. They are the one thing on this link that is owed rather than
  offered.
- **Notification sending is paced to the transport and fairly ordered.** Two
  faults compounded: the send order was fixed, so whichever stream went last
  absorbed every refusal — with GPS, IMU and CAN all subscribed, CAN was
  refused almost in full while the other two flowed, purely by position — and
  the device fired regardless of whether the stack could take it. The order now
  rotates, and CoreBluetooth's ready-to-send callback is hooked (bless only
  logged it) so at most one notification per stream is held rather than
  produced into a queue known to be full. Measured in use: 195 callbacks, zero
  safety timeouts.
- **The debug panel was throttling the transport it exists to measure.**
  `root.update()` blocks the loop for 15–30 ms per call, costing 20–35% of
  notification throughput — every earlier throughput figure in this branch was
  an artefact of the window rather than a property of the link. Repainting is
  not the cost (0.6 ms); the writes are now cached anyway, and the panel
  reports its own overhead so the trade is visible. `--no-display` is
  documented as the mode for throughput-sensitive testing.
- **The peripheral ignored the return value of its notify call.** The host
  stack returns false when it will not carry a notification, and the
  notification is then never sent. Ignoring that lost data silently *and*
  misreported it, because the device's own `dropped` counter never learned. It
  is checked now, and refused items are counted into `dropped` in source items
  — eighteen samples for an IMU batch, not "one notification", because
  `dropped` is defined in items.

  A refusal because **nobody has subscribed** to that characteristic is not
  loss and is counted separately: those notifications were never due, and
  reporting them as discarded claimed data was missing that no client had asked
  for.
- **`VtpDevice.on_connect()` was never called by the transport.** Only the
  selftest called it, so the live device had never performed its per-connection
  reset: sequence numbers never restarted (§8.2) and the CAN subscription table
  was never cleared (§9.2). A conformance violation in the reference device,
  which is the worst place for one. A connection edge now drives it, and both
  transitions are logged.
- The display reported "client connected" forever. It latched true on the first
  read or write and nothing ever cleared it, because there was no disconnect
  detection at all.

### Added
- **The peripheral's window is now a scrollable debug panel.** Notify subscriptions per
  characteristic, per-stream sent/refused/no-subscriber counts and rates, the
  installed CAN subscription table with modes and arguments, and a rolling log
  of control requests with their status — alongside the Monitor values.

  Every value is a widget rather than preformatted text, so each carries its
  own colour: dim is idle, bright is active, amber wants attention, red is
  loss. The body scrolls, because the panel is taller than the window and the
  Monitor section otherwise sat below the bottom edge with no way to reach it.

  It calls out the combination that produces silent failure: CAN ids installed
  with no subscriber on the CAN characteristic. Those are two different
  subscriptions and having one without the other looks, from the client side,
  exactly like a decode bug.
- **A screen on the software peripheral** (`reference/peripheral/display.py`).
  A Monitor device exists to display values it cannot compute, so the only way
  to tell whether the role works end to end is to look at one. The window shows
  the channels the device asked for and the values the client supplied.

  What it makes visible is absence. A slot the client has not supplied, or has
  marked absent, renders as `—·—` in a dimmer colour — never as `0.000`. Before
  the first lap of a session there is no last lap time, and a display showing
  zero for it has been told something false. That distinction is the reason
  `monitor_value` carries a `present` bit (§13.4), and it is invisible in a log
  of numbers.

  Formatting is where the channel enum earns itself: each channel has exactly
  one unit fixed by §13.2, so the device renders a lap time as `1:27.340` and a
  speed as `136.8` km/h without asking the client anything.

  Split like the rest of the peripheral — the formatting is pure and checked in
  CI, and `tkinter` is imported lazily, because the interpreter CI runs does not
  have it and must not need it. `python3 display.py` shows the screen alone with
  no Bluetooth; `serve.py --no-display` runs headless.
- The device's requested channel set is configurable, and it now asks for six.

### Fixed
- **The peripheral replayed its backlog after a stall instead of discarding
  it.** The IMU catch-up loop emitted one sample per elapsed period however
  long it had been since the last poll, so a device left unpolled for a minute
  delivered six thousand batches as fast as the radio would take them. Found by
  reading the log of a real run: 12,400 notifications in 0.2 seconds after the
  process spent 38 minutes waiting on a permission prompt.

  Delivering a backlog is worse than losing it. The timestamps say when the
  samples were taken, so a client cannot tell the stream is behind — it just
  receives a flood of stale data with old times on it. SPEC.md §8.3 already
  says what to do: discard what cannot be delivered and report it in `dropped`.
  The device now bounds the catch-up to one batch and counts the rest.
- The display window is created **after** the server starts advertising. Tk
  takes over the main run loop when it initialises, and CoreBluetooth needs that
  run loop to deliver its power-on callback; creating the window first left
  `BlessServer.start()` waiting for an event that could no longer arrive, with a
  window up and nothing behind it.
- `make_macos_app.sh` refuses to rebuild over an existing bundle. The bundle
  holds only the interpreter — the scripts are read from the repository at run
  time — so rebuilding is almost never necessary, and re-signing changes the
  code signature, which makes macOS treat it as a different app and ask for
  Bluetooth permission again. A peripheral hanging with nothing but `logging to`
  in its log is waiting for that prompt. `FORCE=1` overrides.
- Characteristic setup is logged one at a time, so a GATT call that never
  returns can be told apart from any other. One of them did.

### Added
- **The Monitor role (SPEC.md §13).** The one part of VTP/1 that runs
  client-to-device: the client supplies values the device cannot compute, so a
  device with a display can show them. Lap time is the case that justifies it —
  a logger has no idea where the start and finish line is, because that is drawn
  on a map in the client, so a device can only ever display a lap time the
  client sends it.

  **Channels are enumerated, not computed.** The prior art sends an expression
  string the client must parse and evaluate against its own namespace of
  variable names. VTP/1 defines a `channel` enum instead: the device names a
  thing, not a computation. There is therefore no expression language, no shared
  variable namespace and no parser on either side, and a client can fail to
  satisfy a request in exactly one way — not implementing the channel. New
  channels are enum members, which §11.4 already permits a minor version to add.

  Each channel has exactly one unit, fixed by the table. No unit negotiation and
  no scale factor: `lap_time` is milliseconds everywhere, forever.

  **A value carries a `present` bit** (`monitor_validity`), and a value whose bit
  is clear MUST be written as zero and rendered as unavailable. This is §1.1 in
  the one place the protocol reverses direction, and it is the gap the prior art
  cannot express: before the first lap of a session there is no last lap time,
  and a device displaying 0.000 for it has been told something false. A client
  that cannot supply a channel MUST say so rather than fall silent — absence is
  a state a display can render, silence is indistinguishable from a crash.

  The device's declaration is read with the new `MONITOR_LIST` opcode, paged
  exactly as `CAN_LIST` is, and is fixed for the duration of a connection — the
  same rule as §9.2's subscription table, so a client never inherits state it
  did not establish.
- `capabilities` bit 3 now means what it says; the software peripheral
  implements the role, declares four channels and renders absent slots as
  unavailable rather than as zero.

### Changed
- **Records other than `gps_fix` are declared closed for the life of major
  version 1**, and SPEC.md §11.3 now describes the extension mechanisms that
  exist instead of promising one that did not. §11.2 previously said "new
  fields MUST be added as extension records" while eight of nine records had no
  such mechanism.

  The decision could not be deferred. A conforming receiver rejects a payload
  whose length it does not expect, so a trailer introduced in a later minor is
  rejected by every client already deployed — the first device to send one
  stops working with the installed base. Extensibility is settled before 1.0 or
  decided against.

  What a minor version may add is now stated exactly: extension records on the
  records that carry them (`gps_fix` alone), reserved bits and bytes, and new
  control opcodes — which are not fixed-size records, so anything a client can
  *ask* for remains extensible without limit. Multi-bus CAN (§6.9) is intended
  to close that way.

  A trailer on `can_record` would cost 4 kB/s at 4000 frames per second, on the
  one stream RATIONALE §4.1 identifies as able to saturate a link — the same
  arithmetic §6.6 used to exclude BRS and ESI, which does not stop applying
  because the byte is named `ext_count`.

  Consequences are stated in RATIONALE §5.4 rather than left to be discovered:
  a magnetometer triple, a remote frame's requested length, and BRS/ESI are all
  VTP/2 changes, as is any pushed batch-level field beyond the two reserved
  bytes in each header.
- §11.3 is now "What a minor version may add"; the prohibitions move to §11.4.
  Every reference across the specification, the references, the tooling and the
  issue templates was updated — a renumbering the docs checker cannot catch,
  because the old number still resolves, just to the wrong section.

### Added
- The extensibility table in SPEC.md §11.3 is generated from the schema, so the
  specification cannot claim a record is extensible when the codecs disagree —
  which is exactly what the old wording did for eight of nine records.

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
