#!/usr/bin/env python3
"""The synthetic device's screen.

A Monitor device exists to display values it cannot compute, so the only way to
tell whether the role works end to end is to look at one. This draws what such a
device would show: the channels it asked for, the values the client supplied,
and — the part worth watching — an unmistakable difference between a value and
the absence of one.

Two layers again, for the same reason as vtp_device.py and serve.py. Everything
above `MonitorDisplay` is pure formatting with no GUI dependency, so CI checks it
on a machine with no display and no Tk. `MonitorDisplay` imports tkinter lazily,
because the interpreter CI runs does not have it and must not need it.

Formatting is where the channel enum pays off. Each channel has exactly one unit
fixed by SPEC.md §13.2, so a device renders `lap_time` as a lap time without
asking anyone anything — no unit negotiation, no scale factor, no configuration.
"""
import sys

# SPEC.md §13.2. Mirrored rather than imported so this module stays free of any
# dependency; the selftest asserts the two agree.
LAP_TIME, LAST_LAP_TIME, BEST_LAP_TIME = 1, 2, 3
DELTA_BEST, PREDICTED_LAP_TIME, LAP_NUMBER = 4, 5, 6
SPEED, SESSION_DISTANCE, SESSION_TIME = 7, 8, 9

LABELS = {
    LAP_TIME: "LAP", LAST_LAP_TIME: "LAST", BEST_LAP_TIME: "BEST",
    DELTA_BEST: "DELTA", PREDICTED_LAP_TIME: "PRED", LAP_NUMBER: "LAP No.",
    SPEED: "SPEED", SESSION_DISTANCE: "DIST", SESSION_TIME: "SESSION",
}

# What a device shows when the client has told it the value does not exist.
# Deliberately not a number, and deliberately not blank: SPEC.md §13.4 requires
# absence to be rendered, and a blank cell is indistinguishable from a device
# that has stopped drawing.
ABSENT = "—·—"


def _ms_to_clock(ms):
    """87340 -> '1:27.340'; 42318 -> '42.318'."""
    sign = "-" if ms < 0 else ""
    ms = abs(int(ms))
    minutes, rest = divmod(ms, 60_000)
    seconds, millis = divmod(rest, 1000)
    if minutes:
        return f"{sign}{minutes}:{seconds:02d}.{millis:03d}"
    return f"{sign}{seconds}.{millis:03d}"


def format_value(channel, value, present):
    """Render one channel. `present` false always wins."""
    if not present:
        return ABSENT
    if channel in (LAP_TIME, LAST_LAP_TIME, BEST_LAP_TIME,
                   PREDICTED_LAP_TIME, SESSION_TIME):
        return _ms_to_clock(value)
    if channel == DELTA_BEST:
        # A delta is only useful with its sign shown even when positive.
        return ("+" if value >= 0 else "") + _ms_to_clock(value)
    if channel == LAP_NUMBER:
        return str(value)
    if channel == SPEED:
        return f"{value * 0.0036:.1f}"          # mm/s -> km/h
    if channel == SESSION_DISTANCE:
        return f"{value / 1000:.2f}" if value >= 1000 else str(value)
    return str(value)


def unit_of(channel):
    return {SPEED: "km/h", SESSION_DISTANCE: "km"}.get(channel, "")


def render_lines(state):
    """A plain-text rendering of the whole screen, for logs and for tests.

    `state` is what VtpDevice.monitor_state() returns: (slot, channel, value,
    present) per requested channel.
    """
    return [f"{LABELS.get(ch, f'CH{ch}')}: {format_value(ch, v, p)}"
            for _, ch, v, p in state]


class MonitorDisplay:
    """A window showing the device's screen. macOS/Tk, created lazily."""

    BG, FG, DIM, ACCENT = "#0b0d10", "#f2f5f7", "#4b5563", "#5eead4"

    def __init__(self, title="VTP Logger — display"):
        try:
            import tkinter as tk
        except ImportError:  # pragma: no cover - depends on the interpreter
            raise RuntimeError(
                "tkinter is not available in this interpreter; run without "
                "--display, or use the bundle make_macos_app.sh builds") from None
        self._tk = tk
        self.root = tk.Tk()
        self.root.title(title)
        self.root.configure(bg=self.BG)
        self.root.geometry("560x420")
        self._cells = {}
        self._built_for = None

        self.status = tk.Label(self.root, text="advertising — no client yet",
                               bg=self.BG, fg=self.DIM,
                               font=("SF Mono", 12), anchor="w")
        self.status.pack(fill="x", padx=18, pady=(14, 4))

        self.body = tk.Frame(self.root, bg=self.BG)
        self.body.pack(fill="both", expand=True, padx=18, pady=6)

        self.footer = tk.Label(self.root, text="", bg=self.BG, fg=self.DIM,
                               font=("SF Mono", 11), anchor="w", justify="left")
        self.footer.pack(fill="x", padx=18, pady=(4, 14))

    def _build(self, state):
        """Lay out one cell per requested channel. The device's declaration is
        fixed for a connection (SPEC.md §13.1), so this runs once per layout."""
        for child in self.body.winfo_children():
            child.destroy()
        self._cells.clear()
        tk = self._tk

        if not state:
            self._cells["__empty__"] = None
            tk.Label(self.body, text="device requested no channels",
                     bg=self.BG, fg=self.DIM, font=("SF Mono", 13)).pack(pady=40)
            self._built_for = ()
            return

        columns = 2 if len(state) > 3 else 1
        for i, (slot, channel, _, _) in enumerate(state):
            cell = tk.Frame(self.body, bg=self.BG)
            cell.grid(row=i // columns, column=i % columns,
                      sticky="nsew", padx=10, pady=8)
            label = f"{LABELS.get(channel, f'CH{channel}')}"
            unit = unit_of(channel)
            tk.Label(cell, text=label + (f"  {unit}" if unit else ""),
                     bg=self.BG, fg=self.DIM, font=("SF Mono", 11),
                     anchor="w").pack(fill="x")
            value = tk.Label(cell, text=ABSENT, bg=self.BG, fg=self.DIM,
                             font=("SF Mono", 34, "bold"), anchor="w")
            value.pack(fill="x")
            self._cells[slot] = value
        for c in range(columns):
            self.body.grid_columnconfigure(c, weight=1)
        self._built_for = tuple((s, c) for s, c, _, _ in state)

    def update(self, state, *, connected=False, seq=None, updates=0):
        layout = tuple((s, c) for s, c, _, _ in state)
        if layout != self._built_for:
            self._build(state)

        for slot, channel, value, present in state:
            cell = self._cells.get(slot)
            if cell is None:
                continue
            cell.configure(text=format_value(channel, value, present),
                           # Absent is dim; a supplied value is bright. The
                           # difference is the whole point of the present bit.
                           fg=self.FG if present else self.DIM)

        self.status.configure(
            text=("client connected" if connected
                  else "advertising — no client yet"),
            fg=self.ACCENT if connected else self.DIM)
        supplied = sum(1 for _, _, _, p in state if p)
        self.footer.configure(
            text=(f"{supplied}/{len(state)} channels supplied   "
                  f"updates {updates}"
                  + (f"   seq {seq}" if seq is not None else "")))

    def pump(self):
        """Service the GUI from the caller's loop. Returns False once closed."""
        try:
            self.root.update()
            return True
        except self._tk.TclError:
            return False

    def close(self):
        try:
            self.root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    # A standalone smoke test: no Bluetooth, just the screen, so the rendering
    # can be looked at without a client.
    import time
    demo = [(0, LAP_TIME, 0, False), (1, LAST_LAP_TIME, 0, False),
            (2, DELTA_BEST, 0, False), (3, LAP_NUMBER, 0, False)]
    d = MonitorDisplay(title="VTP display — demo")
    start = time.monotonic()
    while d.pump():
        t = time.monotonic() - start
        lap = int((t % 95) * 1000)
        done = int(t // 95)
        demo = [(0, LAP_TIME, lap, True),
                (1, LAST_LAP_TIME, 87_340, done >= 1),
                (2, DELTA_BEST, -1_250 + (lap % 3000), done >= 1),
                (3, LAP_NUMBER, done + 1, True)]
        d.update(demo, connected=True, seq=int(t * 10), updates=int(t * 10))
        time.sleep(0.05)
    sys.exit(0)
