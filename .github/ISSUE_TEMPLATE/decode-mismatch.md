---
name: Decode disagreement
about: A payload decodes differently than you expect, or two implementations disagree
labels: decode
---

**Record type:** <!-- gps_fix | can_batch | imu_batch | info -->

**Payload (hex):**
```
<paste the exact bytes — this is the part that makes the report actionable>
```

**What you expected:**

**What you got:**

**Reference decoders say:**
```
$ printf '<record>\t<hex>\n' | python3 reference/python/vtp1.py
```

**Implementation:** <!-- your decoder, the C reference, the Python reference -->
**Device firmware / protocol minor, if known:**
