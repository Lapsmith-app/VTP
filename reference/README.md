# Reference implementations

Two decoders, deliberately built differently so that agreement between them
means something:

| | Language | Approach |
| --- | --- | --- |
| `c/` | C99, no dependencies | Compiles the generated offset constants in. Decoder (`vtp1.c`) and encoder (`vtp1_encode.c`) are separate translation units, so a client links the decoder alone and a device links the encoder alone. |
| `python/` | Python 3, PyYAML | Reads `schema/vtp1.yaml` at runtime and derives every offset. Suitable for tooling and log analysis. |

Because one hardcodes what the other derives, both passing the same corpus also
proves that `schema/vtp1.yaml` and the generated C header agree. A transliterated
second implementation would prove much less.

```sh
make -C c && python3 ../conformance/run.py --impl "reference/c/vtp1_cli"
python3 ../conformance/run.py --impl "python3 reference/python/vtp1.py"
```

## The encoder enforces, it does not trust

`vtp_encode_*` writes zero for any field whose validity bit is clear, whatever
the caller left in the struct (SPEC.md §5.1). Firmware that computes a stale
altitude and then clears the bit cannot leak the stale value onto the wire.

That rule is covered by the `stale-values-behind-cleared-bits` vector, which is
deliberately non-canonical: the round-trip requires the encoder to normalise it
rather than reproduce it. Without that case the gating had no test at all —
every other vector is already canonical, so removing the gate changed nothing.

## Not yet written

- **Firmware.** There is no buildable reference device yet. Until there is,
  VTP/1 is specified but unproven on hardware — see the status note in the
  README.
- **A Python encoder.** The Python side decodes only, so it exercises the
  `absent` contract but not the round-trip.
- **Dart.** LapSmith carries a decoder that passes this corpus, but it lives in
  that application rather than here.
