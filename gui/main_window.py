"""AuraShell OS main window: dark cyberpunk GUI built with CustomTkinter.

What the window shows
---------------------
* A pulsing orb + audio-wave visualizer for the assistant state
  (IDLE / LISTENING / PROCESSING / SPEAKING).
* A "last command" card: what was said, the parsed intent, the spoken reply
  and a SUCCESS / FAILED status.
* A live command log, and a scrollable results table (file lists, processes).
* CPU / RAM / Disk cards fed by ONE metrics source (``metrics_provider``).
* A microphone button (also bound to the Space key) and a typed-command box.
* A modal confirmation dialog with a countdown for risky actions.

Public API (see ``PROJECT_PLAN.md`` section 4)
----------------------------------------------
``AuraShellWindow(on_text_command=None, on_mic_click=None, on_confirm=None,
                  *, metrics_provider=None, persist_log=False, log_path=None)``

Callbacks (all three run in a *worker thread*, never on the GUI thread):
    ``on_text_command(text: str)``        user submitted a typed command.
    ``on_mic_click()``                    user pressed the mic button / Space.
    ``on_confirm(token, approved: bool)`` user answered a confirmation dialog.

Methods (all thread-safe except ``run`` and ``export_log``):
    ``run()``                               start the Tk main loop.
    ``set_state(state)``                    idle / listening / processing / speaking.
    ``set_audio_level(level)``              0.0-1.0, drives the wave.
    ``append_log(message, level)``          add a log line.
    ``log_command(raw_text, intent, target)``  log a command, update the card.
    ``show_command_result(ok, reply)``      finish the card (SUCCESS / FAILED).
    ``show_status(message, level=...)``     status bar + log line.
    ``show_table(title, rows, columns=None)``  results table (rows: dicts or lists).
    ``ask_confirmation(token, text, ttl)``  open the confirmation dialog.
    ``close_confirmation(token)``           close it silently (answered by voice).
    ``export_log(path)``                    write the in-memory log to a file.
    ``close()``                             close the window.

``metrics_provider() -> dict`` is called about once per second in a background
thread. It should return the ``SystemInfo`` field names:
``cpu_percent``, ``ram_percent``, ``ram_total_bytes``, ``ram_available_bytes``,
``disk_percent``, ``disk_total_bytes``, ``disk_free_bytes``. The GUI never
imports psutil itself, so the numbers on screen are the numbers SystemInfo
reports (and speaks). ``make_metrics_provider(system_info)`` builds a provider
from a ``SystemInfo`` instance.

Privacy: the command log lives in memory only. Saving it to disk is OFF by
default (``persist_log=False``); it can be switched on in the UI, and
"Export" writes a file only when the user asks.

Wiring with the other modules (as agreed in the project report)
---------------------------------------------------------------
* STT: wherever it expects a level callback, pass ``window.set_audio_level``
  (a float 0..1, may be called from any thread many times per second).
* Executor / confirmations: when an action needs the user's OK, call
  ``window.ask_confirmation(token, summary, ttl)``; the answer comes back in
  ``on_confirm(token, approved)``. If the answer arrives by voice instead, call
  ``window.close_confirmation(token)``.
* Results: ``window.log_command(text, intent, target)``, then
  ``window.show_command_result(result.ok, result.message)`` with the spoken
  reply; tables (files, processes) go through ``window.show_table``.
* Metrics: ``metrics_provider=make_metrics_provider(SystemInfo())``.

Demo without the rest of the system (executes nothing, replies are scripted)::

    python -m gui.main_window          # press Speak / Space to play a command
    python -m gui.main_window --auto   # plays all of them by itself
    python -m gui.main_window --mic    # + real microphone level on the wave
"""

from __future__ import annotations

import itertools
import math
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
from collections import deque
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import Enum
from pathlib import Path
from tkinter import filedialog, ttk
from tkinter import font as tkfont
from typing import Any, Callable

import customtkinter as ctk

try:  # Optional: correct display of Arabic text inside Tk on Linux.
    import arabic_reshaper
    from bidi.algorithm import get_display
except ImportError:  # pragma: no cover
    arabic_reshaper = None
    get_display = None


# --------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------
BG = "#0A0D16"
PANEL = "#111626"
PANEL_ALT = "#0D1120"
BORDER = "#1F2842"
TEXT = "#E6EAF5"
MUTED = "#7C88AA"
CYAN = "#00E5FF"
VIOLET = "#B26BFF"
GREEN = "#3DFFA2"
AMBER = "#FFB547"
RED = "#FF4D6D"
IDLE_BLUE = "#51618F"

MAX_LOG_LINES = 500
MAX_INPUT_CHARS = 500
MAX_MESSAGE_CHARS = 1000
MAX_TABLE_ROWS = 500
MAX_CELL_CHARS = 200
QUEUE_BATCH = 200
METRICS_INTERVAL = 1.0
DEFAULT_CONFIRM_TTL = 15.0
MAX_CONFIRM_TTL = 600.0
DEFAULT_LOG_PATH = Path.home() / ".aurashell" / "gui_command_log.txt"

# Fonts are tried in order; the first installed family wins. The Arabic list
# starts with Noto Sans Arabic (sudo apt install fonts-noto-core).
UI_FONTS = ("Ubuntu", "Noto Sans", "DejaVu Sans", "Helvetica")
ARABIC_FONTS = ("Noto Sans Arabic", "Noto Naskh Arabic", "DejaVu Sans", "Ubuntu")
MONO_FONTS = ("JetBrains Mono", "Ubuntu Mono", "DejaVu Sans Mono", "Courier New")

# Words that make a confirmation look destructive (red dialog).
DANGER_WORDS = (
    "delete", "remove", "erase", "wipe", "format", "close", "kill", "shutdown",
    "power off", "poweroff", "restart", "reboot", "panic",
    "حذف", "امسح", "مسح", "اغلاق", "إغلاق", "اقفل", "اغلق", "أغلق", "طفي",
    "ايقاف", "إيقاف", "ريستارت", "اعادة تشغيل", "إعادة تشغيل", "طوارئ",
)


class AuraState(str, Enum):
    """The four states the visualizer can show."""

    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"


# state -> (title, subtitle, accent colour). State is always also written as
# text, so it is never conveyed by colour alone.
STATE_STYLE: dict[AuraState, tuple[str, str, str]] = {
    AuraState.IDLE: ("IDLE", "Waiting for a command", IDLE_BLUE),
    AuraState.LISTENING: ("LISTENING", "Listening for your voice...", CYAN),
    AuraState.PROCESSING: ("PROCESSING", "Understanding your request...", VIOLET),
    AuraState.SPEAKING: ("SPEAKING", "AuraShell is replying", GREEN),
}

# level -> (badge text, badge colour, message colour)
LEVELS: dict[str, tuple[str, str, str]] = {
    "info": ("INFO", CYAN, TEXT),
    "success": ("OK", GREEN, TEXT),
    "warning": ("WARN", AMBER, AMBER),
    "error": ("ERR", RED, "#FF8FA3"),
    "command": ("CMD", VIOLET, "#E4D0FF"),
    "system": ("SYS", MUTED, MUTED),
}

_ARABIC = re.compile(r"[؀-ۿ]")
_WHITESPACE = re.compile(r"\s+")


# --------------------------------------------------------------------------
# Small helpers (pure functions, unit-tested in tests/test_gui.py)
# --------------------------------------------------------------------------
def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _mix(a: str, b: str, t: float) -> str:
    """Blend colour ``a`` toward ``b`` by ``t`` (0..1)."""
    t = max(0.0, min(1.0, t))
    ra, rb = _rgb(a), _rgb(b)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ra, rb))


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _display_text(text: str) -> str:
    """Shape/reorder Arabic so Tk (no native bidi on Linux) shows it correctly.

    Use this for text that is only *displayed* (labels, log, table cells), never
    for text the user is still typing or that is sent to a handler.
    """
    text = str(text)
    if arabic_reshaper is None or get_display is None or not _ARABIC.search(text):
        return text
    try:
        return get_display(arabic_reshaper.reshape(text))
    except Exception:  # never let cosmetics break logging
        return text


def _clean_message(message: object) -> str:
    """Collapse whitespace/newlines to single spaces and cap the length."""
    text = _WHITESPACE.sub(" ", str(message)).strip()
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 1] + "…"
    return text


def _pick_font(root: tk.Misc, candidates: tuple[str, ...], fallback: str) -> str:
    available = set(tkfont.families(root))
    for name in candidates:
        if name in available:
            return name
    return fallback


def _as_state(state: AuraState | str) -> AuraState:
    if isinstance(state, AuraState):
        return state
    try:
        return AuraState(str(state).strip().lower())
    except ValueError:
        raise ValueError(
            f"Unknown state {state!r}; expected one of "
            f"{[s.value for s in AuraState]}"
        ) from None


def _num(value: object) -> float | None:
    """Return ``value`` as a finite float, or None (bool/NaN/inf/str -> None)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _human_bytes(count: float) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _is_dangerous(text: str) -> bool:
    lowered = text.casefold()
    return any(word in lowered for word in DANGER_WORDS)


_PERCENT_KEYS = ("cpu", "ram", "mem", "percent")


def _format_cell(key: object, value: object) -> str:
    """Format one table cell (sizes as KB/MB, CPU/RAM as percentages)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    name = str(key).casefold()
    number = _num(value)
    if number is not None:
        if ("size" in name or "bytes" in name) and number >= 0:
            return _human_bytes(number)
        if any(word in name for word in _PERCENT_KEYS):
            return f"{number:.1f}%"
        if isinstance(value, float):
            return f"{number:.1f}"
    return _clean_message(value)[:MAX_CELL_CHARS]


def _header_label(key: object) -> str:
    text = str(key)
    return text.replace("_", " ").upper() if text.isascii() else text


def _normalize_table(
    rows: object, columns: Iterable[object] | None = None
) -> tuple[list[str], list[list[str]], bool]:
    """Validate table input and return ``(headers, string_rows, truncated)``.

    ``rows`` is a list of dicts (columns = keys) or a list of lists/tuples
    (give ``columns`` for the headers). At most ``MAX_TABLE_ROWS`` rows are kept.
    """
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Iterable):
        raise TypeError("rows must be an iterable of dicts or sequences")
    items = list(itertools.islice(rows, MAX_TABLE_ROWS + 1))
    truncated = len(items) > MAX_TABLE_ROWS
    items = items[:MAX_TABLE_ROWS]
    keys = list(columns) if columns is not None else None

    if items and all(isinstance(row, Mapping) for row in items):
        if keys is None:
            keys, seen = [], set()
            for row in items:
                for key in row:
                    if key not in seen:
                        seen.add(key)
                        keys.append(key)
        table = [[_format_cell(key, row.get(key)) for key in keys] for row in items]
    elif all(isinstance(row, (list, tuple)) for row in items):
        width = len(keys) if keys is not None else max((len(r) for r in items), default=0)
        if keys is None:
            keys = [f"column {i + 1}" for i in range(width)]
        table = [
            [_format_cell(keys[i], row[i] if i < len(row) else None) for i in range(width)]
            for row in items
        ]
    else:
        raise TypeError("rows must be all dicts or all lists/tuples")
    return [_header_label(key) for key in keys or []], table, truncated


def make_metrics_provider(system_info: Any, disk_path: str = "/") -> Callable[[], dict]:
    """Build a ``metrics_provider`` from a ``core.system_info.SystemInfo``.

    The GUI itself never reads psutil: CPU/RAM come from ``snapshot()`` and
    Disk from ``disk()``, i.e. exactly the numbers SystemInfo reports.
    """

    def provider() -> dict:
        data = dict(system_info.snapshot())
        try:
            data.update(system_info.disk(disk_path))
        except Exception as exc:  # disk is optional; the other cards still work
            data["disk_error"] = str(exc)
        return data

    return provider


# --------------------------------------------------------------------------
# Visualizer: pulsing orb + audio wave
# --------------------------------------------------------------------------
class StateIndicator(ctk.CTkFrame):
    """Pulsing orb, state label and a bar-style audio wave.

    * IDLE        - slow breathing, flat low bars.
    * LISTENING   - bars follow the microphone level (``set_level``).
    * PROCESSING  - a scanner sweeps across the bars, fast pulse.
    * SPEAKING    - rolling speech-like waveform.
    """

    BAR_COUNT = 56
    FRAME_MS = 33  # ~30 fps
    ORB_SIZE = 64

    def __init__(self, master: tk.Misc, fonts: dict[str, ctk.CTkFont]) -> None:
        super().__init__(
            master,
            fg_color=PANEL,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        self._state = AuraState.IDLE
        self._color = STATE_STYLE[AuraState.IDLE][2]
        self._target_level = 0.0
        self._level = 0.0
        self._level_stamp = 0.0
        self._amps = [0.0] * self.BAR_COUNT
        self._bars: list[tuple[int, int]] = []
        self._wave_w = 1
        self._wave_h = 1
        self._after_id: str | None = None

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=18, pady=(12, 0))

        size = self.ORB_SIZE
        self._orb = tk.Canvas(
            header, width=size, height=size, bg=PANEL, highlightthickness=0
        )
        self._orb.pack(side="left")
        c = size / 2
        self._ripples = [
            self._orb.create_oval(c, c, c, c, outline=CYAN, width=2) for _ in range(2)
        ]
        self._core_glow = self._orb.create_oval(c, c, c, c, outline="", fill=PANEL)
        self._core = self._orb.create_oval(c, c, c, c, outline="", fill=CYAN)

        labels = ctk.CTkFrame(header, fg_color="transparent")
        labels.pack(side="left", padx=(14, 0))
        self._title = ctk.CTkLabel(
            labels, text="IDLE", font=fonts["title"], text_color=IDLE_BLUE, anchor="w"
        )
        self._title.pack(anchor="w")
        self._subtitle = ctk.CTkLabel(
            labels,
            text=STATE_STYLE[AuraState.IDLE][1],
            font=fonts["small"],
            text_color=MUTED,
            anchor="w",
        )
        self._subtitle.pack(anchor="w")

        self._wave = tk.Canvas(self, height=92, bg=PANEL, highlightthickness=0)
        self._wave.pack(fill="x", padx=18, pady=(4, 14))
        self._wave.bind("<Configure>", self._on_resize)

        self._tick()

    # -- public ------------------------------------------------------------
    @property
    def state(self) -> AuraState:
        return self._state

    @property
    def title_text(self) -> str:
        return str(self._title.cget("text"))

    def set_state(self, state: AuraState) -> None:
        if state is self._state:
            return
        self._state = state
        title, subtitle, _ = STATE_STYLE[state]
        self._title.configure(text=title)
        self._subtitle.configure(text=subtitle)

    def set_level(self, level: float) -> None:
        self._target_level = _clamp(level)
        self._level_stamp = time.monotonic()

    def destroy(self) -> None:
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        super().destroy()

    # -- drawing -----------------------------------------------------------
    def _on_resize(self, event: tk.Event) -> None:
        self._wave_w, self._wave_h = max(event.width, 1), max(event.height, 1)
        self._wave.delete("all")
        self._bars = []
        slot = self._wave_w / self.BAR_COUNT
        bar_w = max(2.0, slot * 0.52)
        mid = self._wave_h / 2
        for i in range(self.BAR_COUNT):
            x = (i + 0.5) * slot
            glow = self._wave.create_rectangle(
                x - bar_w * 1.1, mid, x + bar_w * 1.1, mid, outline="", fill=PANEL
            )
            bar = self._wave.create_rectangle(
                x - bar_w / 2, mid, x + bar_w / 2, mid, outline="", fill=self._color
            )
            self._bars.append((glow, bar))

    def _bar_targets(self, t: float) -> list[float]:
        n = self.BAR_COUNT
        env = [math.sin(math.pi * (i + 0.5) / n) ** 0.8 for i in range(n)]
        state = self._state

        if state is AuraState.LISTENING:
            fresh = (time.monotonic() - self._level_stamp) < 0.6
            # Without a live mic feed, show a gentle simulated level so the UI
            # still looks alive; real levels from set_audio_level() win.
            base = self._level if fresh else 0.28 + 0.14 * math.sin(t * 2.0)
            return [
                0.05
                + 0.95
                * _clamp(
                    base
                    * env[i]
                    * (0.55 + 0.45 * math.sin(t * 7 + i * 0.9) * math.sin(t * 3.1 + i * 0.37))
                )
                for i in range(n)
            ]
        if state is AuraState.PROCESSING:
            pos = (n - 1) * (0.5 + 0.5 * math.sin(t * 2.4))
            return [
                0.07 + 0.78 * math.exp(-(((i - pos) / 4.5) ** 2)) + 0.05 * env[i]
                for i in range(n)
            ]
        if state is AuraState.SPEAKING:
            mod = 0.55 + 0.45 * math.sin(t * 2.7)
            return [
                _clamp(
                    0.09
                    + 0.8
                    * env[i]
                    * mod
                    * (0.5 + 0.5 * math.sin(t * 8 + i * 0.5))
                    * (0.65 + 0.35 * math.sin(t * 3.3 + i * 0.2))
                )
                for i in range(n)
            ]
        return [0.04 + 0.025 * (1 + math.sin(t * 1.1 + i * 0.25)) for i in range(n)]

    def _draw_orb(self, t: float) -> None:
        speed = {
            AuraState.IDLE: 0.35,
            AuraState.LISTENING: 1.1,
            AuraState.PROCESSING: 2.4,
            AuraState.SPEAKING: 1.7,
        }[self._state]
        c = self.ORB_SIZE / 2
        for k, ring in enumerate(self._ripples):
            phase = (t * speed + k * 0.5) % 1.0
            r = 11 + phase * 19
            self._orb.coords(ring, c - r, c - r, c + r, c + r)
            self._orb.itemconfigure(ring, outline=_mix(self._color, PANEL, phase ** 0.7))
        breathe = 0.5 + 0.5 * math.sin(t * speed * 2 * math.pi)
        core_r = 9 + 2.5 * breathe
        glow_r = core_r + 5
        self._orb.coords(self._core_glow, c - glow_r, c - glow_r, c + glow_r, c + glow_r)
        self._orb.itemconfigure(self._core_glow, fill=_mix(self._color, PANEL, 0.72))
        self._orb.coords(self._core, c - core_r, c - core_r, c + core_r, c + core_r)
        self._orb.itemconfigure(self._core, fill=self._color)

    def _tick(self) -> None:
        t = time.monotonic()
        accent = STATE_STYLE[self._state][2]
        self._color = _mix(self._color, accent, 0.18)
        self._level += (self._target_level - self._level) * 0.4
        if (t - self._level_stamp) > 0.6:
            self._target_level *= 0.9  # decay when the feed stops

        title_color = accent if self._state is not AuraState.IDLE else MUTED
        self._title.configure(text_color=title_color)

        targets = self._bar_targets(t)
        mid = self._wave_h / 2
        half_max = max(mid - 4, 1)
        slot = self._wave_w / self.BAR_COUNT
        bar_w = max(2.0, slot * 0.52)
        for i, (glow, bar) in enumerate(self._bars):
            self._amps[i] += (targets[i] - self._amps[i]) * 0.35
            amp = self._amps[i]
            h = max(1.5, amp * half_max)
            x = (i + 0.5) * slot
            self._wave.coords(bar, x - bar_w / 2, mid - h, x + bar_w / 2, mid + h)
            self._wave.coords(glow, x - bar_w * 1.1, mid - h - 2, x + bar_w * 1.1, mid + h + 2)
            self._wave.itemconfigure(bar, fill=_mix(PANEL, self._color, 0.45 + 0.55 * amp))
            self._wave.itemconfigure(glow, fill=_mix(PANEL, self._color, 0.16 + 0.1 * amp))
        self._draw_orb(t)

        self._after_id = self.after(self.FRAME_MS, self._tick)


# --------------------------------------------------------------------------
# CPU / RAM / Disk card
# --------------------------------------------------------------------------
class MetricCard(ctk.CTkFrame):
    """Title, big percentage, progress bar, detail line and a sparkline."""

    HISTORY = 60

    def __init__(self, master: tk.Misc, title: str, fonts: dict[str, ctk.CTkFont]) -> None:
        super().__init__(
            master,
            fg_color=PANEL,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        self._history: deque[float] = deque(maxlen=self.HISTORY)
        self._color = CYAN

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=16, pady=(10, 0))
        ctk.CTkLabel(
            top, text=title, font=fonts["small_bold"], text_color=MUTED, anchor="w"
        ).pack(side="left")
        self._value = ctk.CTkLabel(
            top, text="--", font=fonts["metric"], text_color=CYAN, anchor="e"
        )
        self._value.pack(side="right")

        self._bar = ctk.CTkProgressBar(
            self, height=8, corner_radius=4, fg_color=BORDER, progress_color=CYAN
        )
        self._bar.set(0)
        self._bar.pack(fill="x", padx=16, pady=(4, 4))

        self._detail = ctk.CTkLabel(
            self, text=" ", font=fonts["small"], text_color=MUTED, anchor="w"
        )
        self._detail.pack(fill="x", padx=16)

        self._spark = tk.Canvas(self, height=28, bg=PANEL, highlightthickness=0)
        self._spark.pack(fill="x", padx=16, pady=(2, 10))
        self._spark.bind("<Configure>", lambda _e: self._draw_spark())

    @property
    def value_text(self) -> str:
        return str(self._value.cget("text"))

    @property
    def detail_text(self) -> str:
        return str(self._detail.cget("text"))

    @staticmethod
    def _color_for(percent: float) -> str:
        if percent >= 85:
            return RED
        if percent >= 60:
            return AMBER
        return CYAN

    def update_value(self, percent: float | None, detail: str = "") -> None:
        if percent is None:
            self._value.configure(text="N/A", text_color=MUTED)
            self._bar.set(0)
            self._detail.configure(text=_display_text(detail) or "unavailable")
            return
        percent = _clamp(percent, 0.0, 100.0)
        self._color = self._color_for(percent)
        self._history.append(percent)
        self._value.configure(text=f"{percent:.0f}%", text_color=self._color)
        self._bar.configure(progress_color=self._color)
        self._bar.set(percent / 100)
        self._detail.configure(text=_display_text(detail) or " ")
        self._draw_spark()

    def _draw_spark(self) -> None:
        canvas = self._spark
        canvas.delete("all")
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if len(self._history) < 2 or w < 10 or h < 10:
            return
        step = w / (self.HISTORY - 1)
        x0 = w - step * (len(self._history) - 1)
        points: list[float] = []
        for i, value in enumerate(self._history):
            points += [x0 + i * step, h - 2 - (value / 100) * (h - 4)]
        canvas.create_line(*points, fill=self._color, width=2, smooth=True)


# --------------------------------------------------------------------------
# "Last command and its result" card
# --------------------------------------------------------------------------
class LastCommandCard(ctk.CTkFrame):
    """Original phrase, intent, spoken reply and a SUCCESS/FAILED badge."""

    STATUS = {
        "none": ("NO COMMAND YET", MUTED),
        "pending": ("… PENDING", AMBER),
        "success": ("✔ SUCCESS", GREEN),
        "failed": ("✘ FAILED", RED),
    }

    def __init__(self, master: tk.Misc, fonts: dict[str, ctk.CTkFont]) -> None:
        super().__init__(
            master,
            fg_color=PANEL,
            corner_radius=14,
            border_width=1,
            border_color=BORDER,
        )
        self._status = "none"
        self.grid_columnconfigure(1, weight=1)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16, pady=(10, 4))
        ctk.CTkLabel(
            head, text="LAST COMMAND", font=fonts["small_bold"], text_color=MUTED
        ).pack(side="left")
        self._badge = ctk.CTkLabel(
            head,
            text="",
            font=fonts["small_bold"],
            corner_radius=8,
            width=118,
            height=22,
        )
        self._badge.pack(side="right")

        self._values: dict[str, ctk.CTkLabel] = {}
        for row, (key, caption) in enumerate(
            (("said", "You said"), ("intent", "Intent"), ("reply", "Reply")), start=1
        ):
            ctk.CTkLabel(
                self, text=caption, font=fonts["small"], text_color=MUTED, anchor="nw", width=70
            ).grid(row=row, column=0, sticky="nw", padx=(16, 8), pady=1)
            label = ctk.CTkLabel(
                self,
                text="—",
                font=fonts["body"],
                text_color=TEXT,
                anchor="w",
                justify="left",
                wraplength=460,
            )
            label.grid(row=row, column=1, sticky="ew", padx=(0, 16), pady=1)
            self._values[key] = label
        self.grid_rowconfigure(4, minsize=8)
        self.bind("<Configure>", self._on_resize)
        self._apply_status("none")

    def _on_resize(self, event: tk.Event) -> None:
        wrap = max(200, event.width - 120)
        for label in self._values.values():
            label.configure(wraplength=wrap)

    def _apply_status(self, status: str) -> None:
        self._status = status
        text, color = self.STATUS[status]
        self._badge.configure(
            text=text, text_color=color, fg_color=_mix(PANEL, color, 0.16)
        )

    # -- public ------------------------------------------------------------
    @property
    def status(self) -> str:
        return self._status

    @property
    def badge_text(self) -> str:
        return str(self._badge.cget("text"))

    def value(self, key: str) -> str:
        return str(self._values[key].cget("text"))

    def set_command(self, raw: str, intent: str | None, target: str | None) -> None:
        self._values["said"].configure(text=_display_text(raw) or "—")
        if intent:
            self._values["intent"].configure(
                text=_display_text(f"{intent}  ·  target: {target or '-'}")
            )
        else:
            self._values["intent"].configure(text="—")
        self._values["reply"].configure(text="…")
        self._apply_status("pending")

    def set_result(self, ok: bool, reply: str) -> None:
        self._values["reply"].configure(text=_display_text(reply) or "—")
        self._apply_status("success" if ok else "failed")


# --------------------------------------------------------------------------
# Confirmation dialog (modal, with countdown)
# --------------------------------------------------------------------------
class ConfirmationDialog(ctk.CTkToplevel):
    """Modal "are you sure?" window with a countdown and Confirm / Cancel.

    Escape, the window close button and a timeout all count as *cancel*. Enter
    does NOT confirm, to avoid an accidental approval.
    """

    def __init__(
        self,
        master: tk.Misc,
        token: object,
        text: str,
        ttl: float,
        danger: bool,
        fonts: dict[str, ctk.CTkFont],
        on_result: Callable[[object, bool, str], None],
    ) -> None:
        super().__init__(master, fg_color=PANEL)
        self.token = token
        self.text = text
        self.danger = danger
        self._on_result = on_result
        self._ttl = ttl
        self._deadline = time.monotonic() + ttl
        self._answered = False
        self._after_id: str | None = None
        accent = RED if danger else AMBER

        self.title("Confirm action")
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", lambda: self._answer(False, "closed"))
        self.bind("<Escape>", lambda _e: self._answer(False, "cancelled"))

        bar = ctk.CTkFrame(self, fg_color=accent, height=4, corner_radius=0)
        bar.pack(fill="x")
        ctk.CTkLabel(
            self, text="CONFIRMATION REQUIRED", font=fonts["title_small"], text_color=accent
        ).pack(padx=24, pady=(16, 0), anchor="w")
        ctk.CTkLabel(
            self,
            text="DESTRUCTIVE ACTION" if danger else "Please confirm this action",
            font=fonts["small_bold"],
            text_color=accent if danger else MUTED,
        ).pack(padx=24, anchor="w")

        box = ctk.CTkFrame(self, fg_color=PANEL_ALT, corner_radius=10)
        box.pack(fill="x", padx=24, pady=14)
        self._text_label = ctk.CTkLabel(
            box,
            text=_display_text(text),
            font=fonts["body"],
            text_color=TEXT,
            wraplength=380,
            justify="left",
            anchor="w",
        )
        self._text_label.pack(fill="x", padx=14, pady=12)

        self._countdown = ctk.CTkLabel(
            self, text="", font=fonts["small"], text_color=MUTED, anchor="w"
        )
        self._countdown.pack(fill="x", padx=24)
        self._progress = ctk.CTkProgressBar(
            self, height=6, corner_radius=3, fg_color=BORDER, progress_color=accent
        )
        self._progress.set(1.0)
        self._progress.pack(fill="x", padx=24, pady=(4, 14))

        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.pack(fill="x", padx=24, pady=(0, 20))
        buttons.grid_columnconfigure((0, 1), weight=1, uniform="b")
        self.cancel_button = ctk.CTkButton(
            buttons,
            text="Cancel",
            height=38,
            font=fonts["small_bold"],
            fg_color="transparent",
            border_width=1,
            border_color=MUTED,
            text_color=TEXT,
            hover_color=BORDER,
            command=lambda: self._answer(False, "cancelled"),
        )
        self.cancel_button.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.confirm_button = ctk.CTkButton(
            buttons,
            text="Confirm",
            height=38,
            font=fonts["small_bold"],
            fg_color=accent,
            hover_color=_mix(accent, "#FFFFFF", 0.25),
            text_color="#16060A",
            command=lambda: self._answer(True, "confirmed"),
        )
        self.confirm_button.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        self._center(master)
        self.after(80, self._grab)
        self._tick()

    # -- internals ---------------------------------------------------------
    def _center(self, master: tk.Misc) -> None:
        self.update_idletasks()
        w, h = 460, max(self.winfo_reqheight(), 250)
        x = master.winfo_rootx() + max((master.winfo_width() - w) // 2, 0)
        y = master.winfo_rooty() + max((master.winfo_height() - h) // 3, 0)
        self.geometry(f"{w}x{h}+{x}+{y}")

    def _grab(self) -> None:
        if self._answered:
            return
        try:
            self.lift()
            self.grab_set()  # modal: the main window ignores input meanwhile
            self.focus_force()
        except tk.TclError:  # window not viewable yet / no window manager
            pass

    def _tick(self) -> None:
        if self._answered:
            return
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            self._answer(False, "timed out")
            return
        self._countdown.configure(text=f"Auto-cancel in {math.ceil(remaining)}s")
        self._progress.set(_clamp(remaining / self._ttl))
        self._after_id = self.after(100, self._tick)

    def _stop(self) -> None:
        self._answered = True
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        try:
            self.grab_release()
        except tk.TclError:
            pass

    def _answer(self, approved: bool, reason: str) -> None:
        if self._answered:
            return
        self._stop()
        self.destroy()
        self._on_result(self.token, approved, reason)

    # -- public ------------------------------------------------------------
    @property
    def countdown_text(self) -> str:
        return str(self._countdown.cget("text"))

    def dismiss(self) -> None:
        """Close without calling the result callback."""
        if self._answered:
            return
        self._stop()
        self.destroy()


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class AuraShellWindow:
    """The AuraShell OS main window (see module docstring for the API)."""

    def __init__(
        self,
        on_text_command: Callable[[str], object] | None = None,
        on_mic_click: Callable[[], object] | None = None,
        on_confirm: Callable[[object, bool], object] | None = None,
        *,
        metrics_provider: Callable[[], Mapping[str, Any]] | None = None,
        persist_log: bool = False,
        log_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._on_text_command = on_text_command
        self._on_mic_click = on_mic_click
        self._on_confirm = on_confirm
        self._metrics_provider = metrics_provider
        self._log_path = Path(log_path) if log_path else DEFAULT_LOG_PATH
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._epoch = 0
        self._epoch_lock = threading.Lock()
        self._closed = False
        self._after_ids: dict[str, str] = {}
        self._stop_metrics = threading.Event()
        self._started = time.monotonic()
        self._command_count = 0
        self._state = AuraState.IDLE
        self._mic_active = False
        self._confirm: ConfirmationDialog | None = None
        self._table: tuple[str, list[str], list[list[str]]] = ("", [], [])
        self._log_lines: deque[str] = deque(maxlen=MAX_LOG_LINES)
        self._last_metrics_error = ""

        ctk.set_appearance_mode("dark")
        self._root = ctk.CTk()
        self._root.title("AuraShell OS")
        self._root.geometry("1120x800")
        self._root.minsize(980, 720)
        self._root.configure(fg_color=BG)

        ui = _pick_font(self._root, UI_FONTS, "TkDefaultFont")
        arabic = _pick_font(self._root, ARABIC_FONTS, ui)
        mono = _pick_font(self._root, MONO_FONTS, "TkFixedFont")
        self._font_families = {"ui": ui, "arabic": arabic, "mono": mono}
        self._fonts = {
            "brand": ctk.CTkFont(family=ui, size=22, weight="bold"),
            "title": ctk.CTkFont(family=ui, size=24, weight="bold"),
            "title_small": ctk.CTkFont(family=ui, size=17, weight="bold"),
            "metric": ctk.CTkFont(family=ui, size=26, weight="bold"),
            "small": ctk.CTkFont(family=ui, size=12),
            "small_bold": ctk.CTkFont(family=ui, size=12, weight="bold"),
            "body": ctk.CTkFont(family=arabic, size=13),
            "mono": ctk.CTkFont(family=mono, size=13),
        }

        self._build_layout()
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._root.bind_all("<space>", self._on_space)

        self._schedule("queue", 40, self._drain_queue)
        self._schedule("session", 1000, self._update_session)
        if metrics_provider is None:
            for card in (self._cpu_card, self._ram_card, self._disk_card):
                card.update_value(None, "no metrics source")
        else:
            threading.Thread(target=self._metrics_loop, daemon=True).start()

        self._write_log("AuraShell OS interface ready.", "system")

    # -- layout ------------------------------------------------------------
    def _build_layout(self) -> None:
        root = self._root
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(root, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(14, 6))
        brand = ctk.CTkFrame(header, fg_color="transparent")
        brand.pack(side="left")
        ctk.CTkLabel(
            brand, text="AURASHELL", font=self._fonts["brand"], text_color=TEXT
        ).pack(side="left")
        ctk.CTkLabel(
            brand, text=" OS", font=self._fonts["brand"], text_color=CYAN
        ).pack(side="left")
        ctk.CTkLabel(
            header,
            text="voice-controlled desktop layer",
            font=self._fonts["small"],
            text_color=MUTED,
        ).pack(side="left", padx=(14, 0), pady=(6, 0))

        body = ctk.CTkFrame(root, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=22, pady=(0, 8))
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0, minsize=280)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(2, weight=1)

        self._indicator = StateIndicator(left, self._fonts)
        self._indicator.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self._last_card = LastCommandCard(left, self._fonts)
        self._last_card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self._build_main_panel(left).grid(row=2, column=0, sticky="nsew")

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        self._cpu_card = MetricCard(right, "CPU", self._fonts)
        self._cpu_card.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self._ram_card = MetricCard(right, "RAM", self._fonts)
        self._ram_card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self._disk_card = MetricCard(right, "DISK", self._fonts)
        self._disk_card.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        self._build_session_card(right).grid(row=3, column=0, sticky="ew")

        status = ctk.CTkFrame(
            root, fg_color=PANEL_ALT, corner_radius=0, border_width=0, height=32
        )
        status.grid(row=2, column=0, sticky="ew")
        self._status_dot = ctk.CTkLabel(
            status, text="●", font=self._fonts["small"], text_color=GREEN, width=16
        )
        self._status_dot.pack(side="left", padx=(18, 4), pady=6)
        self._status_text = ctk.CTkLabel(
            status, text="Ready", font=self._fonts["small"], text_color=TEXT, anchor="w"
        )
        self._status_text.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            status,
            text="Space = microphone",
            font=self._fonts["small"],
            text_color=MUTED,
        ).pack(side="right", padx=18)

    def _build_main_panel(self, master: tk.Misc) -> ctk.CTkFrame:
        """Log / Results panel with the command input row underneath."""
        panel = ctk.CTkFrame(
            master, fg_color=PANEL, corner_radius=14, border_width=1, border_color=BORDER
        )
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(panel, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 6))
        self._tabs = ctk.CTkSegmentedButton(
            top,
            values=["Log", "Results"],
            command=self._switch_panel,
            font=self._fonts["small_bold"],
            fg_color=PANEL_ALT,
            selected_color=BORDER,
            selected_hover_color=BORDER,
            unselected_color=PANEL_ALT,
            unselected_hover_color=BORDER,
            text_color=TEXT,
            height=26,
        )
        self._tabs.set("Log")
        self._tabs.pack(side="left")

        self._log_controls = ctk.CTkFrame(top, fg_color="transparent")
        self._log_controls.pack(side="right")
        self._persist_var = ctk.BooleanVar(value=False)
        self._persist_switch = ctk.CTkSwitch(
            self._log_controls,
            text="Save to disk",
            variable=self._persist_var,
            command=self._toggle_persist,
            font=self._fonts["small"],
            text_color=MUTED,
            progress_color=CYAN,
            switch_width=34,
            switch_height=16,
        )
        self._persist_switch.pack(side="left", padx=(0, 10))
        for text, command in (("Export", self._export_dialog), ("Clear", self._clear_log)):
            ctk.CTkButton(
                self._log_controls,
                text=text,
                width=62,
                height=24,
                font=self._fonts["small"],
                fg_color="transparent",
                hover_color=BORDER,
                border_width=1,
                border_color=BORDER,
                text_color=MUTED,
                command=command,
            ).pack(side="left", padx=(0, 6))

        self._log = ctk.CTkTextbox(
            panel,
            font=self._fonts["mono"],
            fg_color=PANEL_ALT,
            text_color=TEXT,
            border_width=0,
            corner_radius=10,
            wrap="word",
            state="disabled",
        )
        self._log.grid(row=1, column=0, sticky="nsew", padx=14)
        self._log.tag_config("ts", foreground=_mix(MUTED, BG, 0.25))
        for level, (_badge, badge_color, msg_color) in LEVELS.items():
            self._log.tag_config(f"{level}_badge", foreground=badge_color)
            self._log.tag_config(f"{level}_msg", foreground=msg_color)

        self._results = self._build_results_view(panel)
        self._results.grid(row=1, column=0, sticky="nsew", padx=14)
        self._results.grid_remove()

        # Input area: small Arabic preview line + [mic] [entry] [send].
        self._preview = ctk.CTkLabel(
            panel, text="", font=self._fonts["body"], text_color=MUTED, anchor="e"
        )
        self._preview.grid(row=2, column=0, sticky="ew", padx=16, pady=(8, 0))
        self._preview.grid_remove()

        row = ctk.CTkFrame(panel, fg_color="transparent")
        row.grid(row=3, column=0, sticky="ew", padx=14, pady=(6, 12))
        row.grid_columnconfigure(1, weight=1)
        self._mic_btn = ctk.CTkButton(
            row,
            text="◉  Speak",
            width=120,
            height=36,
            font=self._fonts["small_bold"],
            fg_color=VIOLET,
            hover_color=_mix(VIOLET, "#FFFFFF", 0.2),
            text_color="#12041F",
            text_color_disabled=MUTED,
            command=self._on_mic_pressed,
        )
        self._mic_btn.grid(row=0, column=0, padx=(0, 10))
        self._entry = ctk.CTkEntry(
            row,
            height=36,
            font=self._fonts["body"],
            fg_color=PANEL_ALT,
            border_color=BORDER,
            text_color=TEXT,
            placeholder_text="Type a command, e.g. open chrome / system status",
        )
        self._entry.grid(row=0, column=1, sticky="ew", padx=(0, 10))
        self._entry.bind("<Return>", lambda _e: self._submit_text())
        self._entry.bind("<KeyRelease>", lambda _e: self._update_preview())
        ctk.CTkButton(
            row,
            text="Send",
            width=84,
            height=36,
            font=self._fonts["small_bold"],
            fg_color=CYAN,
            hover_color=_mix(CYAN, "#FFFFFF", 0.25),
            text_color="#04131A",
            command=self._submit_text,
        ).grid(row=0, column=2)
        return panel

    def _build_results_view(self, master: tk.Misc) -> ctk.CTkFrame:
        """Scrollable table (ttk.Treeview styled to match the dark theme)."""
        frame = ctk.CTkFrame(master, fg_color=PANEL_ALT, corner_radius=10)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(frame, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(8, 4))
        self._table_title = ctk.CTkLabel(
            head,
            text="No results yet",
            font=self._fonts["small_bold"],
            text_color=TEXT,
            anchor="w",
        )
        self._table_title.pack(side="left")
        self._table_count = ctk.CTkLabel(
            head, text="", font=self._fonts["small"], text_color=MUTED, anchor="e"
        )
        self._table_count.pack(side="right")

        table_font = (self._font_families["arabic"], 11)
        style = ttk.Style(self._root)
        style.theme_use("clam")
        style.configure(
            "Aura.Treeview",
            background=PANEL_ALT,
            fieldbackground=PANEL_ALT,
            foreground=TEXT,
            borderwidth=0,
            rowheight=26,
            font=table_font,
        )
        style.configure(
            "Aura.Treeview.Heading",
            background=PANEL,
            foreground=MUTED,
            relief="flat",
            borderwidth=0,
            font=(self._font_families["ui"], 10, "bold"),
        )
        style.map(
            "Aura.Treeview",
            background=[("selected", BORDER)],
            foreground=[("selected", TEXT)],
        )
        style.map("Aura.Treeview.Heading", background=[("active", BORDER)])
        # Drop the default light border / focus ring around the tree area.
        style.layout("Aura.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])

        self._tree = ttk.Treeview(
            frame, style="Aura.Treeview", show="headings", selectmode="browse"
        )
        self._tree.tag_configure("odd", background=_mix(PANEL_ALT, "#FFFFFF", 0.035))
        self._tree.grid(row=1, column=0, sticky="nsew", padx=(10, 0), pady=(0, 10))
        scroll = ctk.CTkScrollbar(frame, command=self._tree.yview)
        scroll.grid(row=1, column=1, sticky="ns", padx=(2, 6), pady=(0, 10))
        self._tree.configure(yscrollcommand=scroll.set)
        self._table_font = tkfont.Font(family=table_font[0], size=table_font[1])
        return frame

    def _build_session_card(self, master: tk.Misc) -> ctk.CTkFrame:
        card = ctk.CTkFrame(
            master, fg_color=PANEL, corner_radius=14, border_width=1, border_color=BORDER
        )
        ctk.CTkLabel(
            card, text="SESSION", font=self._fonts["small_bold"], text_color=MUTED, anchor="w"
        ).pack(fill="x", padx=16, pady=(10, 4))
        self._session_label = ctk.CTkLabel(
            card, text="", font=self._fonts["small"], text_color=TEXT, anchor="w", justify="left"
        )
        self._session_label.pack(fill="x", padx=16, pady=(0, 10))
        self._update_session(reschedule=False)
        return card

    # -- scheduling --------------------------------------------------------
    def _schedule(self, key: str, delay_ms: int, callback: Callable[[], None]) -> None:
        if self._closed:
            return
        self._after_ids[key] = self._root.after(delay_ms, callback)

    # -- thread-safe public API -------------------------------------------
    def run(self) -> None:
        """Start the Tk main loop. Call from the thread that created the window."""
        self._root.mainloop()

    @property
    def is_closed(self) -> bool:
        return self._closed

    def show_status(self, message: str, *, level: str = "info") -> None:
        self._queue.put(("status", (_clean_message(message), self._norm_level(level))))

    def set_state(self, state: AuraState | str) -> None:
        state = _as_state(state)
        with self._epoch_lock:
            self._epoch += 1
        self._queue.put(("state", state))

    def set_audio_level(self, level: float) -> None:
        try:
            value = float(level)
        except (TypeError, ValueError):
            return
        if math.isnan(value):
            return
        self._queue.put(("level", _clamp(value)))

    def append_log(self, message: str, level: str = "info") -> None:
        self._queue.put(("log", (_clean_message(message), self._norm_level(level))))

    def log_command(
        self, raw_text: str, intent: str | None = None, target: str | None = None
    ) -> None:
        """Log a recognised command and show it on the "last command" card."""
        self._queue.put(("command", (_clean_message(raw_text), intent, target)))

    def show_command_result(self, ok: bool, reply: str = "") -> None:
        """Finish the "last command" card: SUCCESS / FAILED plus the spoken reply."""
        self._queue.put(("result", (bool(ok), _clean_message(reply))))

    def show_table(
        self,
        title: str,
        rows: Iterable[Mapping[str, object] | list | tuple],
        columns: Iterable[object] | None = None,
    ) -> None:
        """Show tabular results (e.g. LIST_FILES, SHOW_PROCESSES) in the Results tab.

        ``rows``: list of dicts (columns = keys, in first-seen order) or list of
        lists/tuples (pass ``columns`` for the headers). Raises ``TypeError`` on
        bad input; shows at most ``MAX_TABLE_ROWS`` rows.
        """
        headers, data, truncated = _normalize_table(rows, columns)
        self._queue.put(("table", (_clean_message(title), headers, data, truncated)))

    def ask_confirmation(
        self,
        token: object,
        text: str,
        ttl: float = DEFAULT_CONFIRM_TTL,
        *,
        danger: bool | None = None,
    ) -> None:
        """Open the modal confirmation dialog.

        ``on_confirm(token, approved)`` is called (in a worker thread) when the
        user clicks Confirm/Cancel or the countdown reaches zero (approved=False).
        ``danger=None`` makes the dialog red when ``text`` mentions delete/close/
        power actions; pass True/False to force it.
        """
        hash(token)  # tokens are compared with ==, so they must be hashable
        ttl = float(ttl)
        if not math.isfinite(ttl) or not 0 < ttl <= MAX_CONFIRM_TTL:
            raise ValueError(f"ttl must be between 0 and {MAX_CONFIRM_TTL:.0f} seconds")
        text = _clean_message(text)
        if danger is None:
            danger = _is_dangerous(text)
        self._queue.put(("confirm_open", (token, text, ttl, bool(danger))))

    def close_confirmation(self, token: object) -> None:
        """Close the dialog for ``token`` without calling ``on_confirm``.

        Use it when the answer arrived another way (e.g. by voice).
        """
        self._queue.put(("confirm_close", token))

    def close(self) -> None:
        self._queue.put(("close", None))

    def export_log(self, path: str | os.PathLike[str]) -> int:
        """Write the in-memory log to ``path`` (UTF-8). Returns the line count."""
        lines = list(self._log_lines)
        Path(path).write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        return len(lines)

    @staticmethod
    def _norm_level(level: str) -> str:
        return level if level in LEVELS else "info"

    # -- GUI-thread internals ---------------------------------------------
    def _drain_queue(self) -> None:
        if self._closed:
            return
        self._process_queue()
        self._schedule("queue", 40, self._drain_queue)

    def _process_queue(self) -> None:
        """Apply queued events (also called directly by the unit tests)."""
        for _ in range(QUEUE_BATCH):
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            if self._closed:
                break
            try:
                self._dispatch(kind, payload)
            except Exception as exc:  # a bad event must not kill the UI loop
                print(f"[gui] failed to handle {kind!r}: {exc}", file=sys.stderr)

    def _dispatch(self, kind: str, payload: Any) -> None:
        if kind == "state":
            self._state = payload
            self._indicator.set_state(payload)
            self._refresh_mic_button()
        elif kind == "level":
            self._indicator.set_level(payload)
        elif kind == "log":
            self._write_log(*payload)
        elif kind == "status":
            message, level = payload
            self._set_status(message, level)
            self._write_log(message, level)
        elif kind == "command":
            raw, intent, target = payload
            self._write_log(raw, "command")
            if intent:
                self._write_log(f"→ {intent}  ·  target: {target or '-'}", "info")
            self._last_card.set_command(raw, intent, target)
        elif kind == "result":
            ok, reply = payload
            self._last_card.set_result(ok, reply)
            self._write_log(reply or ("Done." if ok else "Failed."), "success" if ok else "error")
        elif kind == "table":
            self._show_table_now(*payload)
        elif kind == "confirm_open":
            self._open_confirmation(*payload)
        elif kind == "confirm_close":
            self._close_confirmation(payload)
        elif kind == "metrics":
            self._apply_metrics(payload)
        elif kind == "mic_done":
            self._mic_active = False
            self._refresh_mic_button()
        elif kind == "close":
            self._on_close()

    # -- status bar / log --------------------------------------------------
    def _set_status(self, message: str, level: str) -> None:
        color = {"info": CYAN, "success": GREEN, "warning": AMBER, "error": RED}.get(
            level, MUTED
        )
        self._status_dot.configure(text_color=color)
        self._status_text.configure(text=_display_text(message))

    def _write_log(self, message: str, level: str = "info") -> None:
        level = self._norm_level(level)
        badge = LEVELS[level][0]
        stamp = datetime.now().strftime("%H:%M:%S")
        box = self._log
        at_bottom = box.yview()[1] >= 0.98
        box.configure(state="normal")
        box.insert("end", stamp + "  ", "ts")
        box.insert("end", f"{badge:<4} ", f"{level}_badge")
        box.insert("end", _display_text(message) + "\n", f"{level}_msg")
        lines = int(box.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            box.delete("1.0", f"{lines - MAX_LOG_LINES + 1}.0")
        box.configure(state="disabled")
        if at_bottom:
            box.see("end")

        plain = f"{stamp}  {badge:<4} {message}"
        self._log_lines.append(plain)
        if level == "command":
            self._command_count += 1
        if self._persist_var.get():
            self._persist_line(plain)

    def _clear_log(self) -> None:
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")
        self._log_lines.clear()

    def _toggle_persist(self) -> None:
        if self._persist_var.get():
            self._write_log(f"Saving the log to disk is ON: {self._log_path}", "warning")
        else:
            self._write_log("Saving the log to disk is OFF.", "system")

    def _persist_line(self, line: str) -> None:
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            new_file = not self._log_path.exists()
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            if new_file:
                os.chmod(self._log_path, 0o600)
        except OSError as exc:
            self._persist_var.set(False)  # stop trying; tell the user
            self._write_log(f"Could not save the log ({exc.strerror or exc}); saving is OFF.", "error")

    def _export_dialog(self) -> None:
        default = f"aurashell_log_{datetime.now():%Y%m%d_%H%M%S}.txt"
        path = filedialog.asksaveasfilename(
            parent=self._root,
            title="Export command log",
            defaultextension=".txt",
            initialfile=default,
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            count = self.export_log(path)
        except OSError as exc:
            self._write_log(f"Export failed: {exc.strerror or exc}", "error")
        else:
            self._write_log(f"Exported {count} log lines to {path}", "success")

    # -- Log / Results tabs ------------------------------------------------
    def _switch_panel(self, name: str) -> None:
        if name == "Results":
            self._log.grid_remove()
            self._log_controls.pack_forget()
            self._results.grid()
        else:
            self._results.grid_remove()
            self._log_controls.pack(side="right")
            self._log.grid()
        self._tabs.set(name)

    def _show_table_now(
        self, title: str, headers: list[str], rows: list[list[str]], truncated: bool
    ) -> None:
        self._table = (title, headers, rows)
        tree = self._tree
        tree.delete(*tree.get_children())
        ids = [f"c{i}" for i in range(len(headers))]
        tree.configure(columns=ids)
        for cid, header in zip(ids, headers):
            index = ids.index(cid)
            sample = [header] + [row[index] for row in rows[:100]]
            width = max(self._table_font.measure(_display_text(text)) for text in sample) + 28
            tree.heading(cid, text=_display_text(header), anchor="w")
            tree.column(cid, width=min(max(width, 70), 420), minwidth=60, stretch=True, anchor="w")
        for i, row in enumerate(rows):
            tree.insert("", "end", values=[_display_text(c) for c in row],
                        tags=("odd",) if i % 2 else ())
        self._table_title.configure(text=_display_text(title) or "Results")
        note = f"{len(rows)} rows" + (f" (first {MAX_TABLE_ROWS} shown)" if truncated else "")
        self._table_count.configure(text=note if rows else "no rows")
        self._write_log(f"Results: {title or 'table'} ({note})", "info")
        self._switch_panel("Results")

    # -- microphone button / keyboard shortcut -----------------------------
    def _refresh_mic_button(self) -> None:
        busy_state = self._state in (AuraState.PROCESSING, AuraState.SPEAKING)
        if busy_state:
            text = "Busy…"
        elif self._mic_active:
            text = "Listening…"
        else:
            text = "◉  Speak"
        disabled = busy_state or self._mic_active
        self._mic_btn.configure(
            text=text,
            state="disabled" if disabled else "normal",
            fg_color=BORDER if disabled else VIOLET,
        )

    def _on_space(self, _event: tk.Event) -> str | None:
        """Space presses the mic button, except while typing or in a dialog."""
        if self._confirm is not None:
            return None
        try:
            focus = self._root.focus_get()
        except (KeyError, tk.TclError):
            focus = None
        if isinstance(focus, tk.Entry):
            return None
        if focus is not None and str(focus.winfo_toplevel()) != str(self._root):
            return None  # another window (e.g. the export file dialog) has focus
        self._on_mic_pressed()
        return "break"

    def _on_mic_pressed(self) -> None:
        # Both guards matter: the flag stops two quick presses before the state
        # event has arrived, the state stops presses while processing/speaking.
        if self._mic_active or self._state in (AuraState.PROCESSING, AuraState.SPEAKING):
            return
        handler = self._on_mic_click
        if handler is None:
            self._write_log("No microphone handler is connected yet.", "warning")
            return
        self._mic_active = True
        self._refresh_mic_button()
        self.set_state(AuraState.LISTENING)
        with self._epoch_lock:
            epoch = self._epoch
        self._call_worker("Microphone handler", handler, (), epoch=epoch, done="mic_done")

    # -- typed commands ----------------------------------------------------
    def _update_preview(self) -> None:
        text = self._entry.get()
        if _ARABIC.search(text):
            self._preview.configure(text=_display_text(text))
            self._preview.grid()
        else:
            self._preview.grid_remove()

    def _submit_text(self) -> None:
        text = self._entry.get().strip()[:MAX_INPUT_CHARS]
        if not text:
            return
        self._entry.delete(0, "end")
        self._update_preview()
        self._write_log(text, "command")
        handler = self._on_text_command
        if handler is None:
            self._write_log("No command handler is connected yet.", "warning")
            return
        self.set_state(AuraState.PROCESSING)
        with self._epoch_lock:
            epoch = self._epoch
        self._call_worker("Command handler", handler, (text,), epoch=epoch)

    def _call_worker(
        self,
        label: str,
        func: Callable[..., object],
        args: tuple,
        *,
        epoch: int | None = None,
        done: str | None = None,
    ) -> None:
        """Run a callback in a daemon thread (never touch Tk there).

        If ``epoch`` is given and the callback did not change the state itself,
        the state returns to IDLE afterwards so the UI never gets stuck.
        """

        def runner() -> None:
            try:
                func(*args)
            except Exception as exc:
                self.append_log(f"{label} failed: {exc}", "error")
                if epoch is not None:
                    self.set_state(AuraState.IDLE)
            else:
                if epoch is not None:
                    with self._epoch_lock:
                        unchanged = self._epoch == epoch
                    if unchanged:
                        self.set_state(AuraState.IDLE)
            finally:
                if done:
                    self._queue.put((done, None))

        threading.Thread(target=runner, daemon=True).start()

    # -- confirmation dialog -----------------------------------------------
    def _open_confirmation(self, token: object, text: str, ttl: float, danger: bool) -> None:
        if self._confirm is not None:  # a newer request replaces (and denies) the old one
            old = self._confirm
            old.dismiss()
            self._confirm = None
            self._write_log(f"Confirmation replaced by a newer request: {old.text}", "warning")
            self._notify_confirm(old.token, False)
        self._confirm = ConfirmationDialog(
            self._root, token, text, ttl, danger, self._fonts, self._confirmation_resolved
        )
        self._write_log(f"Confirmation required: {text} (auto-cancel in {ttl:.0f}s)", "warning")
        self._set_status("Waiting for your confirmation…", "warning")

    def _confirmation_resolved(self, token: object, approved: bool, reason: str) -> None:
        """Called by the dialog (GUI thread) when the user/timeout answers."""
        if self._confirm is not None and self._confirm.token == token:
            self._confirm = None
        if approved:
            self._write_log("Confirmed by the user.", "success")
        else:
            self._write_log(f"Not confirmed ({reason}).", "warning")
        self._set_status("Confirmed" if approved else "Cancelled", "success" if approved else "warning")
        self._notify_confirm(token, approved)

    def _notify_confirm(self, token: object, approved: bool) -> None:
        handler = self._on_confirm
        if handler is None:
            self._write_log("No confirmation handler is connected; the answer was dropped.", "warning")
            return
        self._call_worker("Confirmation handler", handler, (token, approved))

    def _close_confirmation(self, token: object) -> None:
        if self._confirm is not None and self._confirm.token == token:
            self._confirm.dismiss()
            self._confirm = None
            self._write_log("Confirmation closed (answered another way).", "info")

    # -- metrics -----------------------------------------------------------
    def _metrics_loop(self) -> None:
        """Background thread: call the provider, hand results to the GUI thread."""
        provider = self._metrics_provider
        while not self._stop_metrics.is_set():
            try:
                payload: object = dict(provider())  # type: ignore[misc]
            except Exception as exc:
                payload = exc
            self._queue.put(("metrics", payload))
            if self._stop_metrics.wait(METRICS_INTERVAL):
                break

    def _apply_metrics(self, data: object) -> None:
        if isinstance(data, Exception):
            reason = str(data) or type(data).__name__
            for card in (self._cpu_card, self._ram_card, self._disk_card):
                card.update_value(None, "metrics unavailable")
            if reason != self._last_metrics_error:  # log each distinct error once
                self._last_metrics_error = reason
                self._write_log(f"Metrics unavailable: {reason}", "warning")
            return
        self._last_metrics_error = ""
        gb = 1024**3
        d: Mapping[str, Any] = data  # type: ignore[assignment]

        uptime = _num(d.get("uptime_seconds"))
        cpu_detail = ""
        if uptime is not None and uptime >= 0:
            hours, minutes = divmod(int(uptime) // 60, 60)
            cpu_detail = f"System uptime {hours}h {minutes:02d}m"
        self._cpu_card.update_value(_num(d.get("cpu_percent")), cpu_detail)

        total, avail = _num(d.get("ram_total_bytes")), _num(d.get("ram_available_bytes"))
        detail = ""
        if total:
            detail = f"{total / gb:.1f} GB total"
            if avail is not None:
                detail += f" · {avail / gb:.1f} GB free"
        self._ram_card.update_value(_num(d.get("ram_percent")), detail)

        disk_total, disk_free = _num(d.get("disk_total_bytes")), _num(d.get("disk_free_bytes"))
        detail = ""
        if disk_total:
            detail = f"{disk_total / gb:.0f} GB total"
            if disk_free is not None:
                detail += f" · {disk_free / gb:.0f} GB free"
        elif "disk_error" in d:
            detail = "disk unavailable"
        self._disk_card.update_value(_num(d.get("disk_percent")), detail)

    def _update_session(self, reschedule: bool = True) -> None:
        if self._closed:
            return
        elapsed = int(time.monotonic() - self._started)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        self._session_label.configure(
            text=f"Uptime     {h:02d}:{m:02d}:{s:02d}\nCommands   {self._command_count}"
        )
        if reschedule:
            self._schedule("session", 1000, self._update_session)

    def _on_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_metrics.set()
        if self._confirm is not None:
            self._confirm.dismiss()
            self._confirm = None
        for after_id in self._after_ids.values():
            try:
                self._root.after_cancel(after_id)
            except tk.TclError:
                pass
        self._root.destroy()


# --------------------------------------------------------------------------
# Microphone level (used by the demo's --mic option)
# --------------------------------------------------------------------------
def _rms_level(frames: bytes, full_scale: float = 4000.0) -> float:
    """Loudness 0..1 of little-endian 16-bit mono audio (RMS / ``full_scale``)."""
    from array import array

    samples = array("h")
    samples.frombytes(frames[: len(frames) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return _clamp(rms / full_scale)


def _mic_meter(window: "AuraShellWindow", stop: threading.Event) -> None:
    """Feed the real microphone level to ``window.set_audio_level`` (demo only).

    Needs ``pyaudio``. The real STT module should do the same through its level
    callback; this just lets you see the visualizer react to your voice.
    """
    try:
        import pyaudio
    except ImportError:
        window.append_log("--mic needs pyaudio (sudo apt install python3-pyaudio).", "warning")
        return
    pa = pyaudio.PyAudio()
    try:
        stream = pa.open(
            format=pyaudio.paInt16, channels=1, rate=16000, input=True, frames_per_buffer=1024
        )
    except Exception as exc:  # no microphone / device busy
        window.append_log(f"Microphone unavailable: {exc}", "error")
        pa.terminate()
        return
    window.append_log("Real microphone level is ON (demo).", "system")
    try:
        while not stop.is_set() and not window.is_closed:
            window.set_audio_level(_rms_level(stream.read(1024, exception_on_overflow=False)))
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


# --------------------------------------------------------------------------
# Standalone demo: python -m gui.main_window
#
#   (no flag)  press the Speak button (or Space) to play the next scripted
#              command: mic -> speech -> command -> result -> reply -> confirmation
#   --auto     play all scripted commands by itself
#   --mic      also feed the REAL microphone level to the visualizer
#
# The demo executes nothing: replies and results are scripted.
# --------------------------------------------------------------------------
_FILES = [
    {"name": "report.pdf", "type": "file", "size": 482_113},
    {"name": "slides.pptx", "type": "file", "size": 6_281_977},
    {"name": "notes.txt", "type": "file", "size": 2_048},
    {"name": "projects", "type": "folder", "size": 0},
]
_PROCESSES = [
    {"name": "firefox", "pid": 2210, "cpu": 14.2, "ram": 8.1},
    {"name": "code", "pid": 3012, "cpu": 6.4, "ram": 5.7},
    {"name": "python3", "pid": 4120, "cpu": 2.1, "ram": 1.3},
]
# phrase, intent, target, ok, spoken reply, table, confirmation text
_SCENARIOS: list[tuple[str, str, str | None, bool, str, tuple | None, str | None]] = [
    ("open chrome", "OPEN_APP", "chrome", True, "Google Chrome is open.", None, None),
    ("افتح فيس كود", "OPEN_APP", "vscode", True, "Visual Studio Code is open.", None, None),
    ("list files", "LIST_FILES", "Documents", True, "I found 4 items in Documents.",
     ("Documents", _FILES), None),
    ("show processes", "SHOW_PROCESSES", None, True, "Here are the top processes.",
     ("Top processes", _PROCESSES), None),
    ("open the calculator", "OPEN_APP", "calculator", False,
     "The calculator is not installed.", None, None),
    ("delete report.pdf", "DELETE_FILE", "report.pdf", True, "The file report.pdf was deleted.",
     None, "Delete the file report.pdf?"),
]


class _Demo:
    """Scripted session; its methods are the window's worker-thread callbacks."""

    def __init__(self, use_real_mic: bool = False) -> None:
        self.window: AuraShellWindow | None = None
        self.use_real_mic = use_real_mic
        self._step = 0
        self._pending: tuple | None = None

    def _listen(self, seconds: float) -> None:
        window = self.window
        end = time.monotonic() + seconds
        while time.monotonic() < end and not window.is_closed:
            if not self.use_real_mic:  # otherwise the mic thread feeds real levels
                window.set_audio_level(0.35 + 0.55 * abs(math.sin(time.monotonic() * 5.3)))
            time.sleep(0.05)

    def _finish(self, ok: bool, reply: str, table: tuple | None) -> None:
        window = self.window
        if table:
            window.show_table(*table)
        window.show_command_result(ok, reply)
        window.set_state(AuraState.SPEAKING)
        time.sleep(2.2)
        window.set_state(AuraState.IDLE)

    def on_mic(self) -> None:
        window = self.window
        phrase, intent, target, ok, reply, table, confirm = _SCENARIOS[
            self._step % len(_SCENARIOS)
        ]
        self._step += 1
        window.set_state(AuraState.LISTENING)  # microphone pressed
        self._listen(2.6)                      # the user speaks
        window.set_state(AuraState.PROCESSING)
        window.log_command(phrase, intent, target)  # the command
        time.sleep(1.2)
        if confirm:                            # risky action: wait for the user
            self._pending = (ok, reply, table)
            window.ask_confirmation("demo-token", confirm, ttl=20)
            return
        self._finish(ok, reply, table)         # result + spoken reply

    def on_confirm(self, token: object, approved: bool) -> None:
        window = self.window
        ok, reply, table = self._pending or (True, "Done.", None)
        self._pending = None
        if approved:
            self._finish(ok, reply, table)
        else:
            self._finish(False, "Okay, I cancelled it.", None)

    def on_text(self, text: str) -> None:
        window = self.window
        window.set_state(AuraState.PROCESSING)
        window.log_command(text)
        time.sleep(1.0)
        self._finish(True, f"(demo) I received: {text}", None)

    def run_all(self) -> None:
        """--auto: play every scenario, approving the confirmation by itself."""
        window = self.window
        time.sleep(1.5)
        for _ in _SCENARIOS:
            if window.is_closed:
                return
            self.on_mic()
            if self._pending is not None:
                time.sleep(2.5)
                window.close_confirmation("demo-token")
                self.on_confirm("demo-token", True)
            time.sleep(0.8)


def main() -> int:
    args = set(sys.argv[1:])
    demo = _Demo(use_real_mic="--mic" in args)

    provider = None
    try:
        from core.system_info import SystemInfo

        provider = make_metrics_provider(SystemInfo())
    except Exception:  # core/psutil missing: the cards show N/A
        pass

    window = AuraShellWindow(
        on_text_command=demo.on_text,
        on_mic_click=demo.on_mic,
        on_confirm=demo.on_confirm,
        metrics_provider=provider,
    )
    demo.window = window
    window.show_status("Demo: press Speak (or Space) to run the next command", level="info")
    if "--mic" in args:
        threading.Thread(target=_mic_meter, args=(window, threading.Event()), daemon=True).start()
    if "--auto" in args:
        threading.Thread(target=demo.run_all, daemon=True).start()
    window.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
