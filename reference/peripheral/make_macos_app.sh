#!/usr/bin/env bash
# Build a macOS .app that can actually present a BLE peripheral.
#
# macOS kills any process that creates a CBPeripheralManager without an
# NSBluetoothAlwaysUsageDescription in its Info.plist. It is not a permission
# you can grant: the process dies before it can ask, so it never appears in
# System Settings > Privacy & Security > Bluetooth, and that pane has no button
# to add one manually.
#
# Three things have to be true, and the third is the one that wastes an
# afternoon:
#
#   1. A NON-FRAMEWORK Python. Homebrew's and Apple's are framework builds, so
#      the process identifies as org.python.python and the wrapper's Info.plist
#      is ignored entirely. uv installs standalone interpreters, which adopt the
#      bundle they are placed in.
#   2. A self-contained bundle: interpreter, stdlib and bless all inside it.
#   3. Launched through LaunchServices with `open`. A binary exec'd directly
#      uses the Mach-O's EMBEDDED __info_plist section, not the file on disk, so
#      a correctly built bundle still dies with the identical error if you run
#      Contents/MacOS/... by hand.
#
# Usage:
#   ./make_macos_app.sh
#   open "$PWD/VTPPeripheral.app" --args "$PWD/serve.py"
#   tail -f /tmp/vtp-peripheral.log
#
# Note `open <path>`, not `open -a <path>`: the -a form takes an application
# NAME to look up, not a path, and fails with "Unable to find application".
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="$HERE/VTPPeripheral.app"
PYVER="${PYVER:-3.13}"

# Rebuilding is almost never necessary and is not free: the bundle contains
# only the interpreter and its libraries -- serve.py, vtp_device.py and
# display.py are read from the repository at run time, so editing them needs no
# rebuild. Re-signing changes the bundle's code signature, macOS treats it as a
# different app, and the Bluetooth permission has to be granted again. If the
# peripheral hangs after a rebuild with nothing but "logging to" in the log,
# that is a permission prompt waiting for a click.
if [ -d "$APP" ] && [ -z "${FORCE:-}" ]; then
    cat >&2 <<EXISTS
$APP already exists.

Editing serve.py, vtp_device.py or display.py does NOT need a rebuild -- they
are read from the repository at run time. Rebuilding re-signs the bundle, which
makes macOS ask for Bluetooth permission again.

Rebuild anyway with:  FORCE=1 $0
EXISTS
    exit 0
fi

command -v uv >/dev/null || {
    echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
}

echo "==> installing a standalone CPython $PYVER"
uv python install "$PYVER" >/dev/null
PYBIN="$(uv python find "$PYVER")"
PYROOT="$(dirname "$(dirname "$(readlink -f "$PYBIN")")")"

if "$PYBIN" -c 'import sysconfig,sys; sys.exit(0 if not sysconfig.get_config_var("PYTHONFRAMEWORK") else 1)'; then
    echo "    standalone build confirmed"
else
    echo "    ERROR: $PYBIN is a framework build; the bundle identity will be" >&2
    echo "    org.python.python and this cannot work. Use a uv interpreter." >&2
    exit 1
fi

echo "==> assembling $APP"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$(readlink -f "$PYROOT/bin/python$PYVER")" "$APP/Contents/MacOS/VTPPeripheral"
cp -R "$PYROOT/lib" "$APP/Contents/lib"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>VTP Peripheral</string>
  <key>CFBundleDisplayName</key><string>VTP Peripheral</string>
  <key>CFBundleIdentifier</key><string>app.lapsmith.vtp.peripheral</string>
  <key>CFBundleExecutable</key><string>VTPPeripheral</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <!-- Deliberately a plain foreground app. Neither LSBackgroundOnly nor
       LSUIElement: an app that cannot put a window on screen cannot display
       the Bluetooth permission prompt either, and macOS kills it instead of
       asking -- reporting the identical "no usage description" error as a
       bundle with no key at all, which is a thoroughly misleading way to say
       "this app cannot be prompted". It costs a Dock icon while running. -->
  <key>NSBluetoothAlwaysUsageDescription</key>
  <string>Presents a synthetic VTP/1 vehicle telemetry device so a client application can be developed against it.</string>
</dict></plist>
PLIST

# From the pinned files, not from floating names. `pip install bless pyyaml`
# resolved whatever was newest on the day the bundle happened to be built, so
# two people following these instructions a month apart ran different bless
# versions against the same peripheral -- and the bundle is exactly the thing
# nobody rebuilds, so the drift is invisible until the radio misbehaves.
echo "==> installing pinned dependencies into the bundle"
"$APP/Contents/MacOS/VTPPeripheral" -m pip install -q --break-system-packages \
    -r "$HERE/requirements.txt" -r "$HERE/../../requirements.txt"

echo "==> signing"
codesign --force --deep --sign - "$APP"

cat <<DONE

Built $APP

Run it. Note 'open <path>' -- NOT 'open -a', which takes an application name
rather than a path, and NOT the binary directly:

    open "$APP" --args "$HERE/serve.py"
    tail -f /tmp/vtp-peripheral.log

macOS will prompt for Bluetooth the first time. After you allow it, the app
appears in System Settings > Privacy & Security > Bluetooth.

To stop it:  pkill -f VTPPeripheral
DONE
