# Proposal: single-frame PID grouping for `OBD_POLL_SET`

**Status**: draft, implemented in the reference peripheral and measured
**Constraint answered**: per-PID sample rate under a fixed transmit floor (RATIONALE §11.5, §11.6)
**Wire impact**: one reserved capability bit; one reserved bit per PID byte. No record size changes, no new records, no new opcodes.

---

## 1. The measurement

A client polling *k* PIDs receives each one every *k* × `interval_ms`
(SPEC §15.4). Against the reference peripheral — `obd_poll_slots` 16,
`obd_min_interval_ms` 20 — a full poll set at the floor yields:

```
1000 / (16 x 20) = 3.13 Hz per PID
```

That is the rate the role's own declared capacities permit at their most
favourable setting. It is slower than the same PID set through an ELM327
using multi-PID Mode 01 requests, which is the comparison every client
author will make, and it is slower for a reason the specification chose
deliberately and priced incorrectly.

## 2. Where §11.5's cost accounting goes wrong

RATIONALE §11.5 rejects multi-PID requests and states the cost:

> "The cost is request count: a client polling six PIDs sends six frames
> where multi-PID packing would send one. At the floors involved — one
> short frame per few tens of milliseconds, on a bus whose ECUs answer
> this traffic for a living — the bus cost is negligible"

Two errors.

**The cost is not measured in the unit the client feels.** Request count
is bus load; the client-visible cost is *cycle latency*. Against a fixed
per-request floor, *k* requests is *k* × `interval_ms` of sample period.
§11.5 measures a quantity nobody was worried about and never states the
one that matters.

**The bus cost of grouping is not negligible — it is zero.** SPEC §15.1
already specifies the request frame as `[0x02, 0x01, pid]` **and
padding**. A classic CAN data frame carrying a 6-PID request,
`[0x07, 0x01, p1 … p6]`, is the same eight bytes on the wire. Identical
airtime, identical frame count, identical `obd_min_interval_ms` spacing.
On the response side grouping is a strict *reduction*: one response frame
per ECU per group instead of one per ECU per PID, so fewer frames on the
bus, fewer records in CAN batches, and less radio time.

The rejection in §11.5 is nonetheless *sound for the case it considered* —
unbounded multi-PID packing, whose responses "routinely exceed seven bytes
and arrive as ISO-TP multi-frame transfers". The bounded case was never
separated out. That is what this proposal recovers.

## 3. What the boundedness argument gives back

§11.5's own reasoning is the mechanism:

> "within `0x01`–`0x60` no Mode 01 response exceeds four data bytes
> (J1979's own sizes), so `41 pid data` fits seven bytes always"

A single frame carries seven data bytes. One is the `41` mode echo, leaving
six for `pid`+`data` pairs. Every pair is at least two bytes, so **a
grouped response fits a single frame whenever the group's pairs total six
bytes or fewer** — which is arithmetic on constants, exactly as §11.5
wants, and yields two or three PIDs per group in practice:

| Group | Response | Bytes | Single frame |
| --- | --- | --- | --- |
| `0D 04 05` (three 1-byte PIDs) | `41 0D x 04 x 05 x` | 7 | yes |
| `0C 10` (two 2-byte PIDs) | `41 0C hi lo 10 hi lo` | 7 | yes |
| `0C 0D` | `41 0C hi lo 0D x` | 6 | yes |
| `0C 0D 04` | `41 0C hi lo 0D x 04 x` | 8 | **no** |

**The device performs none of this arithmetic.** The client does, because
the client already owns the tables — SPEC §15.5 puts them there and says
so: "The decode … belongs in the client, which is where the formula tables
live and are updated". §15.9's "no formula tables in firmware" is preserved
exactly; the device gains one bitmask test and a loop bound.

### 3.1 The oversize case is already specified

If a client groups badly, an ECU answers with a First Frame. SPEC §15.5
already says what happens, in text that needs no amendment:

> "a first frame arriving anyway — another tester's transfer, an
> out-of-spec ECU — is an ordinary frame, forwarded if subscribed and
> otherwise dropped, and the exchange it opens dies for want of a flow
> control this device will not send."

The device still transmits no flow control (§15.1, unchanged), still
reassembles nothing, and still cannot be drawn into a multi-frame exchange.
The failure is the client's, is visible to it, costs one interval, and is
recovered by sending a differently-grouped `OBD_POLL_SET`. **Every property
§15.1 exists to guarantee survives this proposal untouched.**

## 4. Normative changes

### 4.1 New capability bit

`info.capabilities` bit 11 — reserved space per Appendix A, assignable in a
minor version per §11.2.

```yaml
# schema/vtp1.yaml, capabilities.bits
- {bit: 11, name: obd_pid_grouping, implies: [obd],
   desc: "Accepts grouped PIDs in OBD_POLL_SET (SPEC.md 15.4.1)"}
```

Bit 11 is past the eight bits the advertisement carries (§3.3), like bit 10
and for the same reason: grouping is something a client uses after it
connects, not something it ranks devices by before it does.

`implies: [obd]` alone is sufficient and `[obd, can, control]` would be
redundant: the validator in `reference/python/vtp1.py` walks every set bit,
so `obd`'s own implications are checked whenever `obd` is set.

No Info field is added. The two bounds this feature needs — six PIDs per
request frame, six response bytes per group — are constants of CAN and
J1979, not properties of a device, so there is nothing for Info to carry.
`info` stays 24 bytes and stays closed (§11.3).

### 4.2 Encoding — bit 7 of a PID byte

`OBD_POLL_SET` keeps its payload exactly: `interval_ms:u16, count:u8,
pids:u8*`. Within each PID byte:

| Bits | Meaning |
| --- | --- |
| 0–6 | The PID, `0x01`–`0x60` |
| 7 | `more` — this PID is grouped with the byte that follows |

A **group** is a maximal run of bytes with `more` set, terminated by the
first byte with `more` clear. `count` still counts PID *bytes*, so
`obd_poll_slots` keeps its present meaning and needs no restatement.

`[0x8C, 0x0D, 0x85, 0x8F, 0x04]` is two groups: `(0C, 0D)` and
`(05, 0F, 04)`.

This is reserved-space assignment, not repurposing (§11.4). Bit 7 of a PID
byte is space that is unassigned today and MUST read as zero: every value
above `0x60` is already `bad_params` under §15.4 rule 5. An old device
therefore refuses a grouped set cleanly and leaves its installed set
unchanged — but no client should ever reach that path, because bit 11 is
read first. Declare, verify, use, as §15.4 already has it.

### 4.3 SPEC §15.1 — amended paragraph

Replace:

> Every frame either rule permits is a **single frame**: a classic CAN data
> frame carrying one Mode 01 request for one PID — `[0x02, 0x01, pid]` and
> padding — on the request identifier of §15.2.

with:

> Every frame either rule permits is a **single frame**: a classic CAN data
> frame carrying one Mode 01 request — `[0x02, 0x01, pid]` and padding for
> a single PID, or `[1+g, 0x01, pid₁ … pid_g]` and padding for a group of
> *g* PIDs (§15.4.1) — on the request identifier of §15.2.

The three bounds that follow it are unchanged and require no edit. A group
is **one request**: one outstanding at a time, one per `interval_ms`, never
retried. A device's worst case on the bus remains one short frame per
`obd_min_interval_ms`, which is the sentence §15.1 exists to make true.

### 4.4 SPEC §15.4 — amended schedule paragraph

Replace:

> While the set is non-empty the device transmits one Mode 01 request per
> `interval_ms`, walking the list in order and wrapping … `interval_ms` is
> the spacing between consecutive requests, so one PID in a list of *k* is
> sampled every *k* × `interval_ms`.

with:

> While the set is non-empty the device transmits one Mode 01 request per
> `interval_ms`, walking the list in order and wrapping — the list is a
> schedule, not a set. **Entries are ordered and MAY repeat**: a client that
> wants engine speed sampled twice as often as coolant temperature sends
> `[0x0C, 0x05, 0x0C, 0x0F]`, and relative rates exist without per-PID rate
> fields. `interval_ms` is the spacing between consecutive requests, and a
> group (§15.4.1) is one request, so one PID in a schedule of *g* groups is
> sampled every *g* × `interval_ms`. Without grouping every PID is its own
> group and *g* is the list length.

### 4.5 SPEC §15.4.1 — new subsection

> ### 15.4.1 PID grouping
>
> A device declaring capability bit 11 (`obd_pid_grouping`) accepts a poll
> set whose PID bytes carry the `more` flag in bit 7: a byte with bit 7 set
> is grouped with the byte that follows, and a **group** is a maximal such
> run terminated by the first byte with bit 7 clear. The device transmits
> one Mode 01 request per group — `[1+g, 0x01, pid₁ … pid_g]` — and the
> schedule walks groups, not PIDs.
>
> Grouping exists because `interval_ms` spaces requests: a schedule of
> sixteen PIDs in six groups samples each PID at 1/(6 × `interval_ms`)
> rather than 1/(16 × `interval_ms`), for identical airtime — the request
> frame is padded to eight bytes either way (§15.1) — and strictly fewer
> response frames.
>
> Three rules, added to §15.4's ordered refusals after rule 5:
>
> 6. A group longer than **6 PIDs** is `bad_params`. Seven would not fit
>    the request frame, which is the only bound on grouping the device
>    checks.
> 7. Bit 7 set on the **last** byte of `pids` is `bad_params`: a group that
>    continues into nothing is not a schedule.
> 8. On a device that does **not** declare bit 11, any byte with bit 7 set
>    is `bad_params` under rule 5, which needs no amendment — such a byte
>    is a value outside `0x01`–`0x60`.
>
> **The device does not check response sizes, and MUST NOT.** Whether a
> group's answer fits a single frame is arithmetic over J1979 response
> lengths, and those tables live in the client (§15.5). A group whose
> response exceeds seven bytes is answered with a First Frame, which
> §15.5 already disposes of: it is an ordinary frame, forwarded if
> subscribed and otherwise dropped, and the transfer it opens dies for
> want of a flow control this device will not send. The device
> reassembles nothing and transmits no flow control, exactly as §15.1
> requires of it.
>
> A client sizing a group counts six bytes of budget: a single-frame
> response is `41` plus one `pid`+`data` pair per PID, and no Mode 01
> response in `0x01`–`0x60` exceeds four data bytes. Three PIDs per group
> is therefore the practical ceiling and two is common.
>
> **Grouping is functionally addressed like every other request**, so
> every ECU implementing *any* PID in the group answers, each with the
> subset it implements — and each such response is smaller than the group's
> worst case, so a client that sizes against "one ECU answers everything"
> is sizing conservatively. The probe reports the *union* of the ECUs'
> masks and not per-ECU masks (§15.3), so a client that wants per-ECU
> attribution before it groups obtains it the way §15.3 already says: poll
> the PIDs singly, watch which response identifiers answer, then install
> the grouped schedule.

### 4.6 Schema

```yaml
# schema/vtp1.yaml, control.opcodes — desc only; params unchanged
- {value: 0x61, name: OBD_POLL_SET, capability: obd,
   params: "interval_ms:u16, count:u8, pids:u8*",
   response: "",
   desc: "Replace the whole poll set; count 0 stops transmitting; bit 7 of a PID byte groups it with the next (SPEC.md 15.4.1); 0 if no OBD"}
```

Regenerate with `python3 tools/generate.py` per CONTRIBUTING.

## 5. Conformance vectors

Additive only; no existing vector changes decode, which is the §11.2 test.

| Vector | Expect |
| --- | --- |
| Info with bit 11 set and bit 10 set | accepts |
| Info with bit 11 set and bit 10 **clear** | **must-reject** — `implies` violation, encoder refuses |
| `OBD_POLL_SET` `[0x8C, 0x0D]`, bit 11 declared | `ok`; one group `(0C, 0D)` |
| `OBD_POLL_SET` `[0x8C, 0x0D]`, bit 11 **not** declared | **must-reject** — `bad_params` via rule 5 |
| `OBD_POLL_SET` ending `[…, 0x8C]` | **must-reject** — `bad_params`, rule 7 |
| A group of 7 PIDs | **must-reject** — `bad_params`, rule 6 |
| A group of exactly 6 | `ok` |
| `count` 0 with `interval_ms` 0 | `ok` — unchanged, grouping is irrelevant to the empty set |
| Grouped set before any `OBD_INFO` | **must-reject** — `bad_params`, rule 5 unchanged |

Behavioural checks for the harness: with a schedule of *g* groups, exactly
one request frame per `interval_ms`; the request frame is eight bytes
regardless of *g*; the polling flag (§15.6) and every §15.7 clearing edge
behave identically to an ungrouped set.

## 6. Compatibility

| | Old device (bit 11 clear) | New device (bit 11 set) |
| --- | --- | --- |
| **Old client** | unchanged | reads an unknown bit, ignores it, sends ungrouped sets — works |
| **New client** | sees bit 11 clear, sends ungrouped sets — works | groups |

No error path in any quadrant, because the capability is read before the
poll set is written. No record changes size. No field changes meaning,
offset, type or units. No enum member changes value. No UUID changes. The
`pids` array gains an assignment in space that today MUST be zero, which
§11.2 rule 2 permits explicitly.

**One generated vector does move**, and it is worth stating plainly because
CONTRIBUTING forbids modifying an existing conformance vector:
`reserved-bits-info-capabilities` is derived from `reserved_from`, so
assigning *any* reserved capability bit rewrites its input and expected
hex. Its subject is "whatever is reserved right now is masked", not a
recorded payload, so no previously-encoded Info decodes differently — but
the constraint is real and applies to every future bit assignment, not just
this one.

## 7. Measured gain

Implemented in `reference/peripheral/vtp_device.py` and measured by
`tools/obd_rate.py`, which drives the device on an injected clock, decodes
the CAN batches with the reference decoder, and counts per-PID samples by
distinct bus-arrival instant — two ECUs answering the same PID at the same
instant is one sample, not two.

Twelve PIDs, four of them two-byte: `0C 0D 04 05 11 0F 10 42 1F 0B 33 2F`.
A deliberately unclever first-fit packer groups them
`(0C 0D) (04 05 11) (0F 10) (42 1F) (0B 33 2F)`.

```
  ungrouped (today)      12 groups  cycle  240 ms   4.10 Hz per PID
  grouped (15.4.1)        5 groups  cycle  100 ms   9.90 Hz per PID
  overpacked (6/group)    2 groups  cycle   40 ms   0.00 Hz per PID  (744 DEAD first frames)

  gain: 2.41x  (4.10 Hz -> 9.90 Hz)
```

**2.41×, and the gain is stable across floors** — 8.20 → 20.00 Hz at a
declared floor of 10 ms, 16.60 → 39.90 Hz at 5 ms. The response budget caps
a group at three PIDs and not the six the request frame allows, so ~2.4× is
the realistic figure and 3× the ceiling.

The third row is the failure mode, exercised rather than asserted: a client
that reads only J1979's "up to six PIDs" and never counts response bytes
sends a legal poll set — rule 6 passes — and receives **nothing at all**.
Every answer is a first frame that dies for want of a flow control the
device will not send. That is the right shape for the failure: total,
immediate and unmistakable, never a plausible wrong value, and recovered by
one `OBD_POLL_SET`.

### 7.1 Against a direct reader

`tools/obd_rate.py` cannot measure an ELM327, so this is arithmetic and not
measurement, and should be checked against a real adapter:

| | requests/cycle | typical cycle | per-PID |
| --- | --- | --- | --- |
| ELM327, one PID per request | 12 | 12 × 25–40 ms | 2.1–3.3 Hz |
| ELM327, 6-PID packed (multi-frame) | 2 | 2 × 50–80 ms | 6.3–10 Hz |
| **VTP grouped, 20 ms floor** | 5 | 100 ms | **9.9 Hz** |
| **VTP grouped, 10 ms floor** | 5 | 50 ms | **20 Hz** |

Grouped VTP at the reference peripheral's own floor is roughly 3× a
single-PID ELM327 and level with the best a packing one achieves — and it
gets there without ISO-TP anywhere, so the multi-frame round trip that
makes the ELM327's packed case slow than its request count suggests does
not exist here. `obd_min_interval_ms` is a per-device declaration and not a
constant of the specification, so hardware that answers faster than the
reference peripheral moves the whole column.

**Ungrouped VTP loses to both.** That is the finding that motivates this
proposal, and it is why "VTP is an improvement" is not currently true of
this role.

It is worth saying plainly what this does **not** fix: OBD polling is
request/response at a fixed floor, and a client that needs 50 Hz needs the
CAN broadcast stream, which is where §15.9's "deliberately the floor, not
the ceiling" points. Grouping narrows a gap; it does not close a category.

## 8. Reference implementation

Landed in `reference/peripheral/vtp_device.py`:

- **Parse** (the `OBD_POLL_SET` handler): splits `pids` on bit 7 *before*
  rule 5, because rule 5 tests bits 0–6 on a device that groups. On a device
  that does not, the raw byte reaches rule 5 unamended and a grouped set is
  refused there — which is what makes an old device's refusal automatic
  rather than a second rule someone must remember to write.
- **Schedule**: `_obd_index` walks groups. The spacing test against
  `_obd_last_tx_us` is **untouched**, which is the one-line proof that
  §15.1's bus bound does not move.
- **Transmit** (`_obd_transmit`): takes a group; each ECU answers with the
  concatenation of `pid`+`data` for the PIDs its own mask covers, and is
  silent if it covers none.
- **`_obd_mode01_frame`**: one single frame while the body fits six bytes,
  an ISO-TP **first frame** past that — no consecutive frames are ever
  queued, so the synthetic car reproduces the real failure instead of
  hiding it.
- **`_obd_pid_data`**: gained true two-byte encodings for `0x10`, `0x1F`,
  `0x42` and `0x43`. Not cosmetic — a car returning one byte for every PID
  it does not model would let a client pack groups no real vehicle answers
  in a single frame, and the rate measured against it would be a number no
  car produces.

`reference/python/vtp1.py` and `reference/c/` need no change: `OBD_POLL_SET`
is a control write and the answers are ordinary `can_record`s.

**Test status.** `reference/peripheral/selftest.py` gains nine grouping
cases (rules 6, 7, 8, the six-PID boundary, one-request-per-interval, the
shared bus-arrival instant, the exact single-frame layout, the dead first
frame, and an ungrouped build refusing a grouped set). Green, along with:
162/162 conformance vectors through the Python decoder, 162/162 through the
C decoder, a clean `-Werror` C build, and the transport selftest.

## 9. Declined within this proposal

- **Physically addressed groups** (`0x7E0`+*n* rather than `0x7DF`), which
  would remove the cross-ECU subset question entirely. It is a larger
  change — §15.2's probe reports one `request_id` — and belongs in its own
  proposal.
- **A device-side response-size check.** It would need a J1979 length
  table in firmware, which §15.9 excludes, to prevent a failure §15.5
  already disposes of safely.
- **An Info field for a maximum group size.** Both bounds are constants of
  CAN and J1979; a device-specific number would be a second statement of a
  fact the client already has, which is the argument that removed
  `max_notify_bytes` (RATIONALE §8.2).
