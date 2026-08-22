"""`vtp1-harness` — point it at your device and it tells you what is wrong.

One command, one device, one report. Everything optional has a default that is
right for the common case: find the VTP device that is advertising, exercise
every role it declares, and print what it did.
"""
import argparse
import asyncio
import platform
import sys

from . import macos, refdec, report as reporting
from .checks import Status
from .runner import Runner
from .transport import FAULTS, BleakTransport, LoopbackTransport, TransportError

EXIT_OK, EXIT_NOT_CONFORMING, EXIT_ERROR = 0, 1, 2


def build_parser():
    p = argparse.ArgumentParser(
        prog="vtp1-harness",
        description="Connect to a VTP/1 device and check it against the "
                    "specification.",
        epilog="A run with no failures is evidence, not a certificate. "
               "SPEC.md §12.1 says which requirements no client-side test can "
               "reach; the report repeats them under 'Not verified'.")
    target = p.add_argument_group("choosing a device")
    target.add_argument("--address", metavar="ADDR",
                        help="connect to this address (macOS: the per-host UUID "
                             "that --scan prints, not a MAC address)")
    target.add_argument("--name", metavar="NAME",
                        help="connect to the first device whose advertised name "
                             "contains this")
    target.add_argument("--scan", action="store_true",
                        help="list what is advertising and exit")
    target.add_argument("--scan-seconds", type=float, default=6.0,
                        metavar="S", help="how long to scan (default: 6)")
    target.add_argument("--any", action="store_true",
                        help="consider devices that do not advertise the VTP/1 "
                             "service UUID, which §3.3 requires")

    run = p.add_argument_group("what to run")
    run.add_argument("--seconds", type=float, default=12.0, metavar="S",
                     help="how long to watch the streams (default: 12)")
    run.add_argument("--can-id", action="append", default=[], metavar="ID",
                     help="a CAN identifier to subscribe to, hex or decimal; "
                          "repeatable. Needed only if the device does not "
                          "support masked subscriptions")
    run.add_argument("--no-adversarial", action="store_true",
                     help="do not send malformed or out-of-range requests")
    run.add_argument("--no-reconnect", action="store_true",
                     help="skip the reconnection tests of §9.2 and §8.2")
    run.add_argument("--use-cached-services", action="store_true",
                     help="let the OS reuse its cached GATT table (Windows). "
                          "Faster, and wrong the moment you reflash")

    out = p.add_argument_group("output")
    out.add_argument("-v", "--verbose", action="store_true",
                     help="show skips, passes and evidence")
    out.add_argument("--json", metavar="PATH", help="write the full result")
    out.add_argument("--markdown", metavar="PATH",
                     help="write a report to paste into an issue")

    mac = p.add_argument_group("macOS")
    mac.add_argument("--no-bundle", action="store_true",
                     help="do not relaunch through an app bundle. macOS blocks "
                          "Bluetooth for a process without one and kills it "
                          "rather than prompting")
    mac.add_argument("--rebuild-bundle", action="store_true",
                     help="rebuild the cached app bundle. Re-signing makes "
                          "macOS ask for Bluetooth permission again")

    dev = p.add_argument_group("testing the harness itself")
    dev.add_argument("--loopback", action="store_true",
                     help="run against the software peripheral in-process, "
                          "with no Bluetooth at all")
    dev.add_argument("--fault", action="append", default=[], metavar="NAME",
                     help="give the loopback device a specific defect; "
                          "--fault list to see them")
    return p


def _parse_can_id(text):
    try:
        return int(text, 0)
    except ValueError:
        raise SystemExit(f"not a CAN identifier: {text!r}")


def _print_faults():
    print("Faults the loopback device can be given:\n")
    for name, description in sorted(FAULTS.items()):
        print(f"  {name:<32} {description}")
    print("\nEach one is a mistake real firmware makes, and each exists because "
          "a check\nin this harness claims to catch it.")


async def _pick(transport, args):
    adverts = await transport.scan(args.scan_seconds)
    if args.address:
        for advert in adverts:
            if advert.address.lower() == args.address.lower():
                return advert
        # A device that is not advertising can still be connectable by address.
        return args.address
    candidates = [a for a in adverts if args.any or a.is_vtp]
    if args.name:
        candidates = [a for a in candidates
                      if a.name and args.name.lower() in a.name.lower()]
    if not candidates:
        _no_device(adverts, args)
        return None
    if len(candidates) > 1:
        print(f"{len(candidates)} VTP/1 devices are advertising. Choose one "
              f"with --address:\n")
        for advert in candidates:
            print(f"  {advert.address}  {advert.name or '(unnamed)':<24} "
                  f"{advert.rssi} dBm")
        return None
    return candidates[0]


def _no_device(adverts, args):
    print("No VTP/1 device is advertising.\n")
    if adverts:
        print(f"{len(adverts)} other device(s) were seen. Run --scan to list "
              f"them, or --any to try one that does not advertise the VTP/1 "
              f"service UUID.\n")
    print(_platform_hint())


def _platform_hint():
    system = platform.system()
    if system == "Darwin":
        return (
            "On macOS:\n"
            "  - the terminal needs Bluetooth permission; macOS prompts once, "
            "and if it\n    was refused, grant it in System Settings > Privacy "
            "& Security > Bluetooth.\n"
            "  - devices have no MAC address here. --scan prints the per-host "
            "UUID to use\n    with --address, and it differs on every Mac.\n"
            "  - macOS caches a device's GATT table and gives no way to clear "
            "it. If you\n    have reflashed and the layout looks stale, turn "
            "Bluetooth off and on again.")
    if system == "Windows":
        return (
            "On Windows:\n"
            "  - Windows 10 build 1709 or later is required.\n"
            "  - if the device requires encryption, pair it in Settings > "
            "Bluetooth first.\n"
            "  - Windows caches a device's GATT table. This harness asks for an "
            "uncached\n    read by default; if the layout still looks stale, "
            "remove the device in\n    Settings and pair it again.")
    return ("On Linux: BlueZ 5.55 or later, and the adapter must be up "
            "(`bluetoothctl power on`).")


def _macos_preflight(args):
    """On macOS, get Bluetooth working before anything else needs it.

    Returns an exit code if this process should stop here -- either because the
    run happened inside the bundle instead, or because it cannot happen at all.
    """
    if not macos.is_macos() or args.no_bundle or macos.already_bundled():
        return None
    if args.rebuild_bundle:
        macos.build_bundle(force=True)
    ok, reason = macos.probe()
    if ok:
        return None
    if reason != "tcc":
        return None
    if macos.framework_build():
        print(macos.explain("framework"))
        return EXIT_ERROR
    print("macOS blocks Bluetooth for a process without an app bundle. "
          "Relaunching\nthrough one; the first run will ask for permission.\n")
    code, error = macos.relaunch(sys.argv[1:])
    if code is None:
        print(macos.explain("tcc"))
        if error:
            print(f"\nLaunchServices said: {error}")
        return EXIT_ERROR
    return code


async def _main(args):
    if args.fault == ["list"] or "list" in args.fault:
        _print_faults()
        return EXIT_OK

    if args.loopback:
        transport = LoopbackTransport(faults=args.fault)
        target = (await transport.scan(0))[0]
    else:
        relaunched = _macos_preflight(args)
        if relaunched is not None:
            return relaunched
        transport = BleakTransport(use_cached_services=args.use_cached_services)
        if args.scan:
            return await _scan_only(transport, args)
        target = await _pick(transport, args)
        if target is None:
            return EXIT_ERROR

    console = reporting.ConsoleReporter(verbose=args.verbose)
    runner = Runner(
        transport,
        adversarial=not args.no_adversarial,
        observe_s=args.seconds,
        can_ids=[_parse_can_id(v) for v in args.can_id],
        reconnect=not args.no_reconnect,
        on_result=console.result,
        on_phase=console.phase,
    )
    try:
        result = await runner.run(target)
    except TransportError as exc:
        print(f"\nThe link failed: {exc}\n")
        print(_platform_hint())
        return EXIT_ERROR

    console.summary(result)
    if args.json:
        reporting.write_json(result, args.json)
        print(f"  wrote {args.json}")
    if args.markdown:
        reporting.write_markdown(result, args.markdown)
        print(f"  wrote {args.markdown}")
    if _looks_like_a_stale_cache(result):
        print("\n" + _platform_hint())

    if result.aborted or result.errors:
        return EXIT_ERROR
    return EXIT_NOT_CONFORMING if result.failures else EXIT_OK


def _looks_like_a_stale_cache(result):
    """A GATT layout that contradicts Info is usually the OS, not the firmware.

    Worth saying out loud: it is the failure a developer hits most often on
    both platforms, it looks exactly like a device fault, and neither operating
    system gives an application a way to detect it.
    """
    return any(r.check.id in ("gatt.characteristics", "gatt.service")
               and r.status is Status.FAIL for r in result.results)


async def _scan_only(transport, args):
    adverts = await transport.scan(args.scan_seconds)
    vtp = [a for a in adverts if a.is_vtp]
    others = [a for a in adverts if not a.is_vtp]
    print(f"\nVTP/1 devices ({len(vtp)}):")
    if not vtp:
        print("  none\n")
    for advert in vtp:
        data = advert.vtp_service_data
        detail = ""
        if data and len(data) == 3:
            roles = [name for name, bit in refdec.CAPABILITIES.items()
                     if data[1] & (1 << bit)]
            detail = f"  minor {data[0]}, advertises {', '.join(roles) or 'no roles'}"
        print(f"  {advert.address}  {advert.name or '(unnamed)':<24} "
              f"{advert.rssi} dBm{detail}")
    if others and args.verbose:
        print(f"\nOther devices ({len(others)}):")
        for advert in others:
            print(f"  {advert.address}  {advert.name or '(unnamed)':<24} "
                  f"{advert.rssi} dBm")
    print()
    return EXIT_OK


def cli(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        print("\ninterrupted")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(cli())
