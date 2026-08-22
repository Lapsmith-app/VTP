# VTP/1 — Vehicle Telemetry Protocol over Bluetooth LE

An open GATT contract for carrying **GNSS position, CAN-bus frames and inertial
data** from a hand-built motorsport logger to a phone app — on one link, on one
clock, with no lossy packing.

- **[SPEC.md](SPEC.md)** — the normative specification
- **[RATIONALE.md](RATIONALE.md)** — why it is shaped this way
- **[conformance/](conformance/)** — byte vectors every implementation must pass
- **[harness/](harness/)** — **built a device? point this at it.** `uv run vtp1-harness`
  connects over Bluetooth and tells you where your firmware departs from the spec

---

## Status

> **`v0.9` release candidate.** The wire format is stable enough to build
> against and is not yet frozen.
>
> The compatibility guarantees in SPEC.md §11 take effect at `v1.0`. One thing
> stands between here and there, and it is the row marked **not yet done**
> below.

| | |
| --- | --- |
| Specification | Believed internally consistent; §4.1 fixes the profile |
| UUID allocation | Frozen |
| Conformance corpus | 125 vectors across 9 record types, and 35 producer cases |
| Reference decoders | C and Python, both passing every vector |
| Reference encoders | C and Python, both passing every producer case |
| Software peripheral | A synthetic device, verified against the reference decoder |
| Device harness | `harness/` checks a device on Windows, macOS or Linux; verified in-process, not yet over a radio |
| **Real-radio smoke test** | **Not yet run.** `reference/peripheral/smoketest.py` exists; no one has pointed it at hardware. |
| Reference **firmware** | Not written. VTP/1 is unproven on a microcontroller. |
| Independent implementations | None yet |

Everything in this repository is tested without a Bluetooth adapter, which
covers the protocol thoroughly and covers the radio not at all. Until
`smoketest.py` has been run between two machines, nothing here has been over
the air, and that is the gap worth knowing about before you build to it.

The last row is the honest measure of a protocol's maturity. A specification
with no second implementer is a file format with extra steps — but for a
hobbyist protocol that is a reason to publish and find out, not a reason to
wait.

## What problem it solves

A DIY logger has three things on it: a GNSS receiver, a CAN transceiver, and an
IMU. Getting all three into an app currently means two unrelated protocols, two
transports, and two clocks that have no defined relationship — so you cannot say
where the car was when the driver lifted, only roughly.

VTP/1 carries all three over one BLE link, timestamped against one monotonic
device clock, so cross-channel alignment is arithmetic instead of guesswork.

Three design commitments follow from one rule — *no receiver may ever produce a
plausible wrong value*:

- **Absence is a validity bit, never a magic value.** No field has a reserved
  bit pattern meaning "no data", so there is no check you can forget.
- **Unrecognised stays unrecognised.** An unknown enum value, bitmask bit or
  extension type is reported as unknown, never coerced to a default.
- **Malformed is rejected whole.** A receiver never decodes the prefix of a
  short payload.

## Two roles, and the words for them

VTP/1 has exactly two ends, and the whole repository is organised around which
one you are building. SPEC.md §1.2 defines them; in short:

| | The **device** | The **client** |
| --- | --- | --- |
| What it is | The logger. A hand-built box with a GNSS receiver, a CAN transceiver and an IMU in it | The app. Phone or laptop software that connects to the logger |
| Bluetooth role | **Peripheral** — it advertises and waits to be connected to | **Central** — it scans, connects and subscribes |
| What it does | Produces the streams and answers questions about itself | Connects, subscribes, decodes and records |
| Mostly | An **encoder** | A **decoder** |
| You are building one if | you are writing firmware, or wiring up a dev board | you are writing an app that reads a logger |

Both ends decode something, which is why the specification also says
**receiver**: whichever end is decoding the payload under discussion. That is
the client on the GPS, CAN and IMU streams — and the device on Monitor (§13),
the one characteristic where the direction reverses and the app sends values
for the logger's screen to show.

## Which of these is for you

Five things here you might reach for, and which one depends on which end you
are building.

| You have | Reach for | Which answers |
| --- | --- | --- |
| **A device** — firmware, or a prototype on a dev board | **[harness/](harness/)**, `uv run vtp1-harness` | Does my device behave correctly, on a real link, as a whole? |
| **A client**, and no logger to test it against | [reference/peripheral/](reference/peripheral/) | nothing — it *is* a device, synthetic, so a client can be built before any firmware exists |
| **A decoder** you wrote | `conformance/run.py` | Does my decoder read every payload correctly, including the ones it must refuse? |
| **An encoder** you wrote, and can drive from a command line | `conformance/produce.py` | Does my encoder refuse to produce what the specification forbids? |
| Nothing yet | [reference/c/](reference/c/), [reference/python/](reference/python/) | nothing — they are working implementations to read, and to check your own output against |

**The corpus and the harness are not alternatives, and neither replaces the
reference implementations.**

The reference decoders and encoders are *implementations*: known-good code you
read, or run your own bytes through. They test nothing by themselves.

The corpus tests one component of yours, offline. It hands your decoder bytes
and checks what it does with them, or asks your encoder for a payload it must
refuse to produce. That covers every requirement expressed as a byte layout, a
validity rule, an enum value or a length check — which is most of this
specification, and all of it that a file of test vectors can reach.

What a file of test vectors cannot do is ask a device a question. Everything
that exists only as *behaviour* is out of its reach: what a device **answers**
(§9), what its sequence numbers and its clock do over time (§8), what state
survives a reconnect (§9.2), what it does with a write it is required to refuse
(§13.4), and what a device that implements only some roles still owes a client
(§4.1). Those need something connected to the running device, which is the
harness. SPEC.md §12.1 is the specification making this point itself.

Two practical consequences for anybody building firmware:

- `conformance/produce.py` tests your encoder only if you can build it as a
  command-line program. Firmware usually cannot be driven that way, and that is
  exactly the gap the harness fills — it tests the device you actually shipped,
  through the radio, without needing to link against anything inside it.
- The harness decodes every payload it sees with `reference/python/vtp1.py`. It
  is not a second opinion about the byte layout; it is the same opinion, applied
  to a live device.

## Quickstart

```sh
# The Python reference, the generator and every check read schema/vtp1.yaml,
# so a YAML parser is needed before any of them will run.
pip install -r requirements.txt

# Build the C reference and run the conformance suite against it
make -C reference/c
python3 conformance/run.py --impl "reference/c/vtp1_cli"

# Same corpus, independent Python implementation
python3 conformance/run.py --impl "python3 reference/python/vtp1.py"

# The other direction: what an encoder must refuse
python3 conformance/produce.py --impl "reference/c/vtp1_producer"
python3 conformance/produce.py --impl "python3 reference/python/vtp1_produce.py"

# Regenerate every derived artefact from the schema
python3 tools/generate.py

# Or run everything CI runs, in one command
tools/ci.sh            # --quick skips the mutation sweep

# Run a synthetic VTP/1 device you can point a real client at
python3 reference/peripheral/selftest.py      # verify it; needs no Bluetooth
# then see reference/peripheral/README.md to run it over a real adapter

# Check YOUR device against the specification, over Bluetooth.
# Scans, reads Info, works out which roles you declared, tests exactly those.
uv run vtp1-harness                           # Windows, macOS or Linux
uv run vtp1-harness --loopback                # see a report without any hardware
uv run vtp1-harness --markdown report.md      # something to paste into an issue
```

## Layout

```
SPEC.md                  Normative specification (tables generated)
RATIONALE.md             Design reasoning and trade-offs
schema/vtp1.yaml         SOURCE OF TRUTH — everything else derives from this
schema/uuids.json        Frozen UUID allocation
tools/generate.py        schema -> spec tables, C header, conformance vectors
conformance/run.py       Implementation-agnostic vector runner
conformance/vectors/     Generated byte vectors with expected decodes
reference/c/             C99 decoder, no dependencies
reference/python/        Schema-driven decoder and encoder
reference/peripheral/    Synthetic device: GATT service over a host adapter
harness/                 Connects to YOUR device over Bluetooth and tests its
                         BEHAVIOUR: the control plane, the shared clock, loss and
                         sequence, reconnect state — everything no vector reaches
```

Everything derived is generated. `tools/generate.py --check` fails CI if any
generated artefact has drifted from the schema, so the spec tables, the vectors
and the reference header cannot disagree.

## Implementing a device

1. Read [SPEC.md](SPEC.md).
2. Use the UUIDs in `schema/uuids.json` unchanged.
3. Decode your own output with a reference decoder before testing against an
   app — the fastest way to find a byte-layout mistake is to have a known-good
   decoder refuse it.
4. **Point [the harness](harness/) at the device as soon as it advertises**, and
   keep pointing it there as you add roles:

   ```sh
   uv run vtp1-harness
   ```

   It scans, reads Info, derives your roles from the capability bits and tests
   exactly those, so a half-built device is never failed for a role it has not
   claimed yet. It also sends deliberately malformed requests, which is the only
   direct test of the rule the whole specification rests on: that your device
   rejects malformed input *whole* rather than decoding a prefix of it.

   Every finding names the section it came from, and the report ends with what
   it could **not** verify — a clean run is evidence about your device, not a
   certificate.
5. Run `conformance/run.py` against your decoder, and `conformance/produce.py`
   against your encoder, if either can be built as a command-line program.

If you ship a device, open an issue — the implementations list is the only
measure of this specification that matters.

## Implementing a client

A client is a decoder with a Bluetooth stack in front of it, so most of what
you have to get right is reachable by the conformance corpus — which is why
there is no harness pointed the other way.

1. Read [SPEC.md](SPEC.md). §1.1 is the rule the rest follows from, and it lands
   almost entirely on your side: **no receiver may ever produce a plausible
   wrong value**, and you are the receiver for the three streams.
2. Scan for the service UUID in `schema/uuids.json`. §3.2 lets you recognise a
   VTP device whose major version you do not implement, and you SHOULD tell the
   user that rather than let the device look absent.
3. **Run the corpus against your decoder** — this is the client's equivalent of
   pointing the harness at a device, and it is mechanical:

   ```sh
   python3 conformance/run.py --impl "./my-decoder"
   ```

   The runner is language-agnostic: anything that reads `record<TAB>hex` on
   stdin and writes one JSON object per line can be tested, including an
   implementation this repository has never seen. `conformance/README.md` has
   the contract, and `--roles` narrows it to what you implement.

   If you implement Monitor you are also an *encoder* — the client produces
   those payloads — so run `conformance/produce.py` too.
4. **Develop against [reference/peripheral/](reference/peripheral/).** It is a
   real GATT peripheral over your laptop's own Bluetooth adapter, driving a
   synthetic car around a circular circuit, so your app has GPS, CAN and IMU to
   read before any firmware exists. `python3 reference/peripheral/selftest.py`
   verifies it without a radio.
5. Get the handful of rules the corpus cannot reach right, because they are
   about what your client *does* rather than what it decodes:
   - **Re-read Info on every connection** and never cache it across one (§4). A
     DIY device is reflashed by its owner: its capabilities and rate ceilings
     change while its address does not.
   - **Enable indications on Control before your first write** (§9.6). A write
     that precedes enablement is a request whose answer has nowhere to go.
   - **One control request outstanding at a time** (§9). Write, wait for the
     indication, then write the next. A device answers `busy` if you pipeline,
     and `busy` says nothing about the request itself — wait, then retry.
   - **Reprogram your subscriptions on every connection** (§9.2). They do not
     survive a disconnect, by design, so you always find a known state.
   - **A `seq` wrap from 65535 to 0 is not a gap** (§8.2), and `dropped` reading
     65535 means *at least* that many, never exactly (§8.3).
   - **Never substitute a default** for a field whose validity bit is clear, an
     enum value you do not recognise, or a capacity Info did not give you.
   - If you implement Monitor: read the declaration before writing, carry every
     slot in every write, clear the `present` bit rather than sending a
     placeholder, and refresh before `max_age` expires (§13).

Nothing here tests those five for you. A client is checked by the corpus and by
pointing it at the software peripheral; the behavioural half is on you, and it
is the half worth re-reading §9.6 and §13.4 about.

## Versioning

Major versions have separate service UUIDs, so a client scans for what it can
speak and an unsupported major is a discovery outcome rather than a parsing one.
Minor versions are additive by construction: record sizes are frozen, new fields
go in length-prefixed extension records, and reserved bits absorb small
additions. SPEC.md §11 has the rules and the prohibitions.

## Licence

- **Specification text** (`SPEC.md`, `RATIONALE.md`, `schema/`) —
  [CC BY 4.0](LICENSE-SPEC)
- **Code** (`tools/`, `conformance/`, `reference/`) —
  [Apache License 2.0](LICENSE)

Apache-2.0 rather than MIT deliberately: it carries an express patent grant, and
a protocol specification without one is a specification a commercial vendor's
legal review stops at before an engineer ever reads it.

> **Unresolved, and known.** That reasoning argues for a patent grant on the
> *specification*, and the specification is the half that does not have one.
> CC BY 4.0 grants copyright permissions and
> [expressly withholds patent rights](https://creativecommons.org/licenses/by/4.0/legalcode).
> An implementer builds a device from SPEC.md and `schema/`, so the patent
> exposure sits exactly where the licence is silent, while `reference/` — which
> nobody has to ship — carries Apache-2.0 §3.
>
> `schema/` compounds it: listed here as specification text under CC BY, it is
> also the source `tools/generate.py` turns into `reference/c/vtp1_generated.h`
> and the conformance corpus, both of which live under Apache-2.0. Generated
> artefacts crossing a licence boundary is a question in its own right.
>
> The options, none of which should be chosen without advice: apply Apache-2.0
> to the whole repository; keep CC BY for prose and move `schema/` under
> Apache-2.0; or add a separate patent non-assertion covenant covering
> implementations of the specification, which is what several standards bodies
> do. **This needs a lawyer, not an opinion** — it is recorded here so that
> nobody mistakes the current split for a settled decision.

## Prior art

VTP/1 exists because of [RaceChrono's DIY BLE
API](https://github.com/aollin/racechrono-ble-diy-device), which established
that a published, vendor-neutral contract for hand-built loggers is worth having
and has served that ecosystem for the better part of a decade. Several of its
decisions are carried over here unchanged — equations rather than DBC for CAN
decoding, the device as a dumb pipe, per-id rate limiting in the device.
[RATIONALE.md](RATIONALE.md) covers what changed and why.
