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

echo "==> installing dependencies into the bundle"
"$APP/Contents/MacOS/VTPPeripheral" -m pip install -q --break-system-packages bless pyyaml

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
