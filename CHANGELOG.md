# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Versioning follows SPEC.md §11: major versions have separate service UUIDs;
minor versions are additive and never change the decode of an existing
conformance vector.

## [Unreleased]

### Harness: §14.6 made the `applied` path unreachable, so a conforming device got half of §14.4 tested

Reported from the first VTP/1 device firmware, and not as a false failure —
the harness was behaving correctly and saying so. It is a coverage hole two
MUSTs opened between them.

Every aiding transfer the harness sent was built by `checks/aiding.py::_payload`:
a byte pattern that is not all the same, so a misplaced chunk shows up. That is
exactly right for what most of §14 tests — chunk indexing, the CRC, a `begin`
superseding an open transfer, an oversized transfer refused — none of which
care what the bytes mean.

But §14.1 declares `ubx_mga` as a concatenated sequence of UBX-MGA messages,
and §14.6 makes it a MUST NOT: a device MUST NOT accept anything but aiding in
the format it declared. A device that enforces that refuses `_payload()` by
construction — it is not UBX at all and does not frame — and `aiding.transfer`
already handled the refusal correctly, noting that `rejected` is a §14.4
outcome and observing rather than failing.

The consequence is the wrong way round. Everything after that early return
never ran on such a device: `applied` itself, the MUST the check is named for;
the `first_missing` assertion that only exists on the applied branch, which is
its own MUST; and the state key a later check would inherit the same hole
from. **The stricter a device was about §14.6, the less of §14 got tested.**

So the payload is now the client's to supply:

    --aiding-blob PATH    send these bytes as the payload of the §14.4
                          transfer, instead of a synthetic pattern

Only `aiding.transfer` takes it. `aiding.chunk_size`, `aiding.rejects_oversized`
and `aiding.begin_supersedes` each need a transfer of a *specific* size and keep
the synthetic pattern; a real product is the wrong shape for them. A blob larger
than the device's declared `max_bytes` skips rather than being clamped to fit —
§14.2 requires that transfer to be refused, and a blob cut down to size is no
longer aiding in any format, so the run would say something false about the
device.

The harness does not build the blob itself. Doing so would put a
format-specific payload builder inside a harness that is otherwise entirely
format-agnostic, and §14.1's whole shape — the device names a format, it does
not describe one — is what keeps it that way.

What a supplied blob changes is the verdict when the transfer is refused
anyway. Sent synthetic bytes, `rejected` is an observation and nothing more.
Sent bytes the operator vouched for, it is a **warning naming both suspects**:
either those bytes are not aiding this receiver will take — the wrong product,
the wrong format, or stale — or the device refuses one that is. §14.1 makes the
bytes opaque to the harness, so it cannot tell you which, and it must not blame
a conforming device for a stale download.

The hole is now reproducible in the tree rather than only on a bench.
`--fault aid_strict_receiver` is a *conforming* device whose receiver walks the
committed blob as UBX framing and refuses it unless every message frames, every
checksum agrees, every message is in the MGA class, and the blob ends exactly on
a frame boundary. It never looks inside a payload — §14.1 says those bytes are
opaque — so nothing checks a `msgId` against a list or cares about ordering. It
reads the declaration the device made and nothing more. `harness/selftest.py`
runs it three times and requires three different verdicts: an observation with
no blob, a **pass** with a real one, and a warning with a blob that is not
aiding.

The real blob the selftest feeds it is built at run time, in about twenty
lines, from two ordinary `UBX-MGA-INI` messages — time and position. That is
worth recording because `ubx_mga` sounds like it implies a u-blox AssistNow
subscription, and for reaching `applied` on a bench it does not. §14.6 is also
why it is built rather than kept as a fixture: a device applies a transfer at
commit, so a time initialisation on disk is already as old as the file.

Review of the above found six further problems, and three of them would have
failed a conforming device — the thing this harness exists not to do:

- **§14.3's chunk cap is a second ceiling, and clearing `max_bytes` does not
  clear it.** A transfer MUST NOT need more than 65535 chunks, because `index`
  and `first_missing` are both `u16`, and a device MUST answer `bad_params` to
  a BEGIN that would open one. On every ordinary profile `max_bytes` binds
  first, which is why the rule was easy to miss; a device that chunks small and
  accepts large transfers reaches it, and the harness reported the *required*
  refusal as a failed MUST. A blob past the cap now skips, saying so. An
  accepting device is failed explicitly, before the chunk index overflows the
  `u16` it is packed into and turns a finding into a harness error.
- **A commit has no deadline, and the harness gave it three seconds.** §14.3
  forbids handing any part of a transfer to the receiver before the commit, so
  everything the module has to be told happens inside that one response, and
  tens of kilobytes down a UART is tens of seconds at the baud rates GNSS
  modules run at. The commit timeout is now the transfer's size, not the
  control plane's default. Unreachable before this change — the synthetic
  payload is at most two and a half chunks.
- **macOS relaunches through an app bundle, and the bundle has its own working
  directory.** The parent read `--aiding-blob assist.ubx` and then handed the
  child the same relative path from somewhere else, so the invocation in
  `harness/README.md` failed on the one platform whose instructions are longest.
  Path options are now resolved against the invoking shell's directory before
  the relaunch — `--json` and `--markdown` with them, which had written their
  reports into the bundle's cache for as long as the relaunch has existed.

And three that were wrong rather than harmful:

- An unreadable or empty `--aiding-blob` exited 1, which is
  `EXIT_NOT_CONFORMING` — a verdict about a device the run never connected to.
  It is `EXIT_ERROR` now: a run that did not happen.
- The `rejected` branch caught every non-`applied` value, so a member a future
  minor version adds would have been given receiver-refusal semantics. §14.4
  says *other* decodes as unknown, never as a default; the harness now says
  unknown and puts no verdict on it.
- `Fail(severity=...)` moved a finding's severity and reporting kept reading
  the check's, so JSON emitted `severity: MUST` beside `status: warn` and the
  console filed it under "SHOULDs not followed". `Result` now carries the
  finding's own severity — `check_severity` is beside it in JSON for the
  check's — and the console heading no longer claims a SHOULD was broken. This
  is the first *downward* override in the tree, which is why nothing had
  surfaced it.


### §5.2: `num_sv` counts the solution `fix_type` names, and `p_dop` a position

Reported by the first VTP/1 device firmware, against this repository at
`7eeaf56`. Not a decode disagreement — no payload shows it. It is an ambiguity
a device has to resolve before it can emit a fix at all, and §5 did not resolve
it.

`num_sv` was described as "Satellites used in the solution" while §5 opens by
scoping the record to "exactly one position solution", so on a `time_only` fix
the document supported two readings and they disagreed. §5.1 leaves no third
state: the bit is set and the field is a measurement, or it is clear and the
field is absent. Two conforming devices reported one receiver differently, and
an absent `num_sv` meant "no satellites contributed" under one reading and
"this device declines to count them without a position" under the other. It is
not hypothetical: a u-blox MAX-M10S reports `fixType = 5` with a populated
`numSV` for most of the first thirty seconds of every cold start, so every
device built on such a part meets this on the way to its first fix.

**`num_sv` counts the satellites used in the solution `fix_type` names**, which
is not always a position solution. Bit 11 answers whether the device has that
count and nothing else, so a device holding it MUST report it on a `time_only`
fix — withholding it because the fix carries no position is not conforming. A
`fix_type` of `none` names no solution at all, so bit 11 is clear there: the
count of satellites *tracked* does not get to borrow the name of the count
*used*, and VTP/1 has no field for the tracked count.

**`p_dop` describes the geometry of a position solution**, so bit 10 is clear
on a fix reporting no position. That was settled only by the adjective in the
field's description; it is now a requirement, and the pair of bits is stated to
be a pair only by adjacency.

**A `fix_type` of `none` or `time_only` reports no position**, so the
`position` bit is clear on such a fix. That was the premise the two rules above
rest on and it was nowhere stated, so a record could name no position solution
and carry a position, leaving a client to choose between the halves of one
record with nothing on the wire saying which was the defect. §5's opening
sentence and the `gps_fix` description said every notification carries "one
position solution", which is the reading the report came from; both now say one
GNSS solution, as does §1.2's definition of a fix — the unit `dropped` counts,
which had quietly excluded the solutions this section is about.

`fix_type = none` had two meanings and the rules above need one. The enum said
"no position solution" while §5.2 leans on "no solution at all"; it now names
both — no position and no timing solution being computed. `t_utc` is
deliberately not constrained by it: a receiver that has acquired GNSS time
keeps it when the solution providing it is lost, bit 0 says the timestamp came
from a GNSS time solution rather than that one was computed for this fix, and
a device losing its fix reports `none` and goes on reporting the time it
holds.

All three are device-side content rules, so a receiver decodes a fix that
breaks one and surfaces it, as with the RTK flags in §5.3 — and MUST NOT read a
`p_dop` beside an absent position as evidence that a position exists, or pick a
winner between a position and a `fix_type` that names none.

No wire change: no field, enum value, UUID or existing conformance vector
moves, and the descriptions that changed are exempt from the compatibility
baseline. Six vectors are added — a time-only fix carrying six satellites,
and five well-formed records a device MUST NOT emit: a PDOP on a time-only fix
and another with the position bit simply clear, a `num_sv` beside
`fix_type = none`, and a position beside each of the two fix types that name
none. Each of the five is paired with an encoder refusal, and both reference
encoders refuse them. Two producer cases go the other way, because no refusal
can assert what is legal: a time-only fix carrying `num_sv`, and a
`dead_reckon` fix whose satellite count is a measurement of zero. An encoder
that gates `num_sv` on a position — the reading this section closed — passes
every refusal in the file and fails both of those. The corpus is 169 vectors
and 67 producer cases. Reasoning in the RATIONALE contradictions section.

Two things that hold the change up rather than state it.
`tools/check_baseline.py` now covers `conformance/encoders.json` as well as the decode vectors: the
producer corpus makes the same promise about what an encoder must refuse and
was protected by nothing, which this change noticed by altering two existing
cases — the `gps_fix` reserved-bits baseline names a `fix_type` of 3, because a
case the encoder must refuse for another reason tests nothing about the
reserved bits. And `gps.solution_scoped_bits` in the harness reports what no
vector can reach: a device that withholds `num_sv` through every time-only fix
emits nothing wrong, so the corpus cannot see it, and the rule that changed
firmware behaviour would otherwise be the one rule here with nothing watching
it. `gps.solution_scoped_bits` fails a device on the three combinations a host
can prove from a payload — a `p_dop` beside no position, a position beside a
`fix_type` naming none, a `num_sv` beside `none` — and `gps.num_sv_scope`
observes the fourth without failing it, because a receiver with no count to
give leaves the same trace from a host as one withholding it. The seeded
`gps_scope_bits_ignored` fault holds the failing half to account, as
`harness/selftest.py` requires of every MUST it defines.


### Harness: §9's window cannot be measured from a host, and a check was verdicting as though it could

Raised in review of the entry below, which is where the reproduction comes
from. It was not introduced there: the same experiment run against the code
before it fails the same way, and one more besides.

`control.busy_when_outstanding` decided whether its two requests had ever been
outstanding together from two host timestamps — the moment the second request
was written, and the moment the first response was *received*:

    overlapped = second.t_write <= first_response.t_recv

§9's boundary is not the second of those. A response is owed until the device
has SENT it, and a host cannot see the send; it sees its own callback, which on
macOS CoreBluetooth schedules on its own and never reports. Between those two
moments the device owes nothing, and a client writing there is a conforming
client that must be answered `ok`. The harness read that `ok` as a device
applying a request it should have refused, and Failed it. It took one event
loop turn of delivery latency against the promptest device in the tree — the
`answers_before_the_next_write` seed below, which sends inside its write
handler — to produce the failure.

Both of the unknowns run the same way. The response was sent before it arrived,
and the second request reached the device after it was written, so a request
written after the previous response ARRIVED was written after it was SENT.
An overlap can be excluded from a host. None can be proven from one, at any
latency, because every measurable delay is also what a conforming device
answering inside its write handler leaves behind.

So the check no longer verdicts on that branch. It reports the answer it got
and says the slot's state could not be told from here. Its Fails are now the
ones a host can stand behind — a request of either pair left unanswered, which
§9.4 forbids whatever was owed — and its pass is the device's own testimony,
`busy`. What is given up is named rather than papered over: a device that
applies a pipelined request and answers `ok` is reported here and not failed,
because from a host it leaves the same trace as a device that had already
answered. Settling it needs the send timestamped — a sniffer, or a `t_sent`
field in `control_response` that a later minor could add.

The same ruler read the other way is sound, and is now named for the direction
it holds in: `_overlapped` is `_overlap_excluded`, returning true only where
the previous response had already arrived. `control.no_unprovoked_busy` fails a
`busy` only on those, so a held-up delivery there costs a finding missed rather
than a device failed for something it did not do.

`transport.FAULTS` gains `host_callback_lands_late`, which is not a device
defect at all: it holds each control delivery one scheduler turn, which is what
a host stack does. Stacked on `answers_before_the_next_write` it is a response
sent before the second request and received after it — every timestamp the
harness owns saying the device was still owing, and the device not owing.
`harness/selftest.py` runs the prompt-device scenario twice, with the host slow
and with it not, and requires the same two verdicts from both: what a report
says must not turn on when the host got round to the callback. It also requires
each run to reach that verdict by the branch it is about, which is what makes
the second run a test of anything — both report the same two statuses, so
asserting only those would pass whether or not the seeded host did a thing.

Not verdicting on the timing is not the same as not verdicting. The clock
cannot settle the choice BETWEEN `busy` and the answer the same request earns
once the slot is free; everything else about that response is owed either way,
and nothing else in a run looks at it — `control.echoes_request` and
`control.detail_only_on_ok` each probe with a request of their own. So the
opcode it echoes, whether it decodes, whether a refusal carried detail, and
whether the status is one of the two §9 leaves open are all tested before the
timing argument is reached. A device answering the pipelined subscribe
`bad_params` — wrong if the slot was occupied, because §9 says `busy`, and
wrong if it was free, because the request was well formed — is
`pipelined_answered_bad_params` in `transport.FAULTS`, and is failed.

A deferred delivery is held to the link it was scheduled on. `disconnect`
clears `_connected` and then awaits the pump before it clears the subscription
table, so a callback landing in that window found a live table and a dead link.
`_answer` has always dropped a response whose connection has gone; the deferral
now makes the same test.

The clean run gains a verdict baseline to go with its expected-skip one
(`selftest.EXPECTED_OBSERVES`). Several MUSTs here Fail on a violation and
Observe on success, because the number they arrived at is worth printing — so
`observe` and `pass` are both ordinary outcomes and a MUST that quietly stops
verdicting reads as one that passed. Every MUST and SHOULD must now pass
against the reference peripheral unless the table says what it measures. This
check is why: it has two Observe branches and one pass, and inverting the
predicate that chooses between them sends every device down a reporting branch
with nothing to notice that the rule had stopped being tested.

`pipelines_silently` now models a device that applies the pipelined request and
answers nobody, rather than one that answers `ok`. The `ok` half is the half no
host can see, so a seed claiming it was caught was really asserting the timing
accident that made the two devices look different; what is left is a violated
MUST whether or not anything was owed when the request arrived.

### Harness: a device too quick to pipeline against is not a device in violation

Reported from the first firmware implementation, which met it as one MUST
failure in an otherwise clean run — with the two lines that disagreed printed
one above the other:

    ····  A request arriving while one is owed is answered busy, and not applied
          the device answered the first request before the second was written, so
          nothing was ever pipelined and this could not be tested from here
    FAIL  A request refused busy did not take effect
          a subscription written while the device owed a response was installed

`control.busy_when_outstanding` pipelines a `CAN_SUBSCRIBE` behind an
unallocated opcode and Observes when the device answered the first request
before the second could be written, because nothing was ever outstanding
together and there was nothing to detect. On that path the second request was
an ordinary conforming one: nothing was owed when it arrived, `ok` was the only
correct answer, and the device installed it, correctly.
`control.busy_not_applied` then read that subscription back and Failed, on a
premise — finding it installed proves the refusal was a lie — that needs there
to have been a refusal. There had been none. The failure was reachable by any
device fast enough to answer before the host can write again, which is what the
Observe above it calls a device behaving well.

The two checks are halves of one exchange, so the second now reads what the
first measured: whether the requests genuinely overlapped, and what the second
was answered. It Skips unless that answer was `busy`, and says which of the two
it was — the same structural limit the Observe already explains, in the same
words. What is recorded is the timestamps and not the check's intent, for the
reason `_overlap_excluded` gives: a `busy` answered to a request that in the event
overlapped nothing is still a refusal of a conforming client, and
`control.no_unprovoked_busy` still reports it.

The subscription that install left behind is now taken back. Nothing removed it
once `busy_not_applied` stopped probing — it had only ever been removed as a
side effect of the failing case — and a slot held for the rest of the
connection is a slot `can.table_full` counts a few checks later, which is a
second failure with an even less obvious cause.

Seeded as `answers_before_the_next_write` in `transport.FAULTS`: a conforming
device that sends its answer before its write handler returns, so no client can
pipeline against it. It is a scenario seed rather than a matrix entry, like the
quiet OBD car, because the claim is about what must NOT be reported — an
Observe and a Skip, no failure anywhere in the run, and no subscription left
installed. `harness/selftest.py` asserts all four.

### Harness: a diverging clock is caught by its rate, not its offset

Reported from a device in the field: CAN and GPS timestamps that agree at
connect and walk apart from there — a per-sensor timer whose count happens to
start with the others', which is the per-sensor clock §8.1 forbids, wearing
the one disguise `clock.one_clock` cannot see through. The offset stays
inside any workable tolerance for the length of a harness run; what never
stops is the trend, and by the end of a session the cross-channel alignment
this protocol exists to make into arithmetic is off by seconds.

`clock.one_rate` compares every pair of streams at matched moments — each
notification against the other stream's nearest-in-time one — so the host
clock, the common ruler, cancels exactly whatever the two logs' densities
do. The verdict is the slope the relative offset holds through BOTH halves
of the window the pair shares: a trend, not a step, so a one-time change in
delivery latency can neither fake a divergence nor cancel a genuine one out
of an endpoint-to-endpoint slope, and is reported as the step it is. A pair
fails above 10,000 ppm accumulated past 40 ms. The batched streams anchor
on a batch's newest item rather than `t_base`, which timestamps the oldest
and moves with fill time and batch size — for CAN, newest by timestamp, not
position, since §6 orders only record 0. The scale errors this class is
made of — a millisecond counter reported as microseconds, a 32.768 kHz tick
counted as 32 kHz (24,000 ppm) — sit far above the bar; a conforming
device's residual latency movement measures a few thousand ppm, and because
that movement is bounded in real fractions of a second rather than in ppm,
the bar does not drop with a longer window: crystal-to-crystal disagreement
(tens of ppm) is invisible to an arrival-time ruler at any length, and the
report says so rather than promising it. Seeded as `clock_diverges` in
`transport.FAULTS` — a CAN timer mis-scaled by 4%, `dt` ticks included,
re-anchored on every connection — as `harness/selftest.py` requires.

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

### Harness: eleven checks for the rules a byte vector cannot reach

The same report noted that the conformance harness had nothing for any of
this. It now has, and none of it needs a second CAN node — five ask the
control plane a question, and six watch what the device's own bus traffic
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
| `can.changed_reinstall_rearms` | §6.8 — a changed `mode` or `arg` re-arms the first frame |
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
window reads those as new frames. Each one also establishes its own
precondition rather than assuming it, because a device that is right must not
be failed for a bus that is quiet: that the identifier is still arriving, that
it arrives faster than the ration a `dropped` check needs it to exceed, that a
batch arrived to carry a count out at all, and — for the displacement check —
that the device is holding two schedules at once with nothing shed, since §6.8
lets a device at its bound reclaim the very state that check measures. The
identifier is subscribed to in the format it was seen in, so an all-extended
bus (J1939, and most of what is not a passenger car) exercises these rather
than skipping them.

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
