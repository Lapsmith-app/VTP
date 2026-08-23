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
open -n "$PWD/VTPPeripheral.app" --args "$PWD/serve.py"

# headless — REQUIRED when measuring throughput or loss
open -n "$PWD/VTPPeripheral.app" --args "$PWD/serve.py" --no-display
```

**`open -n` is mandatory.** Plain `open` activates an already-running copy and
*silently discards your arguments* — you'll believe you launched headless (or
with a new `--encrypt` mode) when nothing changed. If in doubt, stop the old
instance first.

Other modes (`--encrypt all|control|none`, custom CAN channel CSV): see the
README. Default is encrypt-everything-except-Info.

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
