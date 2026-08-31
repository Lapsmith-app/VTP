# Conformance harness

**For anybody building a VTP/1 device.** A Bluetooth central that connects to
your firmware and checks it against [SPEC.md](../SPEC.md), from Windows, macOS
or Linux, out of one codebase.

It is the only thing in this repository that tests a *device* rather than a
piece of code. `conformance/` hands your decoder bytes and your encoder
structures, offline, and needs both to be buildable as command-line programs —
which firmware generally is not. This connects to the device you actually
built, over the radio, and tests what it does.

```sh
uv run vtp1-harness
```

That is the whole thing: it scans, finds the VTP/1 device that is advertising,
reads Info, works out which roles the device declares, exercises exactly those,
and prints what happened next to the section of the specification that required
it.

---

## Why this exists

`conformance/run.py` decodes bytes. Every requirement expressed as a byte
layout, a validity rule, an enum value or a length check is mechanically
testable that way, and passing the corpus is evidence about all of them.

It cannot ask a device a question. So nothing in this repository tested:

| | |
| --- | --- |
| §9 | the entire control plane — what a device *answers*, not what a client decodes, including `busy` when a client pipelines and the requirement that a request refused `busy` did not take effect |
| §4.1 | that the attribute table is the fixed one, and that an absent capability leaves an *inert* characteristic rather than no characteristic |
| §8.2 | that `seq` starts at 0 on the first notification of a connection |
| §8.1 | that the three streams are on one clock rather than three — in offset, and in rate: a per-sensor timer zeroed at boot agrees at connect and diverges from there |
| §9.1 | that the subscription table is empty after a reconnect |
| §9.1 | that a subscription's identity is the `(id, mask)` pair the client wrote — bits 30 and 31 excluded, `id & mask` not substituted, and a re-install answered `ok` on a full table |
| §9.2 | that a frame matching two subscriptions is forwarded once |
| §6.8 | the whole of what a subscription's schedule *is*: the first matching frame, the interval after it, that a displaced subscription keeps both, and that a byte-identical re-install costs the client nothing |
| §6.3 | that `dropped` counts frames the device accepted and discarded, and not ones it filtered as instructed |
| §9.6 | that a rate answered `ok` is the rate Info then reports |
| §13 | Monitor, which is a thing a device *receives* |
| §5.1 | that a field whose validity bit is clear is actually zero |

Every one of those is a real firmware bug that produces payloads decoding
perfectly. This harness is where they are caught.

It never restates a rule. Every payload it sees goes through
[`reference/python/vtp1.py`](../reference/python/vtp1.py), the same decoder the
corpus tests, reading the same `schema/vtp1.yaml` — and the tables it works
from are the schema's too: §4.1's attribute matrix, the capability
implications, the capacity fields each bit governs, and which capability owns
each opcode. A hand-copy of any of those here would be one more statement of a
fact the specification has just finished reducing to one. What the harness adds
is everything a decoder cannot see: what a device did over time, and what it
said when asked.

---

## Running it

### Any platform

```sh
uv run vtp1-harness                      # from a clone of this repository
uv run vtp1-harness --scan               # just list what is advertising
uv run vtp1-harness --seconds 30         # watch the streams for longer
uv run vtp1-harness --markdown report.md # something to paste into an issue
```

Exit code `0` means nothing this harness can test was violated, `1` means a MUST
was, `2` means the run did not finish.

### macOS

Nothing extra to do, but here is what happens, because it is surprising.

macOS kills any process that touches CoreBluetooth without an
`NSBluetoothAlwaysUsageDescription` in its Info.plist — SIGABRT, exit 134, no
output at all. It is not a permission you can grant afterwards: the process dies
before it can ask, so it never appears in System Settings and that pane has no
button to add one.

The harness detects this in a child process, builds a signed app bundle in
`~/Library/Caches/vtp1-harness`, and relaunches itself through LaunchServices
with `--stdout /dev/stdout`, so the output still lands on your terminal. You
type one command; the first run asks for Bluetooth permission.

Two things break it:

- **A framework Python.** Homebrew's and Apple's identify as
  `org.python.python`, which makes macOS ignore the wrapper's Info.plist
  entirely. Use `uv run`, whose interpreters are standalone builds and adopt
  the bundle they are placed in. The harness detects this and says so.
- **No login session.** LaunchServices needs one, so run this from Terminal or
  iTerm rather than from an editor's task runner or an automation shell.

Two more things to know: devices have no MAC address here — `--scan` prints the
per-host UUID to pass to `--address`, and it differs on every Mac — and macOS
caches a peer's GATT table with no API to clear it. If you have reflashed and
the layout looks stale, turn Bluetooth off and on again.

### Windows

Windows 10 build 1709 or later. If the device requires an encrypted link, pair
it in Settings first.

Windows also caches a peer's GATT table, which matters more here than anywhere
else: a device under development changes its GATT table constantly, and a stale
cache makes this harness report a layout your firmware no longer has. It asks
for an uncached read by default. If the layout still looks wrong, remove the
device in Settings and pair it again.

### Linux

BlueZ 5.55 or later, adapter powered on. Linux is also the only platform where
`btmgmt` can set the connection parameters and PHY that §2.1–§2.3 ask for, so it
is the best place to test them by hand.

---

## What it checks, and what it cannot

Checks are grouped by the section that requires them and reported that way. Run
with `-v` to see skips and evidence.

The report ends with a **Not verified** section, and it is not boilerplate — it
is assembled from the run, so a check skipped for a reason specific to your
device appears there rather than disappearing into a count.

Four things are permanently in it:

- **§2.1–§2.3** — link-layer payload, PHY and connection interval. No desktop
  operating system exposes these to an application, so the harness checks the
  one figure the host does know, the ATT MTU, and nothing else. §12.1 says the
  same thing.
- **§8.1's clock discipline and §6.1's timing bounds.** The host's scheduler and
  Bluetooth stack sit between the device and every arrival time measured here,
  and they are worth tens of milliseconds against a clock specified in
  microseconds. Ordering and internal consistency are checked — the streams
  keeping one *rate* included, down to a few thousand ppm over a default
  window — but accuracy is not, and crystal-grade drift of tens of ppm needs
  a far longer `--seconds` to rise above that jitter; the report says what
  its window could resolve.
- **§9.7's numbers themselves.** The harness checks that a device declaring
  `power` answers `GET_POWER`, that it reports something valid, and that what it
  reports obeys §9.7's rules. Whether the pack is actually two-thirds full is
  not observable from this side of the link, any more than §2's link
  parameters are.
- **§6.8's bound, and the eviction order at it.** A device MAY bound the
  scheduling state it keeps, and nothing distinguishes a device that has never
  reached its bound from one that has no bound at all. What the harness would
  need to reach it is thousands of distinct identifiers on the bus, which is
  the vehicle's business rather than this tool's. The rule it cannot test is
  that displaced state is reclaimed before a governing subscription's frame is
  shed.
- **§13.5's freshness expiry.** Every declared channel carries a `max_age` and
  the harness checks it is non-zero, but what happens when one lapses happens on
  the device's own display and puts nothing on the wire. Stop writing and watch
  the screen.

A clean run is evidence about a device. It is not a conformance certificate, and
the report says so on its last line.

### CAN needs something to listen to

CAN checks need frames, and frames need a subscription. If the device declares
`masked_subscriptions` the harness asks for everything with a mask of zero. If
it does not, there is no way to ask for "whatever is on the bus" and no way to
guess your identifiers, so name them:

```sh
uv run vtp1-harness --can-id 0x1A0 --can-id 0x2C4
```

The §6.8 scheduling checks reprogram the table and then watch one identifier:
they install a `periodic` subscription slow enough that the only frame it owes
is §6.8's first one, and read what arrives after that. They need traffic on
that identifier and nothing else — no second node, no injected frame — and they
put the table back the way they found it. Each one watches the identifier
arrive before it asserts anything, and skips rather than fails when the signal
has stopped: a quiet bus is not a defect, and a check that cannot tell the
difference is worse than no check. They measure by bus-arrival timestamp
rather than by arrival window, because a batch is flushed on the device's own
schedule (§6.2) and frames accepted before a control request are delivered
after it.

What no desktop harness can do is put a frame on the bus, which bounds one
check. `can.format_bit_is_identity` installs a subscription on the *other*
format of an identifier the device is already forwarding and requires that
nothing arrives under it — the half of §9.1 that a controller keeping the
format in a flags word gets wrong. The other half, that an extended
subscription matches extended frames, is only exercised if the vehicle
carries them.

### Adversarial requests

By default the harness sends malformed control requests — truncated parameters,
unallocated opcodes, a tag that is already outstanding, an id and mask that
name nothing, a Monitor write missing a slot. This is the direct test of §1.1:
does your device reject malformed *whole*, or decode a prefix and carry on?

If your firmware is not ready to survive that, `--no-adversarial` turns it off,
and the report says which checks it cost you.

---

## Testing the harness itself

A conformance tool nobody has tested is an opinion.

```sh
python3 harness/selftest.py     # needs no Bluetooth adapter
```

This does two things. It runs every check against the software peripheral in
`reference/peripheral/` — in-process, no radio — and requires a clean report,
because a tool that fails a conforming device is worse than no tool: the first
thing anybody does with a red result is start changing firmware.

It then runs against devices that declare only some of the roles — GPS alone,
GPS with Control — and requires **no failures** there either. §4.1 is precise
about what a partial device still owes a client, and most of what the harness
checks has to become a skip rather than a failure. It is also the only way to
reach the inert half of the specification: a CCCD write on a stream whose
capability bit is clear, and an opcode whose owning capability the device has
not declared.

Then it seeds one specific defect at a time and requires that a specific check
catches it:

```sh
uv run vtp1-harness --fault list
uv run vtp1-harness --loopback --fault seq_starts_at_one
```

Each entry in `transport.FAULTS` is a real mistake — a sequence number that
starts at 1, a `TIME_SYNC` that reads its clock once and reports it as both
timestamps, a subscription table that survives a reconnect, a `capabilities`
word missing a bit another bit requires, a request answered `busy` and applied
anyway — and each exists
because some check here claims to catch it. It is the argument
`tools/check_corpus.py` makes about the byte vectors, applied to the rules that
live outside them: a check nothing can break is a check that does not work, and
it will pass silently forever.

The selftest holds that argument in **both** directions, and the second one is
the one that matters:

- No fault may be defined with no check named against it — otherwise the fault
  is a claim nobody is holding to account.
- **No MUST or SHOULD may exist with no fault against it**, unless
  `selftest.NOT_SEEDED` says why none is possible. Without this half,
  `transport.FAULTS` decides what "detects every defect it claims" means, and
  the FAULTS table is written by whoever wrote the checks. Forty-one checks sat
  in the registry having never once been observed to fail, including the one
  covering §13.3's declaration format — which is exactly the shape of defect a
  device that predates a spec change ships.
- **No excuse may outlive its reason.** An entry in `NOT_SEEDED` claims no
  fault can make that check fail, which is a statement about the whole suite,
  so every fault run is checked against it: if an excused check fails, the
  excuse is already false and the run says so. A fault that breaks the
  conversation rather than one rule — `no_tag_echo` leaves nothing
  correlatable, so every check awaiting a response fails — belongs in
  `selftest.CASCADING` and is exempt, because "it failed while the envelope was
  broken" is not evidence that the check works.

The clean run is also held to an **expected-skip baseline**
(`selftest.EXPECTED_SKIPS`). A skip is the harness saying nothing, and a check
that quietly starts skipping for every device — a renamed state key, a
capability probe that stopped matching, a refusal newly read as "not applicable"
— otherwise looks exactly like a passing run.

`--loopback` is also the fastest way to see what a passing report looks like
before you point the harness at hardware.

---

## Layout

```
vtp1_harness/refdec.py     bridge to the reference decoder and the schema
vtp1_harness/transport.py  the only platform-specific file: bleak, and the loopback
vtp1_harness/session.py    one connection: stream logs, request/response correlation
vtp1_harness/runner.py     ordering, skipping, and what a failure means
vtp1_harness/report.py     console, JSON and Markdown
vtp1_harness/macos.py      the app bundle, and why it is needed
vtp1_harness/checks/       one module per part of the specification
selftest.py                the harness, tested against a device it can break
```

### Adding a check

```python
@check(id="can.something", section="6.8", phase="streams", severity="MUST",
       requires=("can",), title="One sentence, in the specification's terms")
async def can_something(s):
    if <the device did the wrong thing>:
        raise Fail("what it did, and why the specification asks otherwise",
                   payload=offending.hex())
```

`requires` names capability bits from §4, so a device that never claimed a role
is never failed for it. `Fail(..., severity="MUST")` overrides the check's own
severity for one finding, which a SHOULD check needs when its failure mode is
worse than the rule it is mainly about — surfacing an out-of-range reading is a
SHOULD, acting on one breaks a MUST. `raise Skip(...)` when something cannot
be asked here and say why — the reason is printed under Not verified rather than swallowed.
`raise Observe(...)` for a measurement that is nobody's pass or fail, so the
report cannot accumulate green ticks for things nothing was asserted about.

Then add a fault to `transport.FAULTS` and an entry to `selftest.CAUGHT_BY`, or
the selftest will tell you that you have made a claim nobody is holding to
account. If the check genuinely cannot be made to fail against the software
peripheral, say so in `selftest.NOT_SEEDED` with the reason; an entry there is a
debt with an explanation attached, not a dispensation, and shortening that list
is how this harness gets better.

### Installing it, and what an install carries

`pip install .` builds a wheel that carries its own copy of the schema, the
reference decoder and the software peripheral (see the force-include block in
`pyproject.toml`), because the harness has to work from a machine that is not
this repository. That copy is a snapshot: a wheel built from a stale tree tests
last week's peripheral against last week's rulebook, agrees with itself
completely, and reports green.

```sh
python3 tools/check_package.py    # run with the interpreter that has the wheel
```

compares every bundled file against this repository and fails if they differ.
CI builds the wheel, runs that, and then runs the *packaged* harness against the
*packaged* peripheral. If you are debugging firmware against an install and the
answers look a version behind, run it.
