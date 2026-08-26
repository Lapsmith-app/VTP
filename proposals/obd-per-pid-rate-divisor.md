# Proposal: per-entry rate divisors for the OBD poll set

> **SUPERSEDED — landed in SPEC §15.4.2.** Shape A was taken, with the
divisor byte following each group's terminating PID byte. Kept as the
record of the argument; RATIONALE §11.6 is the standing rationale.

**Status**: draft
**Constraint answered**: a PID can be made faster than the cycle and never
slower (SPEC §15.4, RATIONALE §11.6)
**Reported by**: LapSmith, per CONTRIBUTING's "implement it and report what
went wrong"
**Wire impact**: one reserved capability bit; one new control opcode. No
record changes, no field meanings changed.
**Companion**: [obd-response-paced-polling.md](obd-response-paced-polling.md).
This proposal should land **first or simultaneously** — see §5.

---

## 1. The problem

SPEC §15.4 carries one `interval_ms` for the whole schedule and deliberately
no per-PID rate field. RATIONALE §11.6 gives the reason and it is a good one:
a poll interval *generates* bus traffic, so N independent rates would make the
device's bus load the sum of a list only the client knows. Relative rates
survive as list composition — entries are ordered and may repeat, so
`[0C, 05, 0C, 0F]` samples engine speed twice per cycle.

Repetition is a one-way tool. **A PID can be made faster than once per cycle
and can never be made slower.** Everything in the schedule is polled at least
at the cycle rate, whether it needs to be or not, and the only currency for
buying a ratio is `obd_poll_slots`.

### What that costs in practice

From LapSmith, on a 16-slot device: doubling six fast channels needed
`2 × 6 + 6 = 18 > 16` slots, so the doubling was **skipped whole** and ambient
air temperature was polled exactly as often as engine speed. LapSmith has
since made the repeat adaptive and bought a 2:1 ratio by **dropping two slow
channels from the schedule** — because no PID can go below once per cycle, the
only thing left to give was breadth.

That is the failure shape: a client that wants one channel *slower* pays for
it in channels it can no longer read at all.

For contrast, LapSmith's older ELM327 stack expresses the same thing in two
integers — fast every sweep, medium every 5th, slow every 20th — and needs no
schedule slots for it.

## 2. Why §11.6's argument does not forbid this

§11.6 refused **per-PID rates**, on the grounds that N independent intervals
make the bus load a sum only the client knows, with collisions the device must
arbitrate. Every word of that is right, and a divisor is not that.

A divisor **cannot increase** the request rate. The schedule still emits one
request per `interval_ms`; a divisor only lets an entry be *skipped*. So:

- The load-bearing sentence — "at most one request per `interval_ms`, ever" —
  survives **verbatim**, which is the same test §15.4.1 had to pass.
- There are no collisions to arbitrate, because there is still exactly one
  schedule and one cursor.
- §15.1's worst case on the bus is unchanged, and so is the auditability
  argument that rests on it.

The asymmetry is the whole point: repetition already lets a client go *faster*
than the cycle and adds traffic. A divisor lets it go *slower* and removes
traffic. Refusing the one that only ever reduces load, while permitting the
one that increases it, is backwards.

## 3. Sketch

Not settled — this proposal is about establishing that the gap is real and
worth closing. Two shapes worth pricing:

**A. A divisor byte per entry.** `OBD_POLL_SET_DIVIDED` carries `count` pairs
of `(pid, divisor)`. An entry with divisor *d* is transmitted on every *d*-th
pass of the schedule and skipped otherwise; *d* = 1 is today's behaviour and
*d* = 0 is `bad_params`. Costs one byte per slot, composes with §15.4.1's
grouping if the flag stays in the PID byte, and reads exactly like the
ELM327's two integers.

**B. A divisor per group.** Cheaper on the wire where grouping is in use, and
the natural unit if a client's slow channels are already grouped together —
but it forces grouping decisions and rate decisions to be the same decision,
which they are not.

Signalling follows §15.4.1's precedent either way: a capability bit with
`implies: [obd]`, and a new opcode so a device without the bit answers
`unsupported_opcode` rather than silently ignoring the divisors.

## 4. What a client must still not be able to say

A divisor must not become a second interval:

- **No entry may be skipped forever.** A divisor is a `u8`, so the slowest an
  entry can go is one pass in 255 — bounded, statable, and readable from the
  schedule the client installed.
- **The device keeps one cursor.** A skipped entry advances the schedule
  without transmitting; it does not get its own timer.
- **`obd_poll_slots` still counts entries.** The capacity a client reads in
  Info means what it meant.

## 5. Why this should land before response pacing

`obd-response-paced-polling.md` shortens the cycle. Everything in the schedule
speeds up with it, **including the things that should not.** LapSmith's
current schedule on a 5 ms car with response pacing would poll ambient air
temperature at roughly 20 Hz — a channel that changes on the timescale of
weather, sampled twenty times a second, on a bus shared with a moving car's
ECUs.

So pacing makes this problem measurably worse, and the two are complements
rather than alternatives: pacing shortens the cycle, divisors let the slow
tier fall away as it shortens. **Landing pacing alone is the bad ordering.**

## 6. What has not been done

No measurement, and unlike the pacing proposal this one may not need much: the
cost is visible in LapSmith's schedule arithmetic today (§1), and does not
depend on any property of a real car. What it does need is a decision between
§3's two shapes, which is a wire-format question for review rather than a
measurement.
