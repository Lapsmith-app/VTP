"""Getting past macOS's Bluetooth permission, without asking the user to.

macOS kills any process that touches CoreBluetooth without an
`NSBluetoothAlwaysUsageDescription` in its Info.plist. It is not a permission
that can be granted after the fact: the process dies with SIGABRT before it can
ask, so it never appears in System Settings > Privacy & Security > Bluetooth and
that pane has no button to add one. A plain `python -m vtp1_harness` therefore
exits 134 with no output at all, which is the worst possible first experience of
a diagnostic tool.

The fix is the same one `reference/peripheral/make_macos_app.sh` uses for the
peripheral role, and it has the same three requirements:

  1. A NON-FRAMEWORK interpreter. Framework builds identify as
     `org.python.python` and ignore the wrapper's Info.plist entirely, so the
     bundle makes no difference. uv's standalone interpreters adopt the bundle
     they are placed in; Homebrew's and Apple's do not.
  2. A bundle the interpreter can find its own runtime from.
  3. A launch through LaunchServices with `open`. A binary exec'd directly reads
     the Mach-O's embedded __info_plist section rather than the file on disk, so
     a correctly built bundle still dies if you run Contents/MacOS/... by hand.

What is different here is that a harness is a command-line tool, and `open`
normally throws away its output. `open --stdout /dev/stdout --stderr /dev/stderr`
does not, so the relaunch is invisible: one command in, the report out.
"""
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import sysconfig

BUNDLE_NAME = "VTP1Harness.app"
BUNDLE_ID = "app.lapsmith.vtp.harness"
#: Set in the relaunched process so it does not try to relaunch itself again.
MARKER = "VTP1_HARNESS_BUNDLED"

_PROBE = (
    "import asyncio, sys\n"
    "from bleak import BleakScanner\n"
    "async def m():\n"
    "    await BleakScanner.discover(timeout=0.1)\n"
    "try:\n"
    "    asyncio.run(m())\n"
    "except Exception as exc:\n"
    "    print(exc, file=sys.stderr); sys.exit(3)\n"
)


def is_macos():
    return platform.system() == "Darwin"


def already_bundled():
    return os.environ.get(MARKER) == "1"


def cache_dir():
    return pathlib.Path.home() / "Library" / "Caches" / "vtp1-harness"


def bundle_path():
    return cache_dir() / BUNDLE_NAME


def probe():
    """Can this process reach CoreBluetooth at all?

    Answered in a child process, because the way it fails is by killing the
    process that asked. Returns (ok, reason).
    """
    try:
        done = subprocess.run([sys.executable, "-c", _PROBE],
                              capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return True, f"probe inconclusive ({exc}); continuing"
    if done.returncode == 0:
        return True, "CoreBluetooth is reachable"
    if done.returncode == -6:
        return False, "tcc"
    return True, (done.stderr or b"").decode("utf-8", "replace").strip()


def framework_build():
    """True if this interpreter is a framework build, which cannot be bundled."""
    return bool(sysconfig.get_config_var("PYTHONFRAMEWORK"))


def build_bundle(force=False):
    """Assemble (or reuse) the app bundle. Returns its path.

    Rebuilding is not free: it re-signs, macOS treats a re-signed bundle as a
    different app, and the Bluetooth permission has to be granted again.
    """
    app = bundle_path()
    if app.is_dir() and not force:
        return app

    interpreter = pathlib.Path(sys.executable).resolve()
    root = interpreter.parent.parent
    app_macos = app / "Contents" / "MacOS"
    if app.exists():
        shutil.rmtree(app)
    app_macos.mkdir(parents=True)

    shutil.copy2(interpreter, app_macos / "VTP1Harness")
    # A uv-style interpreter loads libpython from @executable_path/../lib, which
    # inside a bundle resolves to Contents/lib.
    libs = list((root / "lib").glob("libpython*.dylib"))
    if libs:
        (app / "Contents" / "lib").mkdir(exist_ok=True)
        for lib in libs:
            shutil.copy2(lib, app / "Contents" / "lib" / lib.name)

    # The same mechanism a virtualenv uses to point an interpreter at a stdlib
    # somewhere else. It goes in Contents/ rather than Contents/MacOS/, because
    # codesign refuses to sign a bundle with a stray file beside the executable.
    (app / "Contents" / "pyvenv.cfg").write_text(
        f"home = {root / 'bin'}\ninclude-system-site-packages = false\n")

    (app / "Contents" / "Info.plist").write_text(f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>VTP/1 Harness</string>
  <key>CFBundleDisplayName</key><string>VTP/1 Harness</string>
  <key>CFBundleIdentifier</key><string>{BUNDLE_ID}</string>
  <key>CFBundleExecutable</key><string>VTP1Harness</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <!-- Deliberately a plain foreground app: an app that cannot put a window on
       screen cannot show the Bluetooth permission prompt either, and macOS
       kills it instead of asking, reporting the identical "no usage
       description" error as a bundle with no key at all. -->
  <key>NSBluetoothAlwaysUsageDescription</key>
  <string>Connects to a VTP/1 telemetry device to check it against the \
specification.</string>
</dict></plist>
""")

    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)],
                   check=True, capture_output=True)
    _write_launcher()
    return app


def _write_launcher():
    """A shim the bundled interpreter runs, carrying this process's import path.

    The bundle holds an interpreter and nothing else -- no copy of the harness,
    no copy of bleak. Passing sys.path across means there is exactly one
    installation of everything, and the bundle never goes stale against it.
    """
    directory = cache_dir()
    (directory / "syspath.json").write_text(
        json.dumps([p for p in sys.path if p]))
    (directory / "launch.py").write_text(f"""\
import json, os, pathlib, sys
here = pathlib.Path(__file__).resolve().parent
sys.path[:0] = json.load(open(here / "syspath.json"))
os.environ["{MARKER}"] = "1"
from vtp1_harness.__main__ import cli
code = cli(sys.argv[1:])
(here / "status").write_text(str(code))
sys.exit(code)
""")


def relaunch(argv):
    """Run the harness inside the bundle, with its output still on this
    terminal. Returns (exit code, error), one of which is always None."""
    app = build_bundle()
    _write_launcher()
    status = cache_dir() / "status"
    if status.exists():
        status.unlink()
    done = subprocess.run(
        ["open", "-W", "--stdout", "/dev/stdout", "--stderr", "/dev/stderr",
         str(app), "--args", str(cache_dir() / "launch.py"), *argv],
        capture_output=True)
    if done.returncode != 0:
        return None, (done.stderr or b"").decode("utf-8", "replace").strip()
    # `open -W` waits for the app but does not report what it exited with, so
    # the launcher writes it down.
    try:
        return int(status.read_text().strip()), None
    except (OSError, ValueError):
        return None, "the bundled run did not report an exit code"


def explain(reason="tcc"):
    """What to tell a user when the bundle route is not available."""
    if reason == "framework":
        return (
            "This is a framework build of Python. macOS gives it the bundle\n"
            "identity org.python.python, which means an app bundle around it is\n"
            "ignored and Bluetooth stays blocked. Nothing this tool does can\n"
            "work around that.\n\n"
            "Use a standalone interpreter instead:\n\n"
            "    uv run --python 3.12 vtp1-harness\n\n"
            "uv's interpreters are not framework builds and adopt the bundle\n"
            "they are placed in.")
    return (
        "macOS blocks Bluetooth for a process with no app bundle, and kills it\n"
        "rather than prompting. The harness builds a signed bundle in\n"
        f"{cache_dir()} and relaunches itself through it, but that launch\n"
        "failed here.\n\n"
        "Run it from Terminal.app or iTerm rather than from an editor or an\n"
        "automation shell — LaunchServices needs a real login session. The\n"
        "first run will ask for Bluetooth permission; after you allow it, the\n"
        "harness appears in System Settings > Privacy & Security > Bluetooth.\n\n"
        "To skip the bundle entirely, pass --no-bundle. On a machine where\n"
        "Bluetooth is already granted to the terminal this works fine; where it\n"
        "is not, the process exits 134 with no output, which is macOS killing\n"
        "it and not a fault in the device you are testing.")
