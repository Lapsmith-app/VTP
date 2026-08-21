# Reference implementations

Two decoders, deliberately built differently so that agreement between them
means something:

| | Language | Approach |
| --- | --- | --- |
| `c/` | C99, no dependencies | Compiles the generated offset constants in. Decoder (`vtp1.c`) and encoder (`vtp1_encode.c`) are separate translation units, so a client links the decoder alone and a device links the encoder alone. |
| `peripheral/` | Python 3, bless | A synthetic device presenting the service over a host Bluetooth adapter, split so the device model has no Bluetooth dependency and is verified by the reference decoder in CI. See `peripheral/README.md`. |
| `python/` | Python 3, PyYAML | Reads `schema/vtp1.yaml` at runtime and derives every offset. Decoder in `vtp1.py`, encoder in `vtp1_encode.py`, split for the same reason. Suitable for tooling, log analysis and driving a software peripheral. |

Because one hardcodes what the other derives, both passing the same corpus also
proves that `schema/vtp1.yaml` and the generated C header agree. A transliterated
second implementation would prove much less.

```sh
make -C c && python3 ../conformance/run.py --impl "reference/c/vtp1_cli"
python3 ../conformance/run.py --impl "python3 reference/python/vtp1.py"
```

Both report `roundtrip_hex`, so both are checked as encoders as well as
decoders — 35 of the 43 vectors are verified byte-identical through each. The
two encoders were also compared directly against each other over the whole
corpus and agree on every byte, which matters because one derives its offsets
from the schema at runtime and the other compiles them in.

## The encoder enforces, it does not trust

Both encoders write zero for any field whose validity bit is clear, whatever
the caller left in the struct or dict (SPEC.md §5.1), and zero an IMU triple
whose presence flag is clear (SPEC.md §7). Firmware that computes a stale
altitude and then clears the bit cannot leak the stale value onto the wire.

One field is deliberately *not* forced to zero: `can_header.reserved` is
written through. A device built against a later minor may have been assigned
those bytes, and an encoder must not silently erase a field it does not
understand.

That rule is covered by the `stale-values-behind-cleared-bits` vector, which is
deliberately non-canonical: the round-trip requires the encoder to normalise it
rather than reproduce it. Without that case the gating had no test at all —
every other vector is already canonical, so removing the gate changed nothing.

## Not yet written

- **Firmware.** There is no buildable reference device yet. Until there is,
  VTP/1 is specified but unproven on hardware — see the status note in the
  README.
- **Firmware, still.** `peripheral/` is a *software* device: it proves the GATT
  contract is implementable and lets a client be built, but a host operating
  system's scheduler is not an MCU's, so it says nothing about SPEC.md §8's
  clock discipline, §6.1's timing bounds or the transport rules of §2.1-§2.3.
- **Dart.** LapSmith carries a decoder that passes this corpus, but it lives in
  that application rather than here.
