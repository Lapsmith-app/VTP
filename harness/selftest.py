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
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from vtp1_harness import refdec                             # noqa: E402
sys.path.insert(0, str(refdec.ROOT / "reference" / "peripheral"))

from vtp1_harness.checks import Status, load_all           # noqa: E402
from vtp1_harness.checks import control as control_checks  # noqa: E402
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
    "update_refused_when_full": "can.update_in_place_when_full",
    "transmission_bits_in_identity": "can.transmission_bits_ignored",
    "identity_folds_on_mask": "can.identity_is_the_pair",
    "unknown_mode_defaults": "can.unknown_mode_refused",
    "rate_admission": "can.no_rate_admission",
    "no_first_frame": "can.periodic_first_then_rations",
    "periodic_ignored": "can.periodic_first_then_rations",
    "reinstall_rearms": "can.identical_reinstall_costs_nothing",
    "reinstall_never_rearms": "can.changed_reinstall_rearms",
    "schedule_keyed_by_identifier": "can.displaced_schedule_survives",
    "format_bit_ignored": "can.format_bit_is_identity",
    "dropped_counts_declined": "can.dropped_excludes_declined",
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
    "clock_diverges": "clock.one_rate",
    "drops_a_response": ("control.time_sync",
                         "control.no_busy_for_conforming_client"),
    "pipelines_silently": "control.busy_when_outstanding",
    "owes_until_confirmed": ("control.no_busy_for_conforming_client",
                             "control.no_unprovoked_busy"),
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
    "obd_accepts_bad_group": "obd.grouping_refusals",
    "obd_splits_groups": "obd.grouping_is_one_request",
    "obd_ignores_group_minimum": "obd.group_minimum_is_honoured",
    "obd_ignores_stop": "obd.poll_and_flag",
    "obd_reset_keeps_polling": "obd.reset_stops",
    "obd_polls_before_probe": "obd.poll_before_probe",
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
    "info.reserved_fields":
        "Info has no reserved fields in this version (SPEC.md 15 assigned "
        "bytes 20 and 22-23), so no byte exists to seed a fault against; "
        "when a later minor reserves one, the check revives itself and this "
        "excuse must be deleted with a fault added",
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

#: Faults whose narrowing is itself the claim: they must break the checks named
#: against them and NOTHING else.
#:
#: CAUGHT_BY asserts the named checks fail. It says nothing about the rest of
#: the run, so a fault that quietly starts failing half the suite still passes
#: the matrix -- and "caught by X" then means "X was among the fourteen", which
#: is the accident-of-ordering claim this file exists to refuse.
#: `owes_until_confirmed` is narrowed deliberately (see transport.py `_answer`:
#: eligible only on a well-formed TIME_SYNC, spent on the first refusal it
#: causes) because unnarrowed it refuses every conforming client and every
#: request this harness makes is conforming. The narrowing is what makes the
#: entry mean anything, and without this it could rot back into the cascade it
#: was written to avoid with nothing to say so.
ISOLATED = {"owes_until_confirmed"}

#: Faults that are SCENARIO seeds rather than matrix entries: neither breaks
#: a rule one check can catch on its own, so neither belongs in CAUGHT_BY.
#: `obd_pid_never_answers` is a CONFORMING car whose polled PID nothing
#: answers -- SPEC.md §15.4 makes that gap legal, and the claim under test is
#: that obd.poll_and_flag does NOT fail it (a Skip is the required verdict:
#: nothing observable separates a quiet PID from a discarded answer).
#: `obd_reprobe_refused` only means anything stacked on it: the diagnostic
#: re-probe a silent poll is entitled to is refused, which §15.2 makes a
#: failure. The targeted-scenario section of main() asserts both, so these
#: are held to account there rather than by the one-fault-one-check matrix.
#: `answers_before_the_next_write` is a CONFORMING device that answers inside
#: its write handler, so the harness can never get a second request in while
#: one is owed. The claim under test is that the pair of §9 checks built on
#: pipelining report that limit rather than a violation -- an Observe and a
#: Skip, and in particular NOT the Fail that a device fast enough to be
#: unverifiable used to earn (issue #46).
SCENARIO_FAULTS = {"obd_pid_never_answers", "obd_reprobe_refused",
                   "answers_before_the_next_write"}

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
            "control": vtp_device.CAP_CONTROL, "obd": vtp_device.CAP_OBD}
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

    missing = sorted(set(FAULTS) - set(CAUGHT_BY) - SCENARIO_FAULTS)
    if missing:
        problems.append(
            f"fault(s) {missing} are defined but no check is named as catching "
            f"them. Either add the check or remove the fault -- an untested "
            f"fault is a claim nobody is holding to account")
    misfiled = sorted(SCENARIO_FAULTS - set(FAULTS))
    if misfiled:
        problems.append(f"SCENARIO_FAULTS names fault(s) that do not exist: "
                        f"{misfiled}")
    doubly = sorted(SCENARIO_FAULTS & set(CAUGHT_BY))
    if doubly:
        problems.append(
            f"fault(s) {doubly} are both scenario seeds and matrix entries; "
            f"a fault the matrix already holds to account does not need the "
            f"scenario exemption, so delete one classification")
    unknown = sorted(set(CAUGHT_BY) - set(FAULTS))
    if unknown:
        problems.append(f"unknown fault(s) named here: {unknown}")
    stray = sorted(set(CASCADING) - set(FAULTS))
    if stray:
        problems.append(f"CASCADING names fault(s) that do not exist: {stray}")
    stray = sorted(ISOLATED - set(CAUGHT_BY))
    if stray:
        problems.append(
            f"ISOLATED names fault(s) the matrix does not run: {stray}. A "
            f"fault can only be held to breaking nothing BUT its own checks "
            f"if something names what its own checks are")
    both = sorted(ISOLATED & set(CASCADING))
    if both:
        problems.append(
            f"fault(s) {both} are listed both ISOLATED and CASCADING, which "
            f"are opposite claims: one says the fault breaks only its own "
            f"checks, the other that it breaks most of the run")

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


async def _model_device_problems():
    """The loopback's own conformance to SPEC.md §9, driven directly.

    Everything else in this file tests CHECKS against a device that can be
    broken. This tests the unbroken device itself, because a model that cannot
    exhibit a rule cannot be evidence that a check covers it -- and this
    particular rule was got wrong once in exactly that way: refusals were
    delivered on independent timers, so both responses landed together and the
    interval SPEC.md §9 is about never existed here.
    """
    problems = []
    ctrl = refdec.CHAR["control"]
    busy = refdec.STATUS_VALUE["busy"]
    ok = refdec.STATUS_VALUE["ok"]
    sync = refdec.OPCODE["TIME_SYNC"]

    # A conforming client: write, wait for the response to ARRIVE, write again.
    # It must never be refused. SPEC.md §9 anchors owing on the send precisely
    # so this holds -- a device owing until the confirmation would answer busy
    # to a client that waited exactly as long as it was told to.
    t = LoopbackTransport()
    await t.connect()
    seen = []
    await t.subscribe(ctrl, lambda data, ts: seen.append(bytes(data)))
    for tag in (1, 2, 3):
        await t.write(ctrl, bytes([sync, tag]))
        for _ in range(500):                       # wait for arrival, not more
            if len(seen) >= tag:
                break
            await asyncio.sleep(0.001)
    if [r[2] for r in seen] != [ok, ok, ok]:
        problems.append(
            f"a client that waited for each response before writing the next "
            f"was answered {[r[2] for r in seen]}; SPEC.md §9 owes nothing "
            f"once a response has been sent, so none of these may be busy")
    await t.disconnect()

    # A client that pipelines, three deep. The refusal to the second is itself
    # a response and is still unsent when the first goes out, so the third is
    # refused too.
    t = LoopbackTransport()
    await t.connect()
    seen = []
    await t.subscribe(ctrl, lambda data, ts: seen.append(bytes(data)))
    await t.write(ctrl, bytes([sync, 1]))
    await t.write(ctrl, bytes([sync, 2]))
    for _ in range(500):
        if seen:
            break
        await asyncio.sleep(0.001)
    await t.write(ctrl, bytes([sync, 3]))
    await asyncio.sleep(0.5)
    if [r[2] for r in seen] != [ok, busy, busy]:
        problems.append(
            f"three pipelined requests were answered {[r[2] for r in seen]}; "
            f"SPEC.md §9 owes the busy refusal until it too has been sent, so "
            f"the third must be refused as well")
    await t.disconnect()

    # ...and the holding is bounded. SPEC.md §9 asks for one response beyond
    # the one in flight and no more, so a client that keeps writing gets two
    # answers and the rest are discarded -- not an unbounded pile of pending
    # deliveries, which is a model device growing without limit under exactly
    # the abuse a real one is bounded against.
    t = LoopbackTransport()
    await t.connect()
    seen = []
    await t.subscribe(ctrl, lambda data, ts: seen.append(bytes(data)))
    for tag in range(1, 9):
        await t.write(ctrl, bytes([sync, tag]))
    high = t._owed
    await asyncio.sleep(0.5)
    if high > 2 or len(seen) != 2:
        problems.append(
            f"eight back-to-back requests left {high} response(s) owed and "
            f"{len(seen)} answered; SPEC.md §9 bounds a device at the one "
            f"going out and one behind it, and discards past that")
    await t.disconnect()

    # ...and a deferred decrement does not outlive the link it belonged to.
    # `owes_until_confirmed` defers one past the delivery, `connect` zeroes the
    # counters, and a task still sleeping when the link drops would otherwise
    # drive them negative on a connection its response never reached. A
    # negative `_late_pending` reads as truthy, which spends the one-shot fault
    # before the check under test writes anything -- so the fault would stop
    # firing on any run that reconnects, silently, and the matrix would still
    # be green because it only asserts the named checks FAIL.
    t = LoopbackTransport(faults=["owes_until_confirmed"])
    await t.connect()
    await t.subscribe(ctrl, lambda data, ts: None)
    await t.write(ctrl, bytes([sync, 1]))
    await asyncio.sleep(t._control_latency * 1.5)   # sent; decrement deferred
    if t._late_pending != 1:
        problems.append(
            f"the deferred decrement was not outstanding after the response "
            f"had been sent (_late_pending {t._late_pending}); the fault this "
            f"models cannot fire, so the checks named against it are not "
            f"being tested by it")
    await t.disconnect()
    await t.connect()
    await asyncio.sleep(t._control_latency * 3)     # past the deferred wake
    if (t._late_pending, t._owed) != (0, 0):
        problems.append(
            f"a deferred decrement from a dropped link left _late_pending at "
            f"{t._late_pending} and _owed at {t._owed} on the connection "
            f"after it; a task whose lock has been replaced MUST touch "
            f"neither")
    await t.disconnect()
    return problems


async def _diverge_anchor_problems():
    """The diverging-clock seed re-anchors on every connection.

    The fault models a timer that shares the streams' epoch, so its anchor is
    the connection's FIRST batch. An anchor surviving a link drop hands the
    next connection a first batch already offset by the whole previous
    connection's accumulated drift — a defect in the seed itself, which the
    matrix cannot see: it only asserts that the named check fails, and a
    pre-offset first batch makes it fail harder, for the wrong reason. The
    new connection's first batch is deliberately stamped LATER than the old
    anchor, which is the case a reset guard comparing values cannot catch.
    """
    off = refdec.offset("can_header", "t_base")

    def stamped(t_base):
        payload = bytearray(refdec.size("can_header"))
        struct.pack_into("<Q", payload, off, t_base)
        return payload

    problems = []
    t = LoopbackTransport(faults=["clock_diverges"])
    await t.connect()
    t._apply_stream_faults("can", stamped(1_000_000))       # the anchor
    t._apply_stream_faults("can", stamped(9_000_000))       # drift accrues
    await t.disconnect()
    await t.connect()
    fresh = stamped(5_000_000)          # a new clock, above the old anchor
    t._apply_stream_faults("can", fresh)
    got = struct.unpack_from("<Q", fresh, off)[0]
    await t.disconnect()
    if got != 5_000_000:
        problems.append(
            f"clock_diverges carried its anchor across a reconnect: the new "
            f"connection's first batch was rewritten from 5000000 to {got}, "
            f"beginning already offset by the previous connection's drift")
    return problems


async def _prompt_device_problems():
    """A device too quick to pipeline against is not a device in violation.

    `control.busy_when_outstanding` writes a second request meaning to have it
    arrive while the first is owed, and against a device that answers before
    the host can write again it does not manage to -- which the check itself
    calls a device behaving well. The second request is then an ordinary
    conforming one, `ok` is the only correct answer to it, and the device
    installs it.

    `control.busy_not_applied` used to read that subscription back and Fail:
    its premise is that finding the probe installed proves a refusal was a lie,
    and on this path there was no refusal. It is the tool-worse-than-no-tool
    case in this file's header, reachable by any device that answers promptly,
    and the run that reported it had the Observe and the Fail printed one line
    apart contradicting each other.

    Also asserted here: the subscription that install left behind is taken back
    again. Nothing else removes it once `control.busy_not_applied` stops
    probing, and a slot held for the rest of the connection is a slot
    `can.table_full` is counting a few checks later.
    """
    problems = []
    left_installed = []
    transport = LoopbackTransport(faults=["answers_before_the_next_write"])
    target = (await transport.scan(0))[0]

    def on_result(result):
        # Read the moment the pair is done, and not at the end of the phase:
        # the CAN checks after them reset the table, so a slot leaked here is
        # invisible a few checks later however long it was held.
        if result.check.id == "control.busy_not_applied":
            left_installed.append(sorted(
                key for key in transport.device._subscriptions
                if key[0] == control_checks.BUSY_PROBE_ID))

    runner = Runner(transport, observe_s=OBSERVE_SECONDS,
                    reconnect=False, on_result=on_result)
    report = await runner.run(target)

    expected = {"control.busy_when_outstanding": Status.OBSERVE,
                "control.busy_not_applied": Status.SKIP}
    for check_id, want in expected.items():
        result = result_for(report, check_id)
        if result is None:
            problems.append(f"answers_before_the_next_write: {check_id} did "
                            f"not run")
        elif result.status is not want:
            problems.append(
                f"a conforming device that answers before the harness can "
                f"write again was reported {result.status.value} by "
                f"{check_id}: {result.message}. Nothing was ever pipelined "
                f"against it, so the only honest verdict is "
                f"{want.value}")
    if report.aborted:
        # Without this the two results above are enough to report success on a
        # run that never reached the end: they come from the control phase, and
        # everything that would have failed after it never ran.
        problems.append(f"the prompt-device scenario run aborted: "
                        f"{report.aborted}")
    also_broken = sorted(r.check.id for r in
                         report.failures + report.warnings + report.errors)
    if also_broken:
        problems.append(
            f"a conforming device that answers promptly was reported "
            f"fail/warn/error on {also_broken}; answering quickly breaks no "
            f"rule in SPEC.md")
    if not left_installed:
        problems.append("control.busy_not_applied never reported, so nothing "
                        "could be said about what the pair left installed")
    elif left_installed[0]:
        problems.append(
            f"the subscription the pipelined request installed was left "
            f"behind ({[(hex(i), hex(m)) for i, m in left_installed[0]]}); "
            f"nothing removes it once busy_not_applied stops probing, and it "
            f"holds a slot that can.table_full a few checks later is counting")
    return problems


async def main():
    problems = _coverage_problems()
    problems += await _model_device_problems()
    problems += await _diverge_anchor_problems()

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
        if fault in ISOLATED:
            spread = sorted(
                r.check.id for r in report.failures + report.warnings
                if r.check.id not in checks_for(fault))
            if spread:
                problems.append(
                    f"{fault} is listed ISOLATED and also broke {spread}. "
                    f"Either the narrowing that made it isolated has come "
                    f"undone -- in which case fix it rather than the table, "
                    f"because the whole point of that narrowing is that "
                    f"'caught by X' names a check and not a crowd -- or the "
                    f"extra failures are real, and it belongs in CASCADING "
                    f"with the excuse guard waived and the reason written "
                    f"down")
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

    # The verdict-shaped claims the one-fault matrix cannot express: runs that
    # must OBSERVE or SKIP (failing them would fail a conforming device -- the
    # tool-worse-than-no-tool case in this file's header) and a stacked run
    # that must FAIL for the right stated reason.
    print("\nTargeted scenarios")
    prompt = await _prompt_device_problems()
    problems += prompt
    if not prompt:
        print("  ok   a device too quick to pipeline against is reported as "
              "untestable, not as in violation")

    quiet = await run(faults=["obd_pid_never_answers"])
    result = result_for(quiet, "obd.poll_and_flag")
    if result is None:
        problems.append("obd_pid_never_answers: obd.poll_and_flag did not run")
    elif result.status is not Status.SKIP:
        problems.append(
            f"a conforming car whose polled PID nothing answers was reported "
            f"{result.status.value} by obd.poll_and_flag: {result.message}. "
            f"SPEC.md §15.4 makes that silence legal, so the only honest "
            f"verdict is a skip")
    else:
        print("  ok   a legally-unanswered poll is reported indeterminate, "
              "not failed")
    for check_id in ("obd.probe", "obd.poll_refusals", "obd.reset_stops"):
        result = result_for(quiet, check_id)
        if result is None or result.status not in (
                Status.PASS, Status.SKIP, Status.OBSERVE):
            problems.append(
                f"obd_pid_never_answers: {check_id} reported "
                f"{'nothing' if result is None else result.status.value} on "
                f"a conforming car that is merely quiet")

    stacked = await run(faults=["obd_pid_never_answers",
                                "obd_reprobe_refused"])
    result = result_for(stacked, "obd.poll_and_flag")
    if result is None:
        problems.append("obd_reprobe_refused: obd.poll_and_flag did not run")
    elif result.status is not Status.FAIL:
        problems.append(
            f"a device that refuses the diagnostic re-probe was reported "
            f"{result.status.value} by obd.poll_and_flag; §15.2 makes the "
            f"refusal a failure in its own right")
    elif "bad_params" not in (result.message or ""):
        problems.append(
            f"the re-probe failure must report the refusing status; the "
            f"message was: {result.message}")
    else:
        print("  ok   a refused diagnostic re-probe fails, naming the status")

    print()
    if problems:
        print("FAILED")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"ok: the reference peripheral passes, all {len(CAUGHT_BY)} seeded "
          f"defects were caught, and all {len(SCENARIO_FAULTS)} scenario "
          f"seeds verdict as required")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
