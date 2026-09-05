"""Ordering, skipping and running the checks against one device."""
import asyncio
import dataclasses
import time
import traceback

from . import refdec
from .checks import PHASES, Fail, Observe, Result, Skip, Status, load_all
from .session import (ControlEchoMismatch, ControlRefusedAtAtt,
                      ControlTimeout, Session, StreamLog)
from .transport import DeviceRefused, TransportError


@dataclasses.dataclass
class Report:
    results: list
    session: Session
    aborted: str = None
    started: float = 0.0
    duration_s: float = 0.0

    def by_status(self, status):
        return [r for r in self.results if r.status is status]

    @property
    def failures(self):
        return self.by_status(Status.FAIL)

    @property
    def warnings(self):
        return self.by_status(Status.WARN)

    @property
    def errors(self):
        return self.by_status(Status.ERROR)

    @property
    def conforms(self):
        """Every MUST that could be tested was met, and nothing broke."""
        return not self.failures and not self.errors and self.aborted is None

    @property
    def counts(self):
        out = {status.value: 0 for status in Status}
        for result in self.results:
            out[result.status.value] += 1
        return out


class Runner:
    def __init__(self, transport, *, adversarial=True, observe_s=12.0,
                 can_ids=(), reconnect=True, aiding_blob=None, on_result=None,
                 on_phase=None):
        self.transport = transport
        self.adversarial = adversarial
        self.observe_s = observe_s
        self.can_ids = list(can_ids)
        self.reconnect = reconnect
        # SPEC.md §14.6 -- the operator's own aiding, for a device that
        # refuses anything the harness could invent. None means the synthetic
        # pattern, which is what every check but aiding.transfer uses anyway.
        self.aiding_blob = aiding_blob
        self.on_result = on_result or (lambda result: None)
        self.on_phase = on_phase or (lambda phase, note: None)
        self.checks = load_all()

    async def run(self, target):
        session = Session(self.transport, adversarial=self.adversarial)
        session.state["can_ids"] = self.can_ids
        session.state["aiding_blob"] = self.aiding_blob
        report = Report(results=[], session=session, started=time.time())
        self._target = target
        began = time.monotonic()
        try:
            await self._run(session, target, report)
        except TransportError as exc:
            report.aborted = str(exc)
        except Exception as exc:                    # noqa: BLE001
            # Everything already found has to survive. A run that reaches the
            # reconnect phase has sixty results in it, and losing them to one
            # unexpected error -- in a backend call this transport does not
            # wrap, or in teardown -- costs the whole session and tells the
            # user nothing about their device.
            report.aborted = (f"{type(exc).__name__}: {exc}\n"
                              + traceback.format_exc(limit=6))
        finally:
            report.duration_s = time.monotonic() - began
            try:
                await session.close()
            except Exception:                       # noqa: BLE001 - teardown
                pass
        return report

    async def _run(self, session, target, report):
        await session.open(target)
        await session.read_info()
        await session.start_streams()
        await session.start_control()
        # A short settle before anything is asked for. It is also the window
        # can.silent_until_asked judges: SPEC.md §9.2 clears the subscription
        # table on connect, so nothing should arrive in it.
        await asyncio.sleep(1.0)

        # Sliced from PHASES rather than listed again. The list used to be
        # written out here as well, so a phase added to PHASES was registered,
        # ordered, reported on -- and never run, which surfaces as checks that
        # silently do not exist rather than as an error.
        #
        # "observe" ends the interrogation and "reconnect" is driven separately
        # below, so those two names are the only structure this needs.
        interrogate = PHASES[:PHASES.index("observe") + 1]
        after_collection = PHASES[PHASES.index("observe") + 1:
                                  PHASES.index("reconnect")]

        for phase in interrogate:
            await self._phase(session, report, phase)

        self.on_phase("collect", f"listening for {self.observe_s:.0f}s")
        await asyncio.sleep(self.observe_s)

        for phase in after_collection:
            await self._phase(session, report, phase)

        if self.reconnect:
            await self._reconnect(session, report)
            await self._phase(session, report, "reconnect")

    async def _reconnect(self, session, report):
        self.on_phase("reconnect", "dropping the link and connecting again")
        session.state["installed_before_reconnect"] = dict(
            session.state.get("installed") or {})
        session.state["info_first_connection"] = session.info_raw
        try:
            await session.close()
            await asyncio.sleep(1.0)
            session.streams = {name: StreamLog(name) for name in session.streams}
            session.state.pop("_decoded", None)
            await session.open(self._target)
            await session.read_info()
            await session.start_streams()
            await session.start_control()
            session.state["reconnected"] = True
            await asyncio.sleep(2.0)
        except TransportError as exc:
            session.note(f"could not reconnect to test SPEC.md §9.2 and 8.2: {exc}")
            session.state["reconnected"] = False

    async def _phase(self, session, report, phase):
        selected = [c for c in self.checks if c.phase == phase]
        if not selected:
            return
        self.on_phase(phase, f"{len(selected)} check(s)")
        for check in selected:
            result = await self._one(session, check)
            report.results.append(result)
            self.on_result(result)

    async def _one(self, session, check):
        began = time.monotonic()

        def finish(status, message="", evidence=None, severity=None):
            return Result(check=check, status=status, message=message,
                          evidence=evidence or {}, severity=severity,
                          duration_s=time.monotonic() - began)

        missing = [c for c in check.requires if c not in session.capabilities]
        if missing:
            return finish(Status.SKIP,
                          f"the device does not declare {', '.join(missing)}")
        if check.adversarial and not session.adversarial:
            return finish(Status.SKIP, "adversarial checks are disabled")
        try:
            await check.fn(session)
        except Fail as exc:
            severity = exc.severity or check.severity
            return finish(Status.FAIL if severity == "MUST" else Status.WARN,
                          exc.message, exc.evidence, severity=severity)
        except Skip as exc:
            return finish(Status.SKIP, exc.reason, exc.evidence)
        except Observe as exc:
            return finish(Status.OBSERVE, exc.message, exc.evidence)
        except ControlRefusedAtAtt as exc:
            if exc.needs_pairing:
                # SPEC.md §10 -- the device wants an encrypted link and this
                # host has not paired with it. Pairing is the client's job,
                # and a client meeting this MUST NOT report the device as
                # faulty: not verified, and the operator is told what to do.
                return finish(Status.SKIP,
                              f"{refdec.opcode_name(exc.opcode)} was refused "
                              f"at the ATT layer ({exc.reason}). SPEC.md §10 "
                              f"puts pairing on the client: pair this host "
                              f"with the device and run again", exc.evidence)
            # SPEC.md §9.4 -- a device that declares control answers with a
            # response and never at the ATT layer. A MUST regardless of the
            # severity of the check that happened to provoke it; it used to
            # reach the TransportError clause below and abort (issue #61).
            message, evidence = _with_superseded(exc, str(exc), exc.evidence)
            return finish(Status.FAIL, message, evidence, severity="MUST")
        except (ControlTimeout, ControlEchoMismatch) as exc:
            # SPEC.md §9 -- a device MUST respond to every request it applies
            # and MUST echo the opcode and tag. Both are MUSTs regardless of
            # the severity of the check that happened to provoke them.
            message, evidence = _with_superseded(
                exc, str(exc), getattr(exc, "evidence", {}))
            return finish(Status.FAIL, message, evidence, severity="MUST")
        except DeviceRefused as exc:
            # Before TransportError, which it subclasses. The transport says
            # "refused" only while the link is up, so the device is still
            # there for every check after this one; what this check cannot
            # say is what it was going to, because an operation it did not
            # guard was refused. An error in this check's run, not a dead
            # link -- which is what letting it through made it (issue #61).
            return finish(Status.ERROR,
                          f"the device refused an operation this check did "
                          f"not expect it to: {exc}")
        except TransportError:
            raise
        except Exception as exc:                    # noqa: BLE001
            return finish(Status.ERROR, f"{type(exc).__name__}: {exc}",
                          {"traceback": traceback.format_exc(limit=4)})
        return finish(Status.PASS)


def _with_superseded(exc, message, evidence):
    """A control-layer finding raised while a Fail was already propagating.

    A cleanup request in a check's `finally` that times out or is refused
    replaces the check's own verdict on the way out, and the report showed
    only the replacement: the rule the check was written for was lost to
    the rule the cleanup broke. Both are true of that device, so both are
    reported -- the control-layer MUST as the verdict, and the finding it
    superseded beside it.
    """
    context = exc.__context__
    if not isinstance(context, Fail):
        return message, evidence
    evidence = dict(evidence, superseded=context.message)
    return (f"{message}. This was raised by the check's cleanup and "
            f"superseded its own finding: {context.message}"), evidence


async def run_once(transport, target, **kwargs):
    return await Runner(transport, **kwargs).run(target)
