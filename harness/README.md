# Conformance harness

A Bluetooth central that connects to your device and checks it against
[SPEC.md](../SPEC.md). It runs on Windows, macOS and Linux from one codebase.

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
| §9 | the entire control plane — what a device *answers*, not what a client decodes |
| §4.1 | that the attribute table is the fixed one, and that an absent capability leaves an *inert* characteristic rather than no characteristic |
| §8.2 | that `seq` starts at 0 on the first notification of a connection |
| §8.1 | that the three streams are on one clock rather than three |
| §9.2 | that the subscription table is empty after a reconnect |
| §9.3 | that a frame matching two subscriptions is forwarded once |
| §9.8 | that a rate answered `ok` is the rate Info then reports |
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

Three things are permanently in it:

- **§2.1–§2.3** — link-layer payload, PHY and connection interval. No desktop
  operating system exposes these to an application. The harness checks the
  device's own `GET_LINK_PARAMS` report for internal consistency and against the
  one figure the host does know, the ATT MTU. A device that misreports cannot be
  caught by any means this specification provides. §12.1 says the same thing.
- **§8.1's clock discipline and §6.1's timing bounds.** The host's scheduler and
  Bluetooth stack sit between the device and every arrival time measured here,
  and they are worth tens of milliseconds against a clock specified in
  microseconds. Ordering and internal consistency are checked; accuracy is not.
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

### Adversarial requests

By default the harness sends malformed control requests — truncated parameters,
unallocated opcodes, a tag that is already outstanding, a handle that names
nothing, a Monitor write missing a slot. This is the direct test of §1.1: does
your device reject malformed *whole*, or decode a prefix and carry on?

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
word missing a bit another bit requires — and each exists
because some check here claims to catch it. The selftest fails if a fault is
defined with no check named against it. It is the argument
`tools/check_corpus.py` makes about the byte vectors, applied to the rules that
live outside them: a check nothing can break is a check that does not work, and
it will pass silently forever.

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
is never failed for it. `raise Skip(...)` when something cannot be asked here and
say why — the reason is printed under Not verified rather than swallowed.
`raise Observe(...)` for a measurement that is nobody's pass or fail, so the
report cannot accumulate green ticks for things nothing was asserted about.

Then add a fault to `transport.FAULTS` and an entry to `selftest.CAUGHT_BY`, or
the selftest will tell you that you have made a claim nobody is holding to
account.
