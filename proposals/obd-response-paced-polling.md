# Proposal: response-paced OBD polling

> **SUPERSEDED — landed in SPEC §15.4.** With no third-party consumers, the
poll clock was fixed rather than extended: there is no capability bit 12
and no `OBD_POLL_SET_PACED`, because `OBD_POLL_SET` itself is paced. Kept
as the record of the argument and the measurement; RATIONALE §11.5a is
the standing rationale.

**Status**: draft — **measured against a real car; recommend accept**
**Constraint answered**: `interval_ms` forces one number to decide two
independent things (SPEC §15.4, RATIONALE §11.6)
**Reported by**: LapSmith, the OBD role's only current client, per
CONTRIBUTING's "implement it and report what went wrong"
**Wire impact**: one reserved capability bit; one new control opcode. No
record changes, no field meanings changed.
**Companion**: [obd-per-pid-rate-divisor.md](obd-per-pid-rate-divisor.md),
which this proposal makes more urgent and which should land first.

---

## 1. The problem

SPEC §15.4 paces polling on a fixed clock: `interval_ms` is the spacing
between consecutive requests. §15.1 adds that a request still unanswered
when the next transmission is due is abandoned.

So one client-supplied number decides two unrelated questions at once:

- **How fast do I sample?** — smaller is better.
- **How much latency will I tolerate before abandoning a request?** — larger
  is safer.

There is no setting that is right for both, and the client has nothing to
base the compromise on: the protocol never tells it what the car's response
latency is. A client that guesses low samples fast and abandons requests; one
that guesses high never abandons and wastes the car. LapSmith guesses
`max(obd_min_interval_ms, 25 ms)` — a constant, chosen blind.

**That coupling is the defect.** It is not that the clock is fixed.

## 2. What is not wrong, and must survive

Stated so review does not re-litigate them:

- **§15.1's audit bound was the starting position**, and this proposal ends
  up trading it deliberately rather than preserving it — see §3.2, which is
  where the argument belongs and where a reviewer should attack. It is traded
  only for the new opcode; `OBD_POLL_SET` keeps the bound untouched, so a
  vendor unwilling to make the trade declines bit 12 and loses nothing.
- **One request outstanding.** It is what keeps a device out of another
  tester's ISO-TP transfer.
- **No retry.** Retrying an unanswered diagnostic request on a live bus is
  how a tester becomes a fault.

This proposal is about **when the next request goes out**, and nothing else.

## 3. The rule

A device declaring the capability transmits the next request when the previous
one has been **answered**, no sooner than `interval_ms` after the previous
transmission, and abandons an unanswered request after ISO 15765-4's P2max.

```
spacing = max( interval_ms, min(car_latency, P2MAX) )        P2MAX = 50 ms
```

`interval_ms` changes from *the exact spacing* to *a minimum spacing*, and
**0 means no client-imposed limit** — go as fast as the car answers. It stays
a client-supplied `u16` in the same position, so no layout moves.

Three things about this shape, and the third is why it is the one proposed:

**One field, and it is the one that already exists.** An earlier draft split
this into a device-published floor and a client-supplied limit. That was
wrong. A device is plugged into a car it has never met, so any rate it
publishes as *safe* is a guess wearing the costume of a fact — the same
criticism this proposal makes of a client hard-coding 25 ms. And once pacing
makes the car the governor, a device-side floor governs nothing that the car
is not already governing. It was complexity buying a number nobody could
honestly fill in.

**The give-up timer is not a policy choice.** It is ISO 15765-4's P2max, the
interval a tester waits for a response, which every OBD tool implements and
which this role's own J1979 foundation already assumes. Taking it from the
standard rather than from a field means the specification states one constant
instead of asking a vendor to invent one, and means an unanswered PID cannot
stall the schedule.

**It is the same field with the guessing removed.** For any client setting
`interval_ms` *above* the car's latency — which is every client being
defensive today — the behaviour is **identical to the current fixed clock**.
It diverges only where the car is slower than the interval: today the device
fires anyway and abandons, under this rule it waits, up to P2max. So this is
not a second pacing model bolted alongside the first. It is the first one,
with the client no longer required to guess a number it cannot know.

`obd_min_interval_ms` is untouched and keeps its present meaning for
`OBD_POLL_SET` (§5). The paced opcode simply does not consult it.

### 3.1 "Answered" is the first response, and late ones are still data

A functionally-addressed request is answered by every ECU implementing any
PID in it, and §15.3 reports only the **union** of the ECUs' masks — so a
device *cannot compute* which ECUs should answer a given group. Any rule of
the form "wait for all expected responders" is unimplementable. This one
waits for the first response on a probe-reported response identifier.

A second ECU's answer arriving after the next request has gone out is
harmless, and this is the non-obvious part: §15.5 puts decoding in the client
keyed on the PID *in the response*, and every `can_record` carries its own
bus-arrival timestamp. **The client never correlates a response to a
request**, so a late answer is ordinary valid data at a known time. Reviewers
whose first instinct is that overlapping responses are a problem should read
§15.5 before objecting.

One wrinkle: "the answer arrived" means *an* answer on a diagnostic response
identifier, and §15.5 already notes those carry other testers' answers too —
a splitter with an insurance dongle in it, or a vehicle-internal module making
its own requests. A device can be paced by traffic it did not cause and run
faster than intended. The effect is bounded: the other requester keeps its own
schedule and does not accelerate because this one did, so it is rate inflation
rather than a runaway, and a client that cares sets a non-zero `interval_ms`.

### 3.2 What the auditable claim becomes

§15.1's bound is a rate — one short frame per `obd_min_interval_ms`, readable
from Info before anything is transmitted — and this proposal gives that up for
the paced opcode. That is the real cost and it should not be smuggled past
review.

**What replaces it is a discipline rather than a number:** *this device sends
at most one diagnostic request at a time, waits for the answer or 50 ms,
never retries, and never transmits a frame the client did not ask for.*

That is worth arguing is the better claim. A rate was always a proxy for "will
this thing disturb my car", and a bad one: one frame per 20 ms sounds
alarming and is harmless, while the property that actually keeps a device from
becoming a fault on a bus is that it never has two requests outstanding and
never retries. The discipline is checkable by inspection, statable without
knowing the car, and true of every conforming device — none of which the rate
managed, because the rate depended on a number the vendor had to guess.

**The rate bound survives unchanged for `OBD_POLL_SET`.** A device that wants
to make the §15.1 claim keeps making it by not declaring bit 12. This is a
capability a vendor opts into, and the trade it makes is explicit.

The remaining bound under pacing is the car itself: a device that waits for an
answer cannot outrun the ECU replying to it, which is how every dedicated
tester on the market has always worked.

## 4. Evidence

`tools/obd_pacing_model.py`. §4.1 and §4.2 are a model — the two rules are
simple enough that it is exact rather than approximate. §4.3 is a measurement
from a real car, and it is what decides the recommendation.

### 4.1 Why a device-side floor had to go

```
gain = I / clamp(L, F, I)     bounded above by I/F
```

| L (ms) | I=20 | I=25 | I=50 | I=100 |
| --- | --- | --- | --- | --- |
| 3 | 1.00× | 1.25× | 2.50× | 5.00× |
| 18 | 1.00× | 1.25× | 2.50× | 5.00× |
| 50 | 1.00× | 1.00× | 1.00× | 2.00× |

*(F = 20 ms, the reference peripheral's floor.)*

Read `F` here as *any* lower bound on spacing, whether the device published
it or the client supplied it. Three things fall out, and they are what killed
the device-published version of this design:

- **At `I == F` the gain is exactly 1.00×, for every latency.** Pacing buys a
  client that already polls at the floor precisely nothing.
- **The gain does not depend on `L` at all once `L ≤ F`.** It is `I/F` — a
  ratio of two numbers the client and the vendor already control.
- **Pacing does nothing for abandonment.** A request unanswered at `t + I` is
  abandoned under *both* rules. §15.1's silent-drop behaviour is a property
  of `I` against `L`, and this proposal does not touch it.

For LapSmith specifically: `I = 25` against the reference `F = 20` caps the
gain at **1.25×** — and the identical rate is available today by setting
`I = 20`, with no protocol change at all.

**This is the whole case for §3's single field.** A floor set by anyone who
cannot see the car is a cap on the car's own speed, and the device is exactly
the party that cannot see the car. Under §3 the only lower bound is the one
the client chose and can set to 0, so the table's first column stops being
somewhere a device can strand a client by accident.

### 4.2 So the real comparison is against an *informed* client

Measuring pacing against whatever interval a client happened to guess
flatters it. The honest comparison is against a client that has been *told*
the latency — the brief's own open question 5.2 — and sets `I` just above the
tail it must tolerate:

| latency shape | mean | p99 | vs blind I=25 | **vs informed client** |
| --- | --- | --- | --- | --- |
| tight fast car (3±1 ms) | 3.0 | 4 | 5.00× | **1.00×** |
| tight gateway (18±2 ms) | 18.0 | 20 | 1.39× | **1.11×** |
| bursty gateway (8 ms, 5% at 40) | 9.6 | 40 | 2.82× | **4.17×** |
| heavy tail (5 ms, 1% at 80) | 5.8 | 5 | 4.81× | **1.00×** |

*(F = 5 ms. Deterministic shapes, no RNG, so the numbers are reproducible.)*

The last row read 13.91× in an earlier draft, on a percentile that
indexed one past nearest-rank and returned the outlier itself as the
99th of 200 samples — which two outliers are not. Corrected it says the
opposite: a distribution that is *mostly* fast with a rare spike is
served perfectly well by an informed client, because the p99 it tunes to
is still 5 ms. It is sustained spread, not rare outliers, that pacing
wins on — which the bursty row shows and §4.3's real car confirms.

**This is the finding.** Against a tight distribution, pacing beats a
well-informed client by 0–11% and a latency report would do as well for far
less. Against a *variable* one it wins by 4–14×, and nothing a client can do
with a single static number comes close — because an informed client must set
`I` to the tail and then pays the tail **on every sample**, while pacing pays
the actual latency each time.

### 4.3 Measured: one real car

A LapSmith session supplied the missing distribution — Lotus Exige 410,
Donington Park, 9m23s, OBDLink MX+, 5,947 intervals. LapSmith's ELM327 loop
is response-paced, so the gaps between distinct capture instants *are* round
trips; `tools/obd_pacing_model.py --from-session obd.csv` reproduces this.

| | ms |
| --- | --- |
| median | 62.4 |
| mean | 94.7 |
| p95 | 227.8 |
| p99 | 640.5 |
| max | 1250.9 |

**The distribution is heavily tailed: p99/median = 10.3×.** 5.8% of intervals
exceed 200 ms, 66 exceed 500 ms. LapSmith's own `obd.update_rate` series
agrees — median 12.9 Hz, but p5 = 2.9 Hz and min 2.05 Hz, so about a
twentieth of the session runs at a fifth of nominal.

Against an informed client, on this distribution:

| | advantage over informed |
| --- | --- |
| **measured, real car** | **6.83×** |

**The tail is not the phone or the radio.** The RaceBox GPS — a *different*
BLE peripheral on the same handset — held a median gap of 40.0 ms with p99
41.0 and max 42.0 across the whole session. Of the 344 OBD stalls over 200
ms, the number during which GPS also stalled is **zero**. That rules out the
one confounder that would have invalidated this measurement.

Caveats, because they bound what this proves:

- **Sweep-level, not per-request.** A sweep may carry several PIDs and
  LapSmith's tiering lengthens some by design. Dividing by requests-per-sweep
  scales median, mean and p99 together, so **the 6.83× is invariant** and only
  the absolute milliseconds are unknown.
- **An ELM327-class adapter is in the path** and a VTP dongle would not be. The
  GPS control rules out the handset, but not the MX+ itself. Stalls correlate
  only weakly with engine state (median rpm 4966 during stalls against 4646
  otherwise) and arrive mostly isolated — 106 of 127 runs are a single
  interval — which argues against an adapter buffer storm but does not settle
  it.
- **One car, one session, one adapter.** A second car would strengthen this a
  lot, and a gatewayed modern car is the case most likely to differ.

### 4.4 What that means for the design case

Pacing's value is that it **decouples the drop-rate decision from the
sample-rate decision**. Today `interval_ms` sets both; under pacing `I` sets
only how long you will wait before giving up, and `F` and the car set how
fast you go. That is a better description of what this proposal is for than
"polling is too slow", and it is the version §1 states.

It also means the question the brief poses — "do cars answer in 2 ms or
18 ms?" — **was the wrong question.** The mean does not decide this; the
variance does. The rule was:

- tail ≈ mean → **decline this proposal**, report the latency instead (§6.1)
- tail ≫ mean → **accept it**; no static interval can serve both ends

§4.3 measured tail/median = 10.3× and an advantage of 6.83× over a client
that already knows everything a latency report could tell it. **That is the
accept branch, and not marginally.** A latency report (§6.1) remains worth
having — it is how a client learns it is misconfigured — but on this evidence
it is not a substitute.

## 5. Signalling

Two separable things: the device says it *can*, the client says it *wants*.

**Capability bit 12, `obd_response_paced`, `implies: [obd]`** — reserved space
per Appendix A, the same shape as bit 11.

**A new opcode, `0x62 OBD_POLL_SET_PACED`**, parameters identical to
`OBD_POLL_SET`. §11.3 names new control opcodes as this protocol's
general-purpose extension point, the request layout does not move, and a
device not declaring bit 12 answers `unsupported_opcode` — the correct answer
rather than silently different pacing.

Declined:

- **A trailing mode byte on `OBD_POLL_SET`.** Only ever sent to a bit-12
  device, so it breaks nobody, but it makes the opcode's parameter length
  conditional on a capability — a new kind of parse rule in this protocol.
- **A flag bit in `interval_ms`.** `0x8000` is a legal 32.7 s interval, so the
  bit is not free, and §15.4.1 has already spent this repository's tolerance
  for stealing a high bit.
- **Redefining `interval_ms` for all devices at a later minor.** §11.4 forbids
  changing the meaning of an existing field. That is a VTP/2 proposal.

Out of scope and unaffected: the probe (§15.2) stays clock-paced, §15.6's
polling flag, §15.7's stop and reset semantics. Grouping (§15.4.1) composes —
pacing makes each request as fast as the car allows, grouping reduces how many
a cycle needs, and they multiply.

## 6. Open questions

**6.1 Would a latency report be the better change?** §4.2 says it depends
entirely on variance, and this is the alternative to price, not an addition.
Records are closed (§11.3) so it needs an opcode of its own — but it is
strictly smaller than pacing, leaves the transmit loop untouched, and gives
the client something pacing does not: the ability to *know* it is
misconfigured. Pacing hides the problem by making it not matter; a report
lets a client fix it. **If the measurement in §7 comes back tight, this
proposal should be withdrawn in favour of that one.**

**6.2 Is P2max the right constant, and should it be stated or configurable?**
§3 takes 50 ms from ISO 15765-4 rather than adding a field, which is right if
50 ms is right. Two doubts: a gatewayed car legitimately slower than P2max
would have every request abandoned, and §4.3 measured a p99 of 640 ms on a
real car — though at sweep rather than request granularity, so it may sit well
under P2max per request. Run against the §4.3 session, the
share of intervals P2max would abandon, by assumed requests-per-sweep *N*:

| N | median | p99 | over 50 ms |
| --- | --- | --- | --- |
| 1 | 62.4 | 640.5 | **100%** |
| 2 | 31.2 | 320.2 | 28.7% |
| 4 | 15.6 | 160.1 | 5.8% |
| 6 | 10.4 | 106.7 | 2.1% |

So P2max bites on this car for any plausible *N*, and catastrophically for
small *N*. That is not automatically wrong — abandoning at 50 ms and moving on
beats stalling 640 ms, which is exactly what LapSmith's ELM327 loop does
today, and §6.6 may mean the late answer arrives anyway. But it makes
requests-per-sweep the measurement that matters most (§7), and it means the
give-up constant cannot simply be adopted from the standard without checking
it. **Currently the largest open question in this proposal.**

**6.3 A schedule mixing answered and unanswered PIDs** degrades to clock
pacing for the entries nothing implements, which is correct but should be
stated rather than discovered.

**6.4 Interaction with `can_max_frames_per_s`.** A faster loop means more
response frames, and on a shed-prone device this could push it into shedding
that §15.6's flag does not distinguish from anything else.

**6.5 `interval_ms` is now slightly the wrong name.** Renaming is forbidden
within major 1 (§11.4). This was accepted rather than missed.

**6.6 Does abandonment actually lose the sample?** §15.1 abandons an
unanswered request, but the ECU may still answer, and §15.5 delivers that
frame as ordinary data. Whether the client loses a sample or merely receives
it late — and irregularly spaced, which for a lap timer is its own problem —
is ECU behaviour the specification cannot control. The brief describes this
as a silent data loss; it may be silent *jitter* instead, which is less
serious and differently fixed. **Unresolved, and it changes how §1's second
half should be described.**

## 7. Evidence status

The grouping proposal earned its place by measuring first, and this one has
now been held to the same standard. §4.3 is a real car, and it answers the
question the brief said must not be skipped.

What would still strengthen it, in order of value:

1. **A second car**, ideally a modern gatewayed one — the case most likely to
   behave differently, and the one where a 640 ms p99 would be least
   surprising.
2. **Requests per sweep**, which converts §4.3's ratios into absolute
   milliseconds and would say whether a VTP client polling at 25 ms on this
   car is transmitting faster than it answers.
3. **A VTP dongle on a real bus**, which removes the ELM327-class adapter from
   the path and is the only way to attribute the tail to the car rather than
   the MX+.

None of those change the recommendation; they change how confident it is.

The normative changes are still **not** in this branch, because the shape of
the wire format (§5) is a review decision rather than a measurement one, and
because `obd-per-pid-rate-divisor.md` should land first (§5 of that
proposal). The evidence gate this proposal set for itself is now met.
