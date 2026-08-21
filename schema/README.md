# Schema

`vtp1.yaml` is the source of truth for VTP/1. The tables in `SPEC.md`, the
conformance vectors, and `reference/c/vtp1_generated.h` are all generated from
it:

```sh
python3 tools/generate.py          # regenerate
python3 tools/generate.py --check  # fail if anything is stale (CI does this)
```

Never hand-edit a generated artefact. Edit the schema and regenerate.

`uuids.json` is the frozen UUID allocation. It is **not** generated, and its
values MUST NOT change for the life of major version 1 — a shipped device
cannot be recalled.

## Why a schema and not just prose

A prose specification with hand-written tables drifts from its examples, and its
examples drift from its reference code. Each consumer then picks a different
artefact to trust, and the disagreements surface as field reports years later.

Generating everything from one file means the tables, the vectors and the
reference decoder cannot disagree. CI enforces it: `--check` fails the build if
any generated artefact is out of date.
