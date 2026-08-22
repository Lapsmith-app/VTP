"""The check registry.

A check is one clause of the specification, tested once, reported once. It
carries the section it comes from so the report can be read next to SPEC.md,
the capability bits it needs so a device that never claimed a role is not
failed for it, and a severity that says what a failure means.

Checks say what they found by raising: `Fail` for a violated MUST or SHOULD,
`Skip` for something this device or this platform cannot be asked, and
`Observe` for a measurement that is worth reporting and is nobody's pass or
fail. Returning normally is a pass.
"""
import dataclasses
import enum


class Status(enum.Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"
    OBSERVE = "observe"
    ERROR = "error"


class Fail(Exception):
    """The device did not do what the section requires.

    `severity` overrides the check's own for this one finding. A check can have
    a failure mode more serious than the rule it is mainly about: implementing
    GET_LINK_PARAMS is a SHOULD, but not ANSWERING it breaks the MUST in §9 that
    every request gets a response, and reporting that as a warning because of
    which check happened to notice would be reporting the wrong thing.
    """

    def __init__(self, message, severity=None, **evidence):
        super().__init__(message)
        self.message = message
        self.severity = severity
        self.evidence = evidence


class Skip(Exception):
    """Not applicable, or not answerable here."""

    def __init__(self, reason, **evidence):
        super().__init__(reason)
        self.reason = reason
        self.evidence = evidence


class Observe(Exception):
    """A measurement, not a verdict.

    Used where the harness can see something real but the specification does
    not make it pass or fail -- a rate, a PHY, a reported link parameter. Kept
    distinct from a pass so that a report cannot accumulate green ticks for
    things nothing was actually asserted about.
    """

    def __init__(self, message, **evidence):
        super().__init__(message)
        self.message = message
        self.evidence = evidence


#: Phases run in this order. The control plane comes before the stream verdicts
#: because the streams are collected in the background throughout -- a device is
#: watched while it is being interrogated, which is also the condition under
#: which its bookkeeping is most likely to slip. "observe" is where the harness
#: asks for the traffic it will then judge; the runner holds the link open for
#: the collection window between that phase and "streams".
PHASES = ("discovery", "gatt", "info", "control", "monitor", "observe",
          "streams", "transport", "reconnect")


@dataclasses.dataclass(frozen=True)
class Check:
    id: str
    section: str
    title: str
    severity: str          # MUST | SHOULD | OBSERVE
    requires: tuple        # capability bit names from SPEC.md §4
    phase: str
    adversarial: bool
    fn: object

    @property
    def failure_status(self):
        return Status.FAIL if self.severity == "MUST" else Status.WARN


@dataclasses.dataclass
class Result:
    check: Check
    status: Status
    message: str = ""
    evidence: dict = dataclasses.field(default_factory=dict)
    duration_s: float = 0.0


REGISTRY = []


def check(*, id, section, title, severity="MUST", requires=(), phase="streams",
          adversarial=False):
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    if severity not in ("MUST", "SHOULD", "OBSERVE"):
        raise ValueError(f"unknown severity {severity!r}")

    def wrap(fn):
        REGISTRY.append(Check(id=id, section=section, title=title,
                              severity=severity, requires=tuple(requires),
                              phase=phase, adversarial=adversarial, fn=fn))
        return fn
    return wrap


def load_all():
    """Import every check module, populating the registry."""
    from . import (discovery, info, control, monitor, streams,  # noqa: F401
                   transport, reconnect)
    return REGISTRY
