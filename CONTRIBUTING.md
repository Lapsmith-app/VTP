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

Within major version 1, the prohibitions in SPEC.md §11.3 are absolute:

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

## Style

Specification text is normative and terse; RFC 2119 keywords in capitals, and
only where a requirement is genuinely being stated. Reasoning goes in
RATIONALE.md, not in SPEC.md.

## Licence

Contributions are accepted under the repository's licences: CC BY 4.0 for
specification text, Apache License 2.0 for code.
