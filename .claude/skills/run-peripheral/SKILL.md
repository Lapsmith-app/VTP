---
name: run-peripheral
description: Start, stop, and monitor the synthetic VTP/1 BLE peripheral (reference/peripheral) with the debug panel or headless. Use when asked to run, start, stop, restart, or check on "the peripheral", or to watch its logs.
---

# Run the software peripheral

The synthetic VTP/1 device in `reference/peripheral/`. It advertises as
**`VTP`** on service `56545001-5f05-5b56-af87-dcab2baf2522`. Full detail,
including the encryption matrix and the smoketest, is in
[reference/peripheral/README.md](../../../reference/peripheral/README.md) —
this skill is the operational subset, verified on macOS.

## Start (macOS)

macOS only allows advertising from an app bundle. The bundle is **gitignored**
(machine-specific signature), so on a fresh clone build it once:

```sh
cd reference/peripheral
./make_macos_app.sh        # once per machine — see rebuild warning below
```

Then, from `reference/peripheral/`:

```sh
# with the debug panel (the device's display) — costs ~30% of
# notification throughput
open -n "$PWD/VTPPeripheral.app" --args "$PWD/serve.py" --encrypt none

# headless — REQUIRED when measuring throughput or loss
open -n "$PWD/VTPPeripheral.app" --args "$PWD/serve.py" --no-display --encrypt none
```

**`open -n` is mandatory.** Plain `open` activates an already-running copy and
*silently discards your arguments* — you'll believe you launched headless (or
with a new `--encrypt` mode) when nothing changed. If in doubt, stop the old
instance first.

**`--encrypt none` is deliberate here**, and differs from `serve.py`'s own
default of `all` (everything except Info). It drops the only demand for an
encrypted link this peripheral makes, which leaves one thing that can fail to
produce a key on a Mac hosting for an iPhone on the same iCloud account instead
of two. It does **not** fix the fault that costs you the link there — that
expectation is the central's, not our GATT permissions', and connecting fails
under `--encrypt none` too. See [When the client can see `VTP` but cannot
connect](#when-the-client-can-see-vtp-but-cannot-connect) for what does fix it.

So keep the flag while you are working on something else, and drop it to
exercise the real postures (`--encrypt all|control|none`) — which is the only
way to test a client against them, and is work to do on a Linux host.

The synthetic bus runs at 80 frames/s by default, which is gentle — a real
500 kbit/s bus carries thousands. `--can-scale FACTOR` multiplies every channel
and `--can-rate ID=HZ` sets one outright, and the startup log reports what the
pump will actually carry rather than what was asked for. Raise `--mtu` to 515
alongside it: LapSmith negotiates 515 but batches are capped at the `--mtu`
ceiling, so the default 247 leaves half the payload unused. The full option
list is `serve.py --help`.

### Linux

No bundle needed: `pip install -r requirements.txt && python3 serve.py`
(same `--no-display` flag applies).

## Verify it's up

There is **no stdout under `open`** — the log file is the only view:

```sh
pgrep -fl VTPPeripheral
tail -f /tmp/vtp-peripheral.log
```

Healthy startup logs the advertisement; a connecting client shows MTU
negotiation and `CTRL ... -> ok` lines. Caveat when reading the log:
"CLIENT CONNECTED/DISCONNECTED" really means *subscribed/unsubscribed* —
CoreBluetooth gives a peripheral no true connect/disconnect callback, so a
client that unsubscribes from everything while staying connected logs as a
disconnect.

## When the client can see `VTP` but cannot connect

The symptom: the client lists the device, connecting does nothing, and
`/tmp/vtp-peripheral.log` shows the advertisement and then **nothing at all** —
no `READ`, no `ATT MTU`, no `CLIENT CONNECTED`.

That silence is not proof the client never arrived. CoreBluetooth gives a
peripheral no connect callback and the MTU line fires only on subscribe, so a
connection and a complete service discovery leave no trace in this log. Check
the Mac's stack instead — note `/usr/bin/log`, because zsh's `log` builtin
shadows the tool and fails with "too many arguments":

```sh
/usr/bin/log show --last 5m \
  --predicate 'subsystem == "com.apple.bluetooth" AND category == "Stack.SMP"' \
  --style compact | grep -c "Failed to encrypt"
```

Non-zero means the fault is the Mac's and not the peripheral's. macOS expects an
encrypted link for an iCloud cloud-paired (`SameAccount`) iPhone on *any*
incoming LE connection — including one to this GATT server, which rides on the
Mac's single LE identity — then fails to produce the key, logs `Failed to
encrypt connection STATUS 761` with `isPairing=0`, and disables encryption
rather than re-pairing. The link stays up and unusable.

Deleting the MacBook from the iPhone's Bluetooth list clears it, but iCloud
re-pairs within seconds, so it returns. There is no `VTP` entry on the iPhone to
forget — the peripheral has no identity of its own. No `serve.py` flag affects
any of this: it fails under `--encrypt none` too, so the flag above narrows
what can go wrong without fixing this. The durable fix is a host with no Apple-ecosystem
relationship to the client — the Linux path above needs no bundle, no signing
and no permission prompt, and `bluetoothctl` gives deterministic bond control.

## Stop

```sh
pkill -f VTPPeripheral
```

or close the panel window. Stopping prints a final stats block (sent/refused
counts, delivery stats) to the log — worth reading before dismissing a run.

## Do NOT rebuild the bundle to pick up code changes

`serve.py`, `vtp_device.py` and `display.py` are read from the repository at
run time — editing them needs only a restart, never a rebuild. Rebuilding
re-signs the bundle, macOS treats it as a different app, and the Bluetooth
permission must be granted again by a human at the machine. Rebuild only if
the bundle is missing or the interpreter inside it is broken.

## No Bluetooth available?

The device logic is testable without an adapter:

```sh
python3 reference/peripheral/selftest.py            # device vs reference decoder
python3 reference/peripheral/transport_selftest.py  # pump vs fake GATT link
python3 reference/peripheral/display.py             # the panel alone, fake data
```
