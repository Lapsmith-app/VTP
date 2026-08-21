# Contributing

## The most useful contribution

**Implement it and report what went wrong.** A specification with one
implementer is a file format. If you built a device or a client and something in
SPEC.md was ambiguous, unimplementable or simply annoying, that is the highest
value issue you can open — more so than a patch.

## Reporting a decode disagreement

Include the **hex of the exact payload**. Prose descriptions of wire bugs are
not actionable; sixty bytes of hex are. Run it through both reference decoders
first:

```sh
printf 'gps_fix\t<your hex>\n' | python3 reference/python/vtp1.py
```

If the two references disagree with each other, that is a bug in this repository
and a high-priority one.

## Changing the wire format

1. Edit `schema/vtp1.yaml` — never a generated artefact.
2. Run `python3 tools/generate.py`.
3. Add conformance vectors covering the change, including a must-reject case if
   the change introduces a new way to be malformed.
4. Confirm both reference decoders still pass.
5. Explain in the PR **which constraint the change answers**. RATIONALE.md is
   organised by constraint; a change that does not map onto one is usually a
   change to something that is already correct.

### What will be refused

Within major version 1, the prohibitions in SPEC.md §11.4 are absolute:

- Changing the meaning, units or scale of an existing field.
- Changing the size, offset or type of an existing field.
- Removing or repurposing a field — deprecate by ceasing to set its validity bit.
- Changing the value or meaning of an existing enum member.
- Changing any UUID.
- Modifying or removing an existing conformance vector.

These are not stylistic preferences. Each one silently corrupts data already
recorded by deployed devices, which is unrecoverable in a way that a bug is not.

A proposal that requires one of these is a proposal for VTP/2, and is welcome as
such.

### Adding a field

New fields go in extension records, not on the end of a base record — record
sizes are frozen for the life of a major version. If the field is a single bit
of state, the reserved bits in `gps_fix.validity`, `fix_flags` and
`info.capabilities` exist for exactly that. Appendix A of SPEC.md lists the
reserved space.

## Releases and version numbers

Two version numbers exist here and they move independently. Conflating them is
the most common confusion this repository produces.

**Protocol version — on the wire.** `major` fixes the service UUID family and
`protocol_major`; `minor` is `protocol_minor` in the Info characteristic. Both
live in `schema/vtp1.yaml` under `protocol:`. SPEC.md §11 governs what may
change in each. A client uses `protocol_minor` to know which additive features
a device has.

**Specification version — the git tag.** Semver, tagged `vX.Y.Z`, describing
this document and the code in this repository. It is what an implementer cites
when they say which version they built against.

`VTP/1 at specification version 0.4.0` is a coherent statement: the protocol is
major 1, the document describing it is still draft.

### Before specification 1.0

The wire format may change without notice. Everything lands under
`[Unreleased]` in CHANGELOG.md, and `protocol.minor` stays `0` — additive
changes fold into the eventual 1.0.0 rather than each earning a minor bump.
Tags in the `v0.x` range are baselines, not compatibility promises.

### After specification 1.0

SPEC.md §11 takes effect and the discipline changes. An additive wire change —
a new opcode, a new extension type, a newly assigned reserved bit — requires
**both**:

1. `protocol.minor` incremented in `schema/vtp1.yaml`, then
   `python3 tools/generate.py` re-run (it feeds `VTP_MINOR` in the generated C
   header), and
2. a minor bump of the specification version.

A change that SPEC.md §11.4 prohibits is not a minor version in either sense.
It requires a new service UUID and a new schema — see "What will be refused"
above.

### Cutting a release

1. Confirm CI is green: `python3 tools/generate.py --check`, both reference
   decoders passing the corpus.
2. Move the `[Unreleased]` entries in CHANGELOG.md under a `## [X.Y.Z] - DATE`
   heading and leave a fresh empty `[Unreleased]`.
3. Update the status table in README.md if anything in it has changed —
   particularly the conformance corpus count and the implementations row.
4. Tag `vX.Y.Z` and push the tag.

There is no release automation. If that changes, document it here.

## Style

Specification text is normative and terse; RFC 2119 keywords in capitals, and
only where a requirement is genuinely being stated. Reasoning goes in
RATIONALE.md, not in SPEC.md.

## Licence

Contributions are accepted under the repository's licences: CC BY 4.0 for
specification text, Apache License 2.0 for code.
