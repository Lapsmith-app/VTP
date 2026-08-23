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
        # Always kilometres, to three places. The unit is part of the static
        # cell label and cannot change per value, so a formatter that switched
        # to bare metres below 1 km rendered 999 m as "999" under a heading
        # that said "km" -- a thousand-fold error, displayed confidently, on
        # the one screen a driver reads at speed. Three decimals of a
        # kilometre is metre resolution, which is the channel's own unit, so
        # nothing is lost by never switching.
        return f"{value / 1000:.3f}"
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


def format_can_id(can_id, mask):
    """`0x0C0` for an exact subscription, `0x100/1FFFFF0` for a masked one."""
    exact = 0x3FFFFFFF
    return (f"0x{can_id:03X}" if mask == exact
            else f"0x{can_id:03X}/{mask:X}")


SUB_MODES = {0: "every", 1: "periodic"}


class MonitorDisplay:
    """The device's screen, and a debug panel for whatever is talking to it.

    The layout follows the questions that came up bringing a client onto this
    protocol, in the order they came up:

      * Is a client connected?
      * Which characteristics has it subscribed to? A device can have CAN ids
        installed and no subscriber on the CAN characteristic, and it then
        produces batches that go nowhere — which from the client side looks
        exactly like a decode bug.
      * Is each stream moving, and how fast? A total cannot tell a stalled
        stream from a slow one, and every stall here looked like a large number
        that had stopped growing.
      * Is anything being lost, and to what?
      * What did the client last ask for, and what was it told?

    Every value is a widget rather than a line of preformatted text, so each
    can carry its own colour: dim means idle, bright means active, amber wants
    attention, red is loss. That is the difference between a panel you read and
    one you scan.
    """

    BG, PANEL, RULE = "#0b0d10", "#151a21", "#232a34"
    FG, DIM, MUTED = "#f2f5f7", "#3f4854", "#8b98a6"
    OK, WARN, BAD = "#5eead4", "#fbbf24", "#f87171"
    MONO = "SF Mono"

    STREAMS = ("gps", "can", "imu")
    COLUMNS = ("sent", "rate", "refused", "no-sub", "dropped")

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
        self.root.geometry("880x700")
        self.root.minsize(760, 560)

        self._cells = {}
        self._built_for = None
        # Tk redraws on every configure() whether or not anything changed, and
        # root.update() blocks the caller's loop while it does. Reconfiguring
        # forty labels twenty times a second cost this peripheral 40% of its
        # BLE throughput -- the window describing the transport was starving
        # it. Nothing is written that is already on screen.
        self._painted = {}

        self._build_scroller()
        self._build_header()
        self._build_streams()
        self._build_can()
        self._build_control()
        self._build_monitor()

    # -- construction -----------------------------------------------------

    def _build_scroller(self):
        """A scrollable body, because the panel is taller than the window.

        Everything below is packed into `self.content` rather than the window
        itself. Without this the last section — the Monitor values, which are
        the part that is a product rather than a diagnostic — sits below the
        bottom edge and cannot be reached at all.
        """
        tk = self._tk
        outer = tk.Frame(self.root, bg=self.BG)
        outer.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(outer, bg=self.BG, highlightthickness=0,
                                borderwidth=0)
        bar = tk.Scrollbar(outer, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.content = tk.Frame(self.canvas, bg=self.BG)
        self._content_id = self.canvas.create_window(
            (0, 0), window=self.content, anchor="nw")

        # bbox("all") walks every item on the canvas, and rebinding it to every
        # Configure event ran that walk inside the caller's loop on each
        # repaint. The scroll region only changes when the content changes
        # HEIGHT, which after the first layout it almost never does.
        self._scroll_height = None

        def resized(_event):
            height = self.content.winfo_reqheight()
            if height != self._scroll_height:
                self._scroll_height = height
                self.canvas.configure(
                    scrollregion=(0, 0, self.content.winfo_reqwidth(), height))

        self.content.bind("<Configure>", resized)
        # Keep the content as wide as the viewport so the grids still stretch.
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._content_id, width=e.width))

        def wheel(event):
            # macOS reports small deltas already in the right direction.
            self.canvas.yview_scroll(-1 * int(event.delta), "units")
        for widget in (self.root, self.canvas):
            widget.bind_all("<MouseWheel>", wheel)

    def _panel(self, title):
        """A titled card. Returns the frame its contents go in."""
        tk = self._tk
        outer = tk.Frame(self.content, bg=self.PANEL)
        outer.pack(fill="x", padx=18, pady=(0, 10))
        head = tk.Frame(outer, bg=self.PANEL)
        head.pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(head, text=title, bg=self.PANEL, fg=self.MUTED,
                 font=(self.MONO, 9, "bold"), anchor="w").pack(side="left")
        tk.Frame(outer, bg=self.RULE, height=1).pack(
            fill="x", padx=14, pady=(6, 0))
        body = tk.Frame(outer, bg=self.PANEL)
        body.pack(fill="x", padx=14, pady=(8, 12))
        return body

    def _cell(self, parent, text, row, column, *, colour=None, size=12,
              weight="normal", anchor="e", pad=(10, 2)):
        label = self._tk.Label(parent, text=text, bg=self.PANEL,
                               fg=colour or self.FG,
                               font=(self.MONO, size, weight), anchor=anchor)
        label.grid(row=row, column=column, sticky="ew",
                   padx=pad[0], pady=pad[1])
        return label

    def _set(self, widget, text=None, fg=None, **rest):
        """configure() only what actually changed."""
        key = id(widget)
        now = (text, fg, tuple(sorted(rest.items())))
        if self._painted.get(key) == now:
            return
        self._painted[key] = now
        options = dict(rest)
        if text is not None:
            options["text"] = text
        if fg is not None:
            options["fg"] = fg
        widget.configure(**options)

    def _build_header(self):
        tk = self._tk
        bar = tk.Frame(self.content, bg=self.BG)
        bar.pack(fill="x", padx=18, pady=(14, 10))

        self.dot = tk.Label(bar, text="\u25cf", bg=self.BG, fg=self.DIM,
                            font=(self.MONO, 15))
        self.dot.pack(side="left", padx=(0, 8))
        self.state_label = tk.Label(bar, text="", bg=self.BG, fg=self.DIM,
                                    font=(self.MONO, 14, "bold"), anchor="w")
        self.state_label.pack(side="left")
        self.meta_label = tk.Label(bar, text="", bg=self.BG, fg=self.DIM,
                                   font=(self.MONO, 11), anchor="e")
        self.meta_label.pack(side="right")

        chips = tk.Frame(self.content, bg=self.BG)
        chips.pack(fill="x", padx=18, pady=(0, 12))
        tk.Label(chips, text="NOTIFY", bg=self.BG, fg=self.MUTED,
                 font=(self.MONO, 9, "bold")).pack(side="left", padx=(0, 10))
        self.chips = {}
        for name in ("gps", "can", "imu", "control"):
            chip = tk.Label(chips, text=f" {name} ", bg=self.PANEL,
                            fg=self.DIM, font=(self.MONO, 11), padx=6, pady=2)
            chip.pack(side="left", padx=3)
            self.chips[name] = chip
        self.chip_note = tk.Label(chips, text="", bg=self.BG, fg=self.BAD,
                                  font=(self.MONO, 11))
        self.chip_note.pack(side="left", padx=(12, 0))

    def _build_streams(self):
        body = self._panel("STREAMS")
        body.grid_columnconfigure(0, minsize=90, weight=0)
        for i in range(len(self.COLUMNS)):
            body.grid_columnconfigure(i + 1, minsize=96, weight=1)

        self._cell(body, "", 0, 0)
        for i, name in enumerate(self.COLUMNS):
            self._cell(body, name, 0, i + 1, colour=self.MUTED, size=10)

        self.stream_cells = {}
        for r, stream in enumerate(self.STREAMS, start=1):
            self._cell(body, stream.upper(), r, 0, colour=self.MUTED,
                       size=12, weight="bold", anchor="w")
            self.stream_cells[stream] = [
                self._cell(body, "0", r, c + 1, size=13)
                for c in range(len(self.COLUMNS))]
        self.stream_note = self._cell(
            body, "", len(self.STREAMS) + 1, 0, colour=self.DIM, size=10,
            anchor="w", pad=(10, (8, 0)))
        self.stream_note.grid(columnspan=len(self.COLUMNS) + 1)

    def _build_can(self):
        self.can_body = self._panel("CAN SUBSCRIPTIONS")
        for i, width in enumerate((200, 120, 90)):
            self.can_body.grid_columnconfigure(i, minsize=width, weight=1)
        self._can_rows = []
        # The empty-state label is its own widget, NOT a short row in the row
        # list. Keeping it there made a one-widget row that a later three-column
        # update indexed into, and the IndexError killed the peripheral.
        self._can_empty = self._cell(self.can_body, "none installed", 0, 0,
                                     colour=self.DIM, size=12, anchor="w")
        self._can_empty.grid(columnspan=3)

    def _build_control(self):
        self.ctrl_body = self._panel("CONTROL")
        self.ctrl_body.grid_columnconfigure(0, minsize=80, weight=0)
        self.ctrl_body.grid_columnconfigure(1, minsize=380, weight=1)
        self.ctrl_body.grid_columnconfigure(2, minsize=140, weight=0)
        self._ctrl_rows = []
        self._ctrl_empty = self._cell(self.ctrl_body, "no control requests yet",
                                      0, 0, colour=self.DIM, size=11,
                                      anchor="w")
        self._ctrl_empty.grid(columnspan=3)

    def _build_monitor(self):
        self.monitor_body = self._panel("MONITOR")

    # -- per-update rendering ---------------------------------------------

    def _fit_rows(self, parent, wanted, rows, columns, empty, start_row=1):
        """Grow or shrink a grid to `wanted` rows of exactly `columns` cells.

        Every row is built to the full width here, so no caller can index past
        the end of a short one. The empty-state label is hidden or shown rather
        than stored as a row, which is what went wrong before: a placeholder
        row of one widget lived in the same list as three-column rows, and the
        first real update indexed into it and took the process down.
        """
        while len(rows) < wanted:
            r = len(rows) + start_row
            rows.append([self._cell(parent, "", r, c, size=11)
                         for c in range(columns)])
        while len(rows) > wanted:
            for widget in rows.pop():
                widget.destroy()
        if wanted:
            empty.grid_remove()
        else:
            empty.grid()
        return rows

    def _build_cells(self, state):
        for child in self.monitor_body.winfo_children():
            child.destroy()
        self._cells.clear()
        tk = self._tk
        if not state:
            tk.Label(self.monitor_body, text="device requested no channels",
                     bg=self.PANEL, fg=self.DIM,
                     font=(self.MONO, 12)).pack(pady=14)
            self._built_for = ()
            return
        columns = 3 if len(state) > 4 else 2
        for i, (slot, channel, _, _) in enumerate(state):
            cell = tk.Frame(self.monitor_body, bg=self.PANEL)
            cell.grid(row=i // columns, column=i % columns,
                      sticky="nsew", padx=(0, 22), pady=(0, 10))
            unit = unit_of(channel)
            tk.Label(cell, text=LABELS.get(channel, f"CH{channel}")
                     + (f"  {unit}" if unit else ""),
                     bg=self.PANEL, fg=self.MUTED, font=(self.MONO, 9, "bold"),
                     anchor="w").pack(fill="x")
            value = tk.Label(cell, text=ABSENT, bg=self.PANEL, fg=self.DIM,
                             font=(self.MONO, 25, "bold"), anchor="w")
            value.pack(fill="x")
            self._cells[slot] = value
        for c in range(columns):
            self.monitor_body.grid_columnconfigure(c, weight=1)
        self._built_for = tuple((s, c) for s, c, _, _ in state)

    def update(self, state, tele):
        self._update_header(tele)
        self._update_streams(tele)
        self._update_can(tele["can_table"])
        self._update_control(tele["control"])

        if tuple((s, c) for s, c, _, _ in state) != self._built_for:
            self._build_cells(state)
        for slot, channel, value, present in state:
            cell = self._cells.get(slot)
            if cell is not None:
                self._set(cell, format_value(channel, value, present),
                          self.FG if present else self.DIM)

    def _update_header(self, tele):
        connected = tele["connected"]
        mins, secs = divmod(int(tele["uptime"]), 60)
        self._set(self.dot, fg=self.OK if connected else self.DIM)
        self._set(self.state_label, "CLIENT CONNECTED" if connected else "ADVERTISING",
                  self.FG if connected else self.MUTED)
        self._set(self.meta_label,
                  f"up {mins}m{secs:02d}s     MTU {tele['mtu']}     "
                 f"gps {tele['configured']['gps']} Hz   "
                 f"imu {tele['configured']['imu']} Hz")

        subscribed = tele["subscribed"]
        for name, chip in self.chips.items():
            on = subscribed is not None and name in subscribed
            self._set(chip, fg=self.OK if on else self.DIM,
                      bg=self.RULE if on else self.PANEL)
        silent = (subscribed is not None and tele["can_table"]
                  and "can" not in subscribed)
        self._set(self.chip_note,
                  "CAN ids installed, nothing subscribed to CAN" if silent else "")

    def _update_streams(self, tele):
        stalled = []
        for stream, cells in self.stream_cells.items():
            sent = tele["sent"][stream]
            rate = tele["rate"][stream]
            refused = tele["refused"][stream]
            nosub = tele["unwanted"][stream]
            dropped = tele["pending_dropped"][stream]

            self._set(cells[0], f"{sent:,}",
                      self.FG if sent else self.DIM)
            self._set(cells[1], f"{rate:.1f}/s" if rate else "—",
                      self.OK if rate else self.DIM)
            self._set(cells[2], f"{refused:,}" if refused else "—",
                      self.BAD if refused else self.DIM)
            self._set(cells[3], f"{nosub:,}" if nosub else "—",
                      self.WARN if nosub else self.DIM)
            self._set(cells[4], f"{dropped:,}" if dropped else "—",
                      self.BAD if dropped else self.DIM)
            if nosub and not sent:
                stalled.append(stream)
        self._set(self.stream_note,
                  ("no-sub counts notifications produced for a characteristic "
                  "nobody subscribed to — not loss: "
                  + ", ".join(stalled) + " going nowhere")
            if stalled else
            "no-sub is not loss; refused and dropped are")

    def _update_can(self, table):
        self._can_rows = self._fit_rows(self.can_body, len(table),
                                        self._can_rows, 3, self._can_empty)
        for row, (can_id, mask, mode, arg) in zip(self._can_rows, table):
            values = (format_can_id(can_id, mask),
                      SUB_MODES.get(mode, f"mode {mode}"),
                      str(arg) if mode == 1 else "—")
            for i, (widget, value) in enumerate(zip(row, values)):
                self._set(widget, value, self.FG, anchor="w" if i < 1 else "e")

    def _update_control(self, control):
        entries = list(reversed(control))
        self._ctrl_rows = self._fit_rows(self.ctrl_body, len(entries),
                                         self._ctrl_rows, 3, self._ctrl_empty)
        for row, (ts, what, status) in zip(self._ctrl_rows, entries):
            self._set(row[0], ts, self.DIM, anchor="w")
            self._set(row[1], what, self.FG, anchor="w")
            self._set(row[2], status, self.OK if status == "ok" else self.WARN,
                      anchor="e")

    def pump(self):
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
    # The panel alone, with no Bluetooth, so the layout can be looked at and
    # the absent rendering checked without a client.
    import time
    d = MonitorDisplay(title="VTP display — demo")
    start = time.monotonic()
    while d.pump():
        t = time.monotonic() - start
        lap, done = int((t % 95) * 1000), int(t // 95)
        state = [(0, LAP_TIME, lap, True),
                 (1, LAST_LAP_TIME, 87_340, done >= 1),
                 (2, BEST_LAP_TIME, 86_090, done >= 2),
                 (3, DELTA_BEST, -1_250 + (lap % 3000), done >= 1),
                 (4, LAP_NUMBER, done + 1, True),
                 (5, SPEED, 38_000, True)]
        d.update(state, {
            "connected": True, "uptime": t, "mtu": 247,
            "subscribed": {"gps", "can", "control"},
            "sent": {"gps": int(t * 10), "can": int(t * 27), "imu": int(t * 5)},
            "refused": {"gps": 0, "can": 0, "imu": 0},
            "unwanted": {"gps": 0, "can": 0, "imu": int(t)},
            "rate": {"gps": 10.0, "can": 27.0, "imu": 5.0},
            "pending_dropped": {"gps": 0, "can": 0, "imu": 0},
            "can_table": [(0x0C0, 0x3FFFFFFF, 0, 0),
                          (0x1A0, 0x3FFFFFFF, 1, 40),
                          (0x2E0, 0x1FFFFF00, 1, 5)],
            "control": [("12:00:01", "CAN_SUBSCRIBE tag=2 id=0x0C0 mode=0 arg=0", "ok"),
                        ("12:00:01", "TIME_SYNC tag=5", "ok")],
            "configured": {"gps": 10, "imu": 100},
            "monitor_seq": int(t), "monitor_updates": int(t * 2),
        })
        time.sleep(0.05)
    sys.exit(0)
