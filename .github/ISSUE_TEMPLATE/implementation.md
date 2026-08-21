---
name: Register an implementation
about: You built a device or client that speaks VTP/1
labels: implementation
---

Independent implementations are the only real measure of this specification.

**Name and link:**
**Kind:** <!-- device firmware | client application | library -->
**Roles implemented:** <!-- GPS / CAN / IMU / Monitor -->
**Protocol minor:**
**Conformance suite result:**
```
$ python3 conformance/run.py --impl "<your decoder>"
```
<!-- If you implement a device rather than a decoder, say so — there is no
     device-side conformance harness yet, and knowing that it is wanted helps. -->

**Anything in SPEC.md that was ambiguous or annoying to implement:**
