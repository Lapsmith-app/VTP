# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versioning follows SPEC.md §11: major versions have separate service UUIDs;
minor versions are additive and never change the decode of an existing
conformance vector.

## [Unreleased]

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
