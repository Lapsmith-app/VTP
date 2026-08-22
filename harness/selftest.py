#!/usr/bin/env python3
"""Prove the harness detects what it claims to, on a machine with no Bluetooth.

Two questions, and the second is the one that matters.

  1. Against the reference peripheral, does the harness report a clean run?
     A tool that fails a conforming device is worse than no tool, because the
     first thing a developer does with a red result is start changing firmware.

  2. For every rule this harness claims to check, can it be made to fail?
     `transport.FAULTS` names a specific mistake and this asserts that a device
     making that mistake is caught by a specific check. It is the same argument
     tools/check_corpus.py makes about the byte vectors, applied to the rules
     that live outside them: a check nothing can break is a check that does not
     work, and it passes silently forever.

    python3 harness/selftest.py            # needs no Bluetooth adapter
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from vtp1_harness import refdec                             # noqa: E402
sys.path.insert(0, str(refdec.ROOT / "reference" / "peripheral"))

from vtp1_harness.checks import Status                     # noqa: E402
from vtp1_harness.runner import Runner                     # noqa: E402
from vtp1_harness.transport import FAULTS, LoopbackTransport  # noqa: E402

#: Each fault, and the check that must catch it. Every entry in
#: transport.FAULTS has to appear here; the run asserts that too, so a fault
#: added without a check to catch it fails this rather than sitting unused.
CAUGHT_BY = {
    "missing_characteristic": "gatt.attribute_table",
    "extra_characteristic": "gatt.no_extra_characteristics",
    "inert_cccd_rejected": "gatt.inert_cccd",
    "implication_broken": "info.capability_implications",
    "opcode_capability_late": "control.opcode_capability",
    "rate_not_applied": "control.rate_readback",
    "info_reserved_nonzero": "info.reserved_fields",
    "seq_starts_at_one": "seq.first_is_zero",
    "seq_repeats": "seq.advances",
    "detail_on_error": "control.detail_only_on_ok",
    "no_tag_echo": "control.echoes_request",
    "timesync_single_reading": "control.time_sync",
    "monitor_accepts_partial": "monitor.rejects_incomplete",
    "monitor_accepts_duplicate_slot": "monitor.rejects_duplicate_slot",
    "subs_survive_reconnect": "reconnect.subscriptions_cleared",
    "unknown_handle_ok": "can.unknown_handle",
    "stream_before_subscribe": "can.silent_until_asked",
    "caps_reserved_bits": "info.reserved_capabilities",
    "absent_field_nonzero": "gps.absent_fields_zero",
    "clock_per_stream": "clock.one_clock",
    "drop_fourth_request": "control.four_outstanding",
    "phy_half_reported": "link.validity_groups",
    "list_reserved_nonzero": "can.list_matches_installed",
}

#: Only one fault is about state surviving a link drop, and only that run needs
#: to pay for the reconnection.
NEEDS_RECONNECT = {"subs_survive_reconnect"}

#: SPEC.md §4.1 -- some rules only exist on a device that does NOT implement a
#: role: a CCCD write on an inert stream, an opcode whose owning capability is
#: clear. They cannot be tested against a device that declares everything, so
#: these faults are seeded into one that does not.
PARTIAL = "gps+imu+control"
NEEDS_PARTIAL = {"inert_cccd_rejected", "opcode_capability_late"}

OBSERVE_SECONDS = 1.5


def capabilities(profile):
    """A capability word, by name, using the peripheral's own constants."""
    import vtp_device
    bits = {"gps": vtp_device.CAP_GPS, "can": vtp_device.CAP_CAN,
            "imu": vtp_device.CAP_IMU, "monitor": vtp_device.CAP_MONITOR,
            "control": vtp_device.CAP_CONTROL}
    word = 0
    for name in profile.split("+"):
        word |= bits[name]
    return word


async def run(faults=(), reconnect=False, profile=None):
    device_kwargs = ({} if profile is None
                     else {"capabilities": capabilities(profile)})
    transport = LoopbackTransport(faults=faults, device_kwargs=device_kwargs)
    target = (await transport.scan(0))[0]
    runner = Runner(transport, observe_s=OBSERVE_SECONDS, reconnect=reconnect)
    return await runner.run(target)


def result_for(report, check_id):
    for result in report.results:
        if result.check.id == check_id:
            return result
    return None


async def main():
    problems = []

    missing = sorted(set(FAULTS) - set(CAUGHT_BY))
    if missing:
        problems.append(
            f"fault(s) {missing} are defined but no check is named as catching "
            f"them. Either add the check or remove the fault -- an untested "
            f"fault is a claim nobody is holding to account")
    unknown = sorted(set(CAUGHT_BY) - set(FAULTS))
    if unknown:
        problems.append(f"unknown fault(s) named here: {unknown}")

    print("A conforming device")
    clean = await run(reconnect=True)
    counts = clean.counts
    print(f"  {counts['pass']} passed, {counts['fail']} failed, "
          f"{counts['warn']} warnings, {counts['skip']} skipped, "
          f"{counts['error']} errors")
    for result in clean.failures + clean.warnings + clean.errors:
        problems.append(f"the reference peripheral was reported "
                        f"{result.status.value} on {result.check.id}: "
                        f"{result.message}")
    if clean.aborted:
        problems.append(f"the clean run aborted: {clean.aborted}")
    if counts["pass"] < 30:
        problems.append(f"only {counts['pass']} checks passed against the "
                        f"reference peripheral; something is not running")

    print("\nA device that implements some of the roles")
    # SPEC.md §4.1 -- a device is never failed for a role it never claimed, and
    # the inert half of the profile is only reachable on a device that has one.
    for profile in (PARTIAL, "gps", "gps+control"):
        report = await run(profile=profile)
        counts = report.counts
        print(f"  {profile:<16} {counts['pass']} passed, {counts['fail']} failed, "
              f"{counts['skip']} skipped, {counts['error']} errors")
        for result in report.failures + report.warnings + report.errors:
            problems.append(f"a {profile} device was reported "
                            f"{result.status.value} on {result.check.id}: "
                            f"{result.message}")

    print("\nA device with one specific defect")
    ordered = sorted(CAUGHT_BY)
    reports = await asyncio.gather(*(
        run(faults=[fault], reconnect=fault in NEEDS_RECONNECT,
            profile=PARTIAL if fault in NEEDS_PARTIAL else None)
        for fault in ordered))

    width = max(len(f) for f in ordered)
    for fault, report in zip(ordered, reports):
        check_id = CAUGHT_BY[fault]
        result = result_for(report, check_id)
        if result is None:
            outcome, ok = "the check did not run", False
        elif result.status in (Status.FAIL, Status.WARN):
            outcome, ok = f"caught by {check_id}", True
        else:
            outcome, ok = (f"{check_id} reported {result.status.value}"), False
        print(f"  {'ok ' if ok else 'MISS'}  {fault:<{width}}  {outcome}")
        if not ok:
            problems.append(
                f"{fault}: {FAULTS[fault]} was not caught by {check_id} "
                f"({outcome})")

    print()
    if problems:
        print("FAILED")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"ok: the reference peripheral passes, and all {len(CAUGHT_BY)} "
          f"seeded defects were caught")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
