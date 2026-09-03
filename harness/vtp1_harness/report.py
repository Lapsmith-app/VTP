"""What the run found, written so it can be read next to SPEC.md.

The report states what was not verified as prominently as what passed. A green
run of this harness is evidence about a device, not a certificate: SPEC.md §12.1
names the requirements no client-side test can reach, and a report that quietly
omitted them would be claiming the one thing this repository is careful never to
claim.
"""
import json
import platform
import sys

from . import refdec
from .checks import Status

_SYMBOL = {
    Status.PASS: "PASS",
    Status.FAIL: "FAIL",
    Status.WARN: "WARN",
    Status.SKIP: "skip",
    Status.OBSERVE: "····",
    Status.ERROR: "ERR ",
}

_COLOUR = {
    Status.PASS: "\033[32m",
    Status.FAIL: "\033[31;1m",
    Status.WARN: "\033[33m",
    Status.SKIP: "\033[90m",
    Status.OBSERVE: "\033[36m",
    Status.ERROR: "\033[35;1m",
}
_RESET = "\033[0m"

SECTION_TITLES = {
    "2": "Transport", "2.1": "Link-layer payload", "2.2": "PHY",
    "2.3": "Connection parameters", "3.1": "UUID allocation",
    "3.2": "The family prefix", "3.3": "Advertisement",
    "3.4": "Device Information", "4": "Info characteristic",
    "5": "GPS characteristic", "5.1": "GPS validity", "5.2": "Fix type",
    "6": "CAN characteristic", "7": "IMU characteristic",
    "8.1": "The clock", "8.2": "Sequence", "8.3": "Loss",
    "9": "Control characteristic", "9.1": "CAN subscriptions",
    "9.2": "Overlapping subscriptions", "9.3": "Load",
    "9.4": "The request lifecycle", "9.5": "TIME_SYNC",
    "9.6": "Setting a rate", "9.7": "Power",
    "10": "Security", "12.1": "What the corpus does not cover",
    "13.1": "The device asks; the client supplies",
    "13.2": "Channels", "13.3": "The declaration", "13.4": "Values",
    "13.5": "Freshness", "14": "Aiding characteristic",
    "14.1": "The device declares; the client supplies",
    "14.2": "What the device already holds",
    "14.3": "Opening a transfer, and filling it", "14.4": "Closing it",
}


def _use_colour(stream):
    return hasattr(stream, "isatty") and stream.isatty()


class ConsoleReporter:
    """Streams results as they happen, then prints the summary."""

    def __init__(self, stream=None, verbose=False):
        self.stream = stream or sys.stdout
        self.colour = _use_colour(self.stream)
        self.verbose = verbose
        self._phase = None

    def _write(self, text=""):
        print(text, file=self.stream, flush=True)

    def _paint(self, status, text):
        if not self.colour:
            return text
        return f"{_COLOUR[status]}{text}{_RESET}"

    def phase(self, name, note):
        self._write()
        self._write(f"  {name.upper()}  {note}")

    def result(self, result):
        if result.status is Status.SKIP and not self.verbose:
            return
        if result.status is Status.PASS and not self.verbose:
            self._write(f"    {self._paint(Status.PASS, 'PASS')}  "
                        f"{result.check.section:>4}  {result.check.title}")
            return
        head = (f"    {self._paint(result.status, _SYMBOL[result.status])}  "
                f"{result.check.section:>4}  {result.check.title}")
        self._write(head)
        if result.message:
            for line in _wrap(result.message, 92):
                self._write(f"            {line}")
        if self.verbose and result.evidence:
            for key, value in result.evidence.items():
                self._write(f"            {key}: {_short(value)}")

    def summary(self, report):
        session = report.session
        self._write()
        self._write("=" * 78)
        self._write("  VTP/1 conformance harness")
        self._write("=" * 78)
        self._write(f"  device      {_device_line(session)}")
        self._write(f"  roles       {', '.join(sorted(session.capabilities)) or 'none declared'}")
        self._write(f"  link        {session.transport.kind}"
                    + (f", ATT MTU {session.mtu}" if session.mtu else ""))
        self._write(f"  host        {platform.system()} {platform.release()}, "
                    f"Python {platform.python_version()}")
        self._write(f"  duration    {report.duration_s:.1f}s")
        self._write()

        counts = report.counts
        line = (f"  {counts['pass']} passed   {counts['fail']} failed   "
                f"{counts['warn']} warnings   {counts['skip']} skipped   "
                f"{counts['observe']} observations")
        if counts["error"]:
            line += f"   {counts['error']} harness errors"
        self._write(line)

        # A skipped MUST is the one number a reader can misread as a pass, so
        # it gets its own line beside the counts rather than only appearing in
        # Not verified below. "47 passed, 0 failed" says nothing about the four
        # requirements nobody could reach.
        unverified = _unverified_musts(report)
        if unverified:
            self._write(f"  {len(unverified)} MUST requirement"
                        f"{'' if len(unverified) == 1 else 's'} could not be "
                        f"verified on this device -- see Not verified")

        if report.aborted:
            self._write()
            self._write(self._paint(Status.ERROR,
                                    f"  RUN ABORTED: {report.aborted}"))
            self._write("  The results above are everything that ran before the "
                        "link failed.")

        if report.failures:
            self._write()
            self._write(self._paint(Status.FAIL, "  Requirements not met"))
            for result in report.failures:
                self._write(f"    {result.check.section:>4}  {result.check.id}")
                for line in _wrap(result.message, 88):
                    self._write(f"          {line}")

        if report.warnings:
            self._write()
            # Not "SHOULDs not followed": a MUST check can produce a finding
            # at SHOULD level when what it found is not the device's to answer
            # for, and printing that under a heading about SHOULDs would say
            # the opposite of what the finding says.
            self._write(self._paint(Status.WARN, "  Findings short of a "
                                    "failed MUST"))
            for result in report.warnings:
                self._write(f"    {result.check.section:>4}  {result.check.id}")
                for line in _wrap(result.message, 88):
                    self._write(f"          {line}")

        if report.errors:
            self._write()
            self._write(self._paint(Status.ERROR, "  Harness errors "
                                    "(a fault in this tool, not necessarily the device)"))
            for result in report.errors:
                self._write(f"    {result.check.id}: {result.message}")

        self._write()
        self._write("  Not verified")
        for note in _not_verified(report):
            for i, line in enumerate(_wrap(note, 88)):
                self._write(("    - " if i == 0 else "      ") + line)

        self._write()
        if report.aborted or report.errors:
            verdict = "INCOMPLETE — the run did not finish"
            status = Status.ERROR
        elif report.failures:
            n = len(report.failures)
            verdict = (f"NOT CONFORMING — {n} requirement"
                       f"{'' if n == 1 else 's'} not met")
            status = Status.FAIL
        else:
            verdict = ("No requirement this harness can test was violated. "
                       "That is evidence, not a certificate — see Not verified.")
            status = Status.PASS
        self._write("  " + self._paint(status, verdict))
        self._write()


def _device_line(session):
    parts = []
    if session.advert is not None:
        parts.append(f"{session.advert.name or '(unnamed)'} "
                     f"[{session.advert.address}]")
    if session.info is not None:
        parts.append(f"VTP/{session.info['protocol_major']}."
                     f"{session.info['protocol_minor']}")
    for key in ("manufacturer_name", "model_number", "firmware_revision"):
        if session.dis.get(key):
            parts.append(session.dis[key])
    return "  ".join(parts) or "unknown"


def _unverified_musts(report):
    """MUST checks that ran but could not reach what they test.

    A skip because the device never claimed the role is not one of these: §12
    lets a device implement the roles it wants, and a check for a role it does
    not have has nothing to say. A skip for any other reason is a requirement
    this run did not test, and the report has to be able to name how many.
    """
    return [r for r in report.results
            if r.status is Status.SKIP and r.severity == "MUST"
            and r.message and "does not declare" not in r.message]


def _not_verified(report):
    """The standing statement, plus whatever this particular run could not reach.

    Assembled from the run rather than written out once, so a check that was
    skipped for a reason specific to this device or this platform appears here
    instead of vanishing into a skip count nobody reads.
    """
    notes = list(report.session.notes)
    for result in _unverified_musts(report):
        notes.append(f"SPEC.md {result.check.section} — {result.check.title}: "
                     f"not tested, because {result.message}.")
    if not notes:
        notes.append("Nothing beyond the standing limits above.")
    return notes


def _wrap(text, width):
    words, line, out = str(text).split(), "", []
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out or [""]


def _short(value, limit=160):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ---------------------------------------------------------------------------
# Machine-readable output
# ---------------------------------------------------------------------------

def to_dict(report):
    session = report.session
    return {
        "harness": {"version": _version(), "transport": session.transport.kind},
        "spec": {
            "major": refdec.PROTOCOL_MAJOR,
            "minor": refdec.SCHEMA["protocol"]["minor"],
        },
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "device": {
            "address": session.advert.address if session.advert else None,
            "name": session.advert.name if session.advert else None,
            "info": session.info,
            "info_hex": session.info_raw.hex() if session.info_raw else None,
            "capabilities": sorted(session.capabilities),
            "device_information": session.dis,
            "att_mtu": session.mtu,
        },
        "run": {
            "started": report.started,
            "duration_s": round(report.duration_s, 3),
            "aborted": report.aborted,
            "conforms": report.conforms,
            "counts": report.counts,
            # Beside `conforms`, because a machine reading this file has the
            # same way of misreading it as a person: a run that verified
            # nothing conforms too.
            "unverified_musts": [r.check.id for r in _unverified_musts(report)],
        },
        "results": [
            {
                "id": r.check.id,
                "section": r.check.section,
                "title": r.check.title,
                "severity": r.severity,
                "check_severity": r.check.severity,
                "adversarial": r.check.adversarial,
                "status": r.status.value,
                "message": r.message,
                "evidence": r.evidence,
                "duration_s": round(r.duration_s, 4),
            }
            for r in report.results
        ],
        "not_verified": _not_verified(report),
    }


def write_json(report, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(to_dict(report), fh, indent=2, default=str)
        fh.write("\n")


def write_markdown(report, path):
    """A report to paste into an issue.

    Markdown rather than HTML because the place a firmware developer takes a
    failing result is a bug tracker.
    """
    session = report.session
    counts = report.counts
    lines = [
        "# VTP/1 conformance report",
        "",
        f"- **Device**: {_device_line(session)}",
        f"- **Roles declared**: {', '.join(sorted(session.capabilities)) or 'none'}",
        f"- **Link**: {session.transport.kind}"
        + (f", ATT MTU {session.mtu}" if session.mtu else ""),
        f"- **Host**: {platform.platform()}",
        f"- **Duration**: {report.duration_s:.1f}s",
        "",
        f"**{counts['pass']} passed, {counts['fail']} failed, "
        f"{counts['warn']} warnings, {counts['skip']} skipped.**",
        "",
    ]
    unverified = _unverified_musts(report)
    if unverified:
        lines += [f"**{len(unverified)} MUST requirement"
                  f"{'' if len(unverified) == 1 else 's'} could not be verified "
                  f"on this device** — see Not verified.", ""]
    if report.aborted:
        lines += [f"> **Run aborted**: {report.aborted}", ""]

    by_section = {}
    for result in report.results:
        by_section.setdefault(result.check.section, []).append(result)

    lines += ["## Results", "",
              "| | Section | Check | Outcome |", "| --- | --- | --- | --- |"]
    for section in sorted(by_section, key=_section_key):
        for result in by_section[section]:
            title = SECTION_TITLES.get(section, "")
            lines.append(
                f"| {_SYMBOL[result.status].strip()} | "
                f"[§{section}](SPEC.md) {title} | {result.check.title} | "
                f"{_cell(result.message)} |")
    lines += ["", "## Not verified", ""]
    lines += [f"- {note}" for note in _not_verified(report)]
    lines += ["",
              "*A run with no failures is evidence that this device met every "
              "requirement this harness can test from a host. It is not a "
              "conformance certificate; see SPEC.md §12.1.*", ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ") or "—"


def _section_key(section):
    return tuple(int(part) for part in section.split("."))


def _version():
    try:
        from importlib.metadata import version
        return version("vtp1-harness")
    except Exception:                               # noqa: BLE001
        return "0.1.0+source"
