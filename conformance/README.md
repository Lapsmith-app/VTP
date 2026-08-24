# Conformance suite

Every VTP/1 implementation MUST pass the vectors for the roles it declares.
This is what makes the compatibility guarantee in SPEC.md §11 mechanical rather
than aspirational.

## Running it

```sh
# C reference
make -C reference/c && python3 conformance/run.py --impl "reference/c/vtp1_cli"

# Python reference
python3 conformance/run.py --impl "python3 reference/python/vtp1.py"

# Your implementation
python3 conformance/run.py --impl "./my-decoder"
```

`--verbose` lists each case as it passes and notes reject-reason differences.

### Declaring roles

SPEC.md §12 asks an implementation to pass the vectors **for the roles it
declares**. A decoder that implements GPS and nothing else says so:

```sh
python3 conformance/run.py --impl "./my-decoder" --roles gps
```

| Role | Records |
| --- | --- |
| `core` | `info` — always included, never optional (§4) |
| `control` | `control_response`, `time_sync` |
| `gps` | `gps_fix` |
| `can` | `can_batch` — implies `control` |
| `imu` | `imu_batch` |
| `monitor` | `monitor_list`, `monitor_update` — implies `control` |
| `power` | `power_state` — implies `control` |
| `gnss_aiding` | `gnss_aid_caps`, `aid_begin_result`, `aid_commit_result` — implies `gps`, `control` |
| `obd` | `obd_info` — implies `can`, `control` |

Only Info is unconditional. The Control characteristic is a capability
(`capabilities` bit 4 — §4.1 has the matrix), not a requirement, so a GPS-only
device is not asked for control responses; `core` used to carry them, which
left such a device failing conformance for records it had never claimed.

The implications come from §4.1's matrix, which makes them normative: a CAN
device is reached through `CAN_SUBSCRIBE`, a Monitor device declares its
channels through `MONITOR_LIST`, a device's supply is read with `GET_POWER`,
and an aiding transfer is opened and closed on Control — so none of the four
is usable without the Control characteristic. This table and that matrix are
the same rule stated once each; the runner used to imply what the
specification did not say.

Omit `--roles` to run everything, which is what a full implementation should
do. The summary line names the scope either way, because a green run over one
role must not be mistaken for conformance across all of them.

## The producer direction

Everything above tests **decoding**. The corpus says nothing about what an
encoder must refuse, because round-tripping a payload that already decoded only
ever asks whether an encoder reproduces something valid.

`conformance/encoders.json` carries the other direction: structured inputs,
each marked `must_refuse`. They cannot be byte vectors, because the point is
that the bytes are never produced.

```sh
# C reference
make -C reference/c && python3 conformance/produce.py --impl "reference/c/vtp1_producer"

# Python reference
python3 conformance/produce.py --impl "python3 reference/python/vtp1_produce.py"

# Your implementation
python3 conformance/produce.py --impl "./my-encoder" --roles gps,can
```

It takes `--roles` and `--verbose` exactly as `run.py` does, and reads the same
role table.

This runner replaced `tools/check_encoders.py`, which imported the Python
encoder as a module. That made a green producer run a statement about one of
this repository's two reference encoders: the C encoder had four
malformed-input crashes and a violation of its own "nothing written on failure"
contract, and every producer run stayed green throughout, because none of them
was ever called. **A producer suite that can only test the language it is
written in is not a conformance suite**, so this one drives a subprocess over a
text contract like the decode runner does.

### The producer contract

**stdin** — one case per line:

```
<record>\t<json-object>
```

`record` names the record being produced. The JSON object is the case's
`input`: the structured values a caller would hand the encoder, with binary
fields carried as hex strings (`payload`, `ext_hex`, `detail_hex`).

**stdout** — exactly one JSON object per input line, in the same order:

```jsonc
{"ok": true,  "hex": "0100..."}                  // the encoder produced these
{"ok": false, "reason": "id outside the field"}  // the encoder refused
```

`reason` is informational and is never compared, exactly as decoder reject
reasons are not.

**A crash is not a refusal.** An implementation that dies partway through has
answered nothing for the case it died on, and the runner reports every
unanswered case as a failure rather than assuming a refusal. That distinction
is the whole reason this file specifies an adapter process rather than a
library call: an encoder that falls over is not an encoder that declined, and
an in-process harness catching a broad exception cannot tell the two apart.

A case marked `must_refuse: false` may also carry `expect_hex`, and the encoder
must produce exactly those bytes. `expect_hex` is generated from the schema's
own field offsets rather than from either reference encoder, so two
implementations that agree on it are agreeing with the source of truth rather
than with each other.

### What the producer suite cannot reach

Every array a JSON case describes exists, so a language whose API takes a count
and a pointer has two promises no case here can test: that a count with no
array behind it is refused rather than dereferenced, and that a refused call
leaves the caller's buffer untouched. Both were broken in the C reference and
invisible to every check in CI.

Those are API contracts rather than protocol ones, and they are tested where
they live: `reference/c/encode_selftest.c`, built with ASan and UBSan
(`make -C reference/c san`). An implementation in a memory-safe language has
nothing to answer here; one in C does.

## The runner contract

The runner is deliberately language-agnostic. Any implementation that speaks
this contract can be tested, including one this repository has never seen.

**stdin** — one case per line:

```
<record>\t<hex>
```

`record` is one of `gps_fix`, `can_batch`, `imu_batch`, `info`. `hex` is the
raw notification or characteristic value, lowercase hex, no separators.

**stdout** — exactly one JSON object per input line, in the same order:

```jsonc
{"ok": true, "seq": 1, "lat": 515074000, ...}                  // gps_fix, info
{"ok": true, "header": {...}, "records": [...]}                // can_batch
{"ok": true, "header": {...}, "samples": [...]}                // imu_batch
{"ok": false, "reason": "length"}                              // rejected
```

Field names match `schema/vtp1.yaml`. Extra fields are permitted and ignored —
the runner only checks the keys a vector actually asserts, so an implementation
may report more than the vector does.

Nothing else may go to stdout. Diagnostics belong on stderr.

### Two optional keys, each worth reporting

Both are optional, and both check something a plain decode cannot. The runner
says at the end of a run whether it exercised them, so a partial implementation
is visible rather than silently unmeasured.

**`absent`** — an array of `gps_fix` field names the implementation reports as
having no value. This is how the protocol's central rule becomes testable:
absence is the validity bitmask's job, never a field value, and without this key
an implementation could return zero for a gated field and the corpus would
never notice. An implementation that carries raw values and leaves gating to
its caller should report what such a caller is required to treat as absent.

**`roundtrip_hex`** — the payload re-encoded from the decoded values. Required
to match byte for byte, which catches two things a decode alone cannot: that
the encoder and decoder agree about the layout, and that the encoder emits the
*canonical* form rather than merely one that decodes back.

For a vector marked `"canonical": false`, the target is `expect_roundtrip_hex`
instead — a conforming encoder must **normalise** the payload, zeroing the
stale bytes sitting behind cleared validity bits. That is the only coverage the
encoder's gating rule gets.

## Reading a vector

```jsonc
{
  "name": "position-without-accuracy",
  "desc": "Position valid, accuracy fields absent. A decoder MUST NOT grade ...",
  "record": "gps_fix",
  "hex": "0300...",
  "expect":        { "lat": 515074000, "h_acc": 0, ... },  // raw wire values
  "expect_scaled": { "lat": 51.5074, ... }                 // after `scale`
}
```

A `gps_fix` case also carries `expect_absent` — the exact set of fields a
conforming decoder must report as having no value — and `fix_type_known`, which
is asserted on every case so that neither coercing an unknown enum to a
plausible default nor reporting everything as unknown can pass.

A case carries either `expect` or `must_reject`. **A `must_reject` case MUST
fail to decode** — an implementation that decodes it has not passed, because
the failure mode this protocol exists to prevent is a confident wrong answer,
not a crash.

The `must_reject` value is a suggested reason code. Reason strings are
informational and are not compared, since SPEC.md does not define them.

`expect` holds raw wire integers. `expect_scaled` holds the same values after
applying the field's `scale` from the schema, for implementations that surface
engineering units. The runner checks `expect`; checking `expect_scaled` is
recommended for any implementation that does scaling, because a scale error is
exactly the kind of plausible wrong number this corpus exists to catch.

## Changing the corpus

Vectors are generated: `python3 tools/generate.py`. Never hand-edit a file
under `vectors/`.

Within major version 1 a minor release **MAY add cases** and **MUST NOT modify
or remove an existing one**. A change that alters the expected decode of an
existing vector is not a minor release — it is a breaking change, and CI is
what says so rather than a reviewer's memory.
