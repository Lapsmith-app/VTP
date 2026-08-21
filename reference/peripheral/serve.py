#!/usr/bin/env python3
"""Present the synthetic device as a real BLE peripheral.

The transport shell, and deliberately thin: everything worth testing lives in
vtp_device.py, which has no Bluetooth dependency and is checked by selftest.py
on machines with no adapter. This file is the part that can only be verified by
connecting a client to it.

    pip install bless
    python3 reference/peripheral/serve.py

Platform notes, because they change what this can demonstrate:

  macOS   Works. But CoreBluetooth's peripheral role accepts only a local name
          and service UUIDs in an advertisement, so the three-byte Service Data
          of SPEC.md §3.3 CANNOT be advertised from a Mac. A client will find
          the device and must read Info to learn its capabilities — which is
          what §3.3 requires anyway, Service Data being advisory. It is the one
          part of the specification a Mac cannot exercise.

  Linux   BlueZ can advertise arbitrary Service Data, so §3.3 is reachable
          there. `btmgmt` can also set the connection parameters and PHY that
          §2.1–§2.3 ask for, which no desktop API exposes to an application.

Nothing here can validate SPEC.md §8's clock discipline or the timing bounds of
§6.1: a host operating system's scheduler is not an MCU's. This makes a client
developable. It does not make the protocol proven on hardware.
"""
import argparse
import asyncio
import json
import logging
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

import vtp_device as dev  # noqa: E402

try:
    from bless import (BlessServer, GATTCharacteristicProperties,
                       GATTAttributePermissions)
except ImportError:
    sys.exit("bless is required: pip install bless")

UUIDS = json.loads((ROOT / "schema" / "uuids.json").read_text())
SERVICE = UUIDS["service"]["vtp1"]
CHAR = UUIDS["characteristics"]

log = logging.getLogger("vtp.peripheral")

# Launched through LaunchServices there is no terminal to write to, so the log
# goes somewhere findable instead of nowhere.
LOG_FILE = "/tmp/vtp-peripheral.log"


def _in_app_bundle():
    return "/Contents/MacOS/" in sys.executable


class Peripheral:
    def __init__(self, device, name="VTP Logger"):
        self.device = device
        self.name = name
        self.server = None
        self._notify = {"gps": CHAR["gps"], "can": CHAR["can"],
                        "imu": CHAR["imu"]}

    # -- GATT callbacks ---------------------------------------------------

    def read_request(self, characteristic, **kwargs):
        """Info is regenerated per read: SPEC.md §4 forbids a client caching it
        across connections precisely because it can change."""
        if characteristic.uuid.lower() == CHAR["info"].lower():
            return self.device.info()
        return characteristic.value or b""

    def write_request(self, characteristic, value, **kwargs):
        if characteristic.uuid.lower() != CHAR["control"].lower():
            return
        response = self.device.handle_control(bytes(value))
        if response is None:
            # Too short to carry a tag, so there is nothing to correlate a
            # reply with. SPEC.md §9 requires a response to every *request*;
            # two bytes are the minimum that constitutes one.
            log.warning("control write of %d byte(s) is not a request",
                        len(value))
            return
        control = self.server.get_characteristic(CHAR["control"])
        control.value = response
        self.server.update_value(SERVICE, CHAR["control"])

    # -- lifecycle --------------------------------------------------------

    async def start(self):
        # macOS TERMINATES any process that creates a CBPeripheralManager
        # without an NSBluetoothAlwaysUsageDescription in its Info.plist --
        # killed outright, no exception to catch, nothing on stderr. It is not
        # a permission that can be granted after the fact: the process dies
        # before it can ask, so it never appears in System Settings, and that
        # pane has no way to add one by hand. Run it from the bundle that
        # make_macos_app.sh builds, via `open`, and note that exec'ing the
        # bundle's binary directly fails identically -- a directly-launched
        # Mach-O uses its embedded __info_plist section, not the file.
        if sys.platform == "darwin" and not _in_app_bundle():
            log.error("not running from an app bundle: macOS will kill this "
                      "process when it creates the peripheral manager")
            log.error("build one with reference/peripheral/make_macos_app.sh, "
                      "then: open -a VTPPeripheral.app --args serve.py")
        self.server = BlessServer(name=self.name)
        self.server.read_request_func = self.read_request
        self.server.write_request_func = self.write_request

        await self.server.add_new_service(SERVICE)

        read = GATTCharacteristicProperties.read
        notify = GATTCharacteristicProperties.notify
        write = GATTCharacteristicProperties.write
        indicate = GATTCharacteristicProperties.indicate
        readable = GATTAttributePermissions.readable
        writeable = GATTAttributePermissions.writeable

        # CoreBluetooth: "Characteristics with cached values must be
        # read-only". Only Info may carry an initial value; anything
        # notifiable or writable must be created with None, or addService_
        # raises NSInternalInconsistencyException.
        await self.server.add_new_characteristic(
            SERVICE, CHAR["info"], read, self.device.info(), readable)
        for name in ("gps", "can", "imu"):
            await self.server.add_new_characteristic(
                SERVICE, CHAR[name], notify, None, readable)
        await self.server.add_new_characteristic(
            SERVICE, CHAR["control"], write | indicate, None,
            readable | writeable)

        await self.server.start()
        log.info("advertising %s as %r", SERVICE, self.name)
        log.info("Service Data (SPEC.md 3.3) is not advertised: the host "
                 "peripheral API does not expose it on every platform")

    async def run(self, poll_hz=200):
        interval = 1.0 / poll_hz
        sent = 0
        while True:
            for characteristic, payload in self.device.poll():
                char = self.server.get_characteristic(self._notify[characteristic])
                char.value = payload
                self.server.update_value(SERVICE, self._notify[characteristic])
                sent += 1
                if sent % 200 == 0:
                    log.info("%d notifications sent", sent)
            await asyncio.sleep(interval)

    async def stop(self):
        if self.server:
            await self.server.stop()


async def main_async(args):
    handlers = [logging.StreamHandler()]
    if not sys.stdout.isatty():
        handlers.append(logging.FileHandler(LOG_FILE, mode="w"))
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers)
    if not sys.stdout.isatty():
        log.info("logging to %s", LOG_FILE)

    device = dev.VtpDevice(mtu=args.mtu, gps_hz=args.gps_hz,
                           imu_hz=args.imu_hz)
    peripheral = Peripheral(device, name=args.name)
    # Launched through LaunchServices there is no stderr, so an unhandled
    # exception would vanish and look exactly like a silent exit. Everything
    # goes to the log file instead.
    try:
        await peripheral.start()
        await peripheral.run()
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("the peripheral stopped with an error")
        raise
    finally:
        try:
            await peripheral.stop()
        except Exception:
            log.exception("error while stopping")
        log.info("stopped")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--name", default="VTP Logger",
                    help="advertised local name")
    ap.add_argument("--mtu", type=int, default=247,
                    help="assumed ATT MTU for batch sizing")
    ap.add_argument("--gps-hz", type=int, default=10)
    ap.add_argument("--imu-hz", type=int, default=100)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass
    except Exception:
        logging.getLogger("vtp.peripheral").exception("fatal")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
