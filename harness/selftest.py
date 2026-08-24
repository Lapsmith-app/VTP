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

from vtp1_harness.checks import Status, load_all           # noqa: E402
from vtp1_harness.runner import Runner                     # noqa: E402
from vtp1_harness.transport import FAULTS, LoopbackTransport  # noqa: E402

#: Each fault, and the check -- or checks -- that must catch it. Every entry in
#: transport.FAULTS has to appear here; the run asserts that too, so a fault
#: added without a check to catch it fails this rather than sitting unused.
#:
#: A tuple names more than one check, and EVERY one of them must fail in that
#: run. One mistake can violate two rules honestly: a device that sets the phy
#: validity bit knowing only half of it breaks §9.1's validity grouping AND
#: reports a PHY §2.2 asks it not to be on, and both are true statements about
#: that device rather than one finding counted twice.
CAUGHT_BY = {
    "missing_characteristic": "gatt.attribute_table",
    "extra_characteristic": "gatt.no_extra_characteristics",
    "inert_cccd_rejected": "gatt.inert_cccd",
    "implication_broken": "info.capability_implications",
    "opcode_capability_late": "control.opcode_capability",
    "aid_stale_held_until": "aiding.declaration",
    "aid_chunk_exceeds_mtu": "aiding.chunk_size",
    "aid_accepts_undeclared_format": "aiding.rejects_undeclared_format",
    "aid_accepts_oversized": "aiding.rejects_oversized",
    "aid_applied_with_missing_index": "aiding.transfer",
    "aid_reports_first_chunk_missing": "aiding.reports_missing_chunk",
    "aid_ignores_crc": "aiding.detects_corruption",
    "aid_begin_keeps_transfer": "aiding.begin_supersedes",
    "aid_token_reused": "aiding.begin_supersedes",
    "aid_token_ignored": "aiding.begin_supersedes",
    "rate_not_applied": "control.rate_readback",
    "seq_starts_at_one": "seq.first_is_zero",
    "seq_repeats": "seq.advances",
    "detail_on_error": "control.detail_only_on_ok",
    "no_tag_echo": "control.echoes_request",
    "timesync_single_reading": "control.time_sync",
    "monitor_accepts_partial": "monitor.rejects_incomplete",
    "monitor_accepts_duplicate_slot": "monitor.rejects_duplicate_slot",
    "subs_survive_reconnect": "reconnect.subscriptions_cleared",
    "unknown_subscription_ok": "can.unknown_subscription",
    "duplicate_consumes_slot": "can.table_full",
    "duplicate_double_entry": "can.subscribe_idempotent",
    "table_full_early": "can.table_full",
    "overlap_wrong_governor": "can.most_specific_governs",
    "tie_break_latest": "can.earliest_installed_governs",
    "can_duplicate_across_batches": "can.forwarded_once",
    "stream_before_subscribe": "can.silent_until_asked",
    "caps_reserved_bits": "info.reserved_capabilities",
    "absent_field_nonzero": "gps.absent_fields_zero",
    "clock_per_stream": "clock.one_clock",
    "drops_a_response": "control.time_sync",
    "pipelines_silently": "control.busy_when_outstanding",
    "busy_but_applied": "control.busy_not_applied",
    "list_reserved_nonzero": "monitor.declaration",
    "timesync_unsupported": "control.time_sync",
    "monitor_paged_declaration": "monitor.declaration",
    "monitor_accepts_bad_length": "monitor.rejects_bad_length",
    "monitor_rejects_unknown_slot": "monitor.ignores_unknown_slot",
    "params_ignored": "control.malformed_params",
    "unallocated_opcode_ok": "control.unsupported_opcode",
    "info_truncated": "info.decodes",
    "info_major_wrong": "info.major",
    "capacity_zero": "info.capacities",
    "advert_no_service_uuid": "adv.service_uuid",
    "advert_caps_disagree": "adv.service_data_agrees",
    "clock_steps_backwards": "clock.monotonic",
    "stream_truncated": "gps.decodes",
    "seq_survives_reconnect": "reconnect.seq_restarts",
    "rate_ceiling_ignored": "control.rate_ceiling",
    "info_rate_above_ceiling": "info.rate_ceiling",
    "inert_control_accepts_writes": "gatt.inert_control_rejects_writes",
    "power_unsupported": "power.get_power",
    "power_percent_impossible": "power.percent_in_range",
    "power_stale_behind_bit": "power.absent_fields_zero",
    "power_declared_but_empty": "power.something_valid",
    "power_reserved_nonzero": "power.reserved",
    "obd_probe_unsupported": "obd.probe",
    "obd_responded_without_ecus": "obd.count_agrees",
    "obd_entries_descending": "obd.entries_ascending",
    "obd_stale_behind_bit": "obd.absent_fields_zero",
    "obd_reserved_nonzero": "obd.reserved",
    "obd_capacity_zero": "obd.capacities",
    "obd_accepts_unsupported_pid": "obd.poll_refusals",
    "obd_ignores_stop": "obd.poll_and_flag",
    "obd_delivery_needs_subscription": "obd.poll_and_flag",
    "obd_flag_never_set": "obd.poll_and_flag",
}

#: A MUST or SHOULD check with no seeded fault against it, and why there is
#: none. Every check must appear either here or as a value in CAUGHT_BY, and
#: the run asserts both directions -- see `_coverage_problems`.
#:
#: This table is the point of the exercise. CAUGHT_BY says which claims are
#: tested; without its complement, a check that no device can fail is
#: indistinguishable from one that no device has failed yet, and it passes
#: silently forever. Every entry is a debt with a reason attached, not a
#: dispensation: shortening this list is how this harness gets better.
NOT_SEEDED = {
    "gatt.service":
        "the loopback transport IS the service; a run against a device without "
        "one cannot reach the checks that follow it",
    "gatt.info":
        "same -- the profile is built from the schema, so the characteristic "
        "cannot be absent without `missing_characteristic`, which is seeded "
        "against gatt.attribute_table",
    "dis.present":
        "the Device Information Service is the host stack's, not the "
        "peripheral's, and the loopback supplies it unconditionally",
    "adv.service_data":
        "advert_no_service_uuid covers the advertisement being wrong; an "
        "absent Service Data block is a SHOULD whose only injection is "
        "'return None', which tests the transport rather than the check",
    "can.subscribe_ok":
        "a subscribe that is refused is a device that cannot subscribe, "
        "which fails every CAN check at once rather than this one",
    "can.matches_subscription":
        "the harness subscribes with a mask that matches everything, so a "
        "device forwarding too much cannot be told from one obeying it",
    "control.applies_only_if_answerable":
        "§9.4 is about a request applied with the indication disabled; the "
        "loopback returns before dispatch, so seeding it means removing the "
        "gate rather than breaking the device",
    "monitor.accepts_complete_write":
        "a device refusing a complete write is a device with no Monitor role",
    "monitor.absent_is_a_state":
        "same shape -- refusing a cleared present bit is refusing the write",
    "gps.reserved_bits":
        "the reference decoder rejects a reserved bit before this check sees "
        "the record, so the fault lands on gps.decodes",
    "can.header_reserved":
        "same -- rejected at decode",
    "imu.reserved_bits":
        "same -- rejected at decode",
    "can.decodes":
        "stream_truncated covers the decode path; a second copy against a "
        "different characteristic tests the fault, not the check",
    "imu.decodes":
        "same",
    "link.att_mtu":
        "the loopback names its own MTU, so a run below the minimum is a test "
        "of the transport's constructor",
}

#: Faults that break the conversation rather than one rule, and the reason.
#:
#: A device whose responses cannot be correlated fails every check that waits
#: for one, so its run says nothing about whether those checks work -- they did
#: not run, they drowned. Such a fault is exempt from the stale-excuse guard
#: below, because "this check failed while the envelope was broken" is not
#: evidence that the check can be made to fail on its own terms, and treating
#: it as evidence is exactly the accident-of-ordering coverage NOT_SEEDED's
#: entries are trying to avoid claiming.
CASCADING = {
    "no_tag_echo":
        "no response can be correlated, so every check awaiting one fails "
        "whatever its own rule says",
}

#: Only these faults are about state surviving a link drop, and only their runs
#: need to pay for the reconnection.
NEEDS_RECONNECT = {"subs_survive_reconnect", "seq_survives_reconnect"}

#: SPEC.md §4.1 -- some rules only exist on a device that does NOT implement a
#: role: a CCCD write on an inert stream, an opcode whose owning capability is
#: clear. They cannot be tested against a device that declares everything, so
#: these faults are seeded into one that does not.
PARTIAL = "gps+imu+control"
NEEDS_PARTIAL = {"inert_cccd_rejected", "opcode_capability_late"}

#: Faults whose rule needs a profile of their own. §4.1's inert-Control rule
#: only exists on a device that has not declared Control at all, which PARTIAL
#: does, so it gets a profile that does not.
FAULT_PROFILE = {"inert_control_accepts_writes": "gps"}


def profile_for(fault):
    if fault in FAULT_PROFILE:
        return FAULT_PROFILE[fault]
    return PARTIAL if fault in NEEDS_PARTIAL else None

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


def checks_for(fault):
    """The check ids a fault claims, whether it named one or several."""
    named = CAUGHT_BY[fault]
    return (named,) if isinstance(named, str) else tuple(named)


def _all_named():
    out = set()
    for fault in CAUGHT_BY:
        out.update(checks_for(fault))
    return out


def _coverage_problems():
    """Both directions of the claim, and the second is the one that was missing.

    Forwards: every fault has a check named against it. That has always been
    asserted, and it is the weaker half -- it holds the FAULTS table to
    account, and the FAULTS table is written by whoever wrote the checks.

    Backwards: every MUST and SHOULD has either a fault that makes it fail or a
    stated reason why it has none. Without it, `transport.FAULTS` decides what
    "detects every defect it claims" means, and a check can sit in the registry
    for a year having never once been observed to fail. Forty-one of them were,
    including the one covering §13.3's declaration format.
    """
    problems = []
    checks = {c.id: c for c in load_all()}

    missing = sorted(set(FAULTS) - set(CAUGHT_BY))
    if missing:
        problems.append(
            f"fault(s) {missing} are defined but no check is named as catching "
            f"them. Either add the check or remove the fault -- an untested "
            f"fault is a claim nobody is holding to account")
    unknown = sorted(set(CAUGHT_BY) - set(FAULTS))
    if unknown:
        problems.append(f"unknown fault(s) named here: {unknown}")
    stray = sorted(set(CASCADING) - set(FAULTS))
    if stray:
        problems.append(f"CASCADING names fault(s) that do not exist: {stray}")

    named = sorted(_all_named() - set(checks))
    if named:
        problems.append(f"CAUGHT_BY names check(s) that do not exist: {named}")
    excused = sorted(set(NOT_SEEDED) - set(checks))
    if excused:
        problems.append(f"NOT_SEEDED names check(s) that do not exist: {excused}")

    both = sorted(set(NOT_SEEDED) & _all_named())
    if both:
        problems.append(
            f"check(s) {both} are excused in NOT_SEEDED and also have a seeded "
            f"fault. Delete the excuse -- an excuse that outlives the reason "
            f"for it is how this list stops meaning anything")

    seeded = _all_named()
    uncovered = sorted(c.id for c in checks.values()
                       if c.severity in ("MUST", "SHOULD")
                       and c.id not in seeded and c.id not in NOT_SEEDED)
    if uncovered:
        problems.append(
            f"{len(uncovered)} check(s) claim a MUST or SHOULD with no seeded "
            f"fault and no stated reason: {uncovered}. Add a fault to "
            f"transport.FAULTS that makes each one fail, or say in NOT_SEEDED "
            f"why none is possible. A check nothing can break is a check that "
            f"does not work")

    blank = sorted(k for k, v in NOT_SEEDED.items() if not str(v).strip())
    if blank:
        problems.append(f"NOT_SEEDED entries with no reason given: {blank}")
    return problems


#: Which checks a fully-capable conforming device is EXPECTED not to reach, and
#: why. A skip is the harness saying nothing at all, and nothing was watching
#: which ones it said nothing about: a check that quietly starts skipping for
#: every device -- a renamed state key, a capability probe that stopped
#: matching, a refusal newly read as "not applicable" -- looks exactly like a
#: passing run. This is the baseline that makes that visible.
EXPECTED_SKIPS = {
    "control.opcode_capability":
        "this profile owns every opcode in this version",
    "gatt.inert_cccd":
        "this profile declares every capability that has a CCCD",
    "gatt.inert_control_rejects_writes":
        "this profile declares Control",
    "can.matches_subscription":
        "the harness subscribes with a mask that matches every frame",
}


def _skip_problems(report):
    """A skip that nobody predicted is a check that stopped running.

    The first three of these are properties of the profile and the last three of
    the loopback link; all six are things the partial-profile runs below DO
    reach. Anything else skipping on a device that declares every role means the
    check could not do its job, and saying so out loud is the difference between
    a tool that reports what it verified and one that reports what it attempted.
    """
    problems = []
    skipped = {r.check.id: r.message for r in report.results
               if r.status is Status.SKIP}
    unexpected = sorted(set(skipped) - set(EXPECTED_SKIPS))
    if unexpected:
        problems.append(
            "on a device declaring every role, check(s) "
            + ", ".join(f"{i} ({skipped[i]})" for i in unexpected)
            + " were skipped. A skip is not a pass: either the check cannot "
              "reach what it tests any more, or it belongs in EXPECTED_SKIPS "
              "with a reason")
    gone = sorted(set(EXPECTED_SKIPS) - set(skipped))
    if gone:
        problems.append(
            f"check(s) {gone} are listed as expected skips but ran. Remove "
            f"them -- an expected-skip list that is not true of the clean run "
            f"stops being read")
    return problems


async def main():
    problems = _coverage_problems()

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
    problems += _skip_problems(clean)

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
            profile=profile_for(fault))
        for fault in ordered))

    width = max(len(f) for f in ordered)
    for fault, report in zip(ordered, reports):
        caught, missed = [], []
        for check_id in checks_for(fault):
            result = result_for(report, check_id)
            if result is None:
                missed.append(f"{check_id} did not run")
            elif result.status in (Status.FAIL, Status.WARN):
                caught.append(check_id)
            else:
                missed.append(f"{check_id} reported {result.status.value}")
        ok = not missed
        outcome = (f"caught by {', '.join(caught)}" if ok
                   else "; ".join(missed))
        print(f"  {'ok ' if ok else 'MISS'}  {fault:<{width}}  {outcome}")
        if not ok:
            problems.append(
                f"{fault}: {FAULTS[fault]} was not caught -- {outcome}")
        # An excuse is a claim about the whole fault suite, not about any one
        # run, so it is only testable here: if a check NOT_SEEDED says cannot
        # be made to fail is failing, the claim is already false. Four of them
        # were on the commit that introduced the list, each broken by a fault
        # aimed at a different check -- which the disjointness check above
        # cannot see, because that only compares two tables against each other.
        if fault in CASCADING:
            continue
        broke_an_excuse = sorted(
            r.check.id for r in report.failures + report.warnings
            if r.check.id in NOT_SEEDED)
        if broke_an_excuse:
            problems.append(
                f"{fault} made {broke_an_excuse} fail, and NOT_SEEDED says "
                f"each of those cannot be made to fail. Name it in CAUGHT_BY "
                f"if the failure is a true statement about that device, list "
                f"the fault in CASCADING if it broke the conversation rather "
                f"than that rule, or correct the reason")

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
