"""AuraShell OS main window: dark cyberpunk GUI built with CustomTkinter.

What the window shows
---------------------
* A pulsing orb + audio-wave visualizer that reflects the assistant state
  (IDLE / LISTENING / PROCESSING / SPEAKING).
* A live command log (timestamped, colour-coded by level).
* Small CPU and RAM cards (value, progress bar, sparkline) refreshed with
  ``psutil`` once per second, plus a tiny session card.
* A text box so commands can also be typed (useful without a microphone).

Public API (matches ``PROJECT_PLAN.md`` section 4, plus a few helpers)
----------------------------------------------------------------------
``AuraShellWindow(on_text_command=None)``
    ``on_text_command(text: str)`` is called in a *worker thread* when the user
    submits a typed command, so slow work never freezes the UI.
``run()``                      start the Tk main loop (blocks until closed).
``show_status(message, level)``  status bar + log line.
``set_state(state)``           ``AuraState`` or "idle"/"listening"/...
``set_audio_level(level)``     0.0-1.0 microphone level (drives the wave).
``append_log(message, level)`` add a line to the live log.
``log_command(raw_text, intent, target)``  log a recognised command + intent.
``close()``                    close the window.

Every public method except ``run()`` is thread-safe: calls are put on a queue
and applied on the GUI thread, so STT / TTS / executor threads can call them
directly (``PROJECT_PLAN.md``: "All Tk updates run on the GUI thread").

Demo without the rest of the system::

    python -m gui.main_window
"""

from __future__ import annotations

import math
import queue
import re
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from collections import deque
from enum import Enum
from tkinter import font as tkfont
from typing import Callable

import customtkinter as ctk

try:  # CPU / RAM cards degrade to "N/A" if psutil is missing.
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

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
QUEUE_BATCH = 200


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
# Small helpers
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
    """Shape/reorder Arabic so Tk (no native bidi on Linux) shows it correctly."""
    if arabic_reshaper is None or get_display is None or not _ARABIC.search(text):
        return text
    try:
        return get_display(arabic_reshaper.reshape(text))
    except Exception:  # never let cosmetics break logging
        return text


def _clean_message(message: object) -> str:
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
        header.pack(fill="x", padx=18, pady=(14, 0))

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

        self._wave = tk.Canvas(self, height=112, bg=PANEL, highlightthickness=0)
        self._wave.pack(fill="x", padx=18, pady=(6, 16))
        self._wave.bind("<Configure>", self._on_resize)

        self._tick()

    # -- public ------------------------------------------------------------
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
# CPU / RAM card
# --------------------------------------------------------------------------
class MetricCard(ctk.CTkFrame):
    """Title, big percentage, progress bar and a 60-sample sparkline."""

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
        top.pack(fill="x", padx=16, pady=(12, 0))
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
        self._bar.pack(fill="x", padx=16, pady=(6, 4))

        self._detail = ctk.CTkLabel(
            self, text=" ", font=fonts["small"], text_color=MUTED, anchor="w"
        )
        self._detail.pack(fill="x", padx=16)

        self._spark = tk.Canvas(self, height=36, bg=PANEL, highlightthickness=0)
        self._spark.pack(fill="x", padx=16, pady=(4, 12))
        self._spark.bind("<Configure>", lambda _e: self._draw_spark())

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
            self._detail.configure(text=detail or "unavailable")
            return
        percent = _clamp(percent, 0.0, 100.0)
        self._color = self._color_for(percent)
        self._history.append(percent)
        self._value.configure(text=f"{percent:.0f}%", text_color=self._color)
        self._bar.configure(progress_color=self._color)
        self._bar.set(percent / 100)
        self._detail.configure(text=detail or " ")
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
# Main window
# --------------------------------------------------------------------------
class AuraShellWindow:
    """The AuraShell OS main window (see module docstring for the API)."""

    def __init__(self, on_text_command: Callable[[str], object] | None = None) -> None:
        self._on_text_command = on_text_command
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._epoch = 0
        self._epoch_lock = threading.Lock()
        self._closed = False
        self._after_ids: dict[str, str] = {}
        self._started = time.monotonic()
        self._command_count = 0

        ctk.set_appearance_mode("dark")
        self._root = ctk.CTk()
        self._root.title("AuraShell OS")
        self._root.geometry("1000x660")
        self._root.minsize(860, 580)
        self._root.configure(fg_color=BG)

        ui = _pick_font(
            self._root, ("Ubuntu", "Noto Sans", "DejaVu Sans", "Helvetica"), "TkDefaultFont"
        )
        mono = _pick_font(
            self._root,
            ("JetBrains Mono", "Ubuntu Mono", "DejaVu Sans Mono", "Courier New"),
            "TkFixedFont",
        )
        self._fonts = {
            "brand": ctk.CTkFont(family=ui, size=22, weight="bold"),
            "title": ctk.CTkFont(family=ui, size=24, weight="bold"),
            "metric": ctk.CTkFont(family=ui, size=26, weight="bold"),
            "small": ctk.CTkFont(family=ui, size=12),
            "small_bold": ctk.CTkFont(family=ui, size=12, weight="bold"),
            "body": ctk.CTkFont(family=ui, size=13),
            "mono": ctk.CTkFont(family=mono, size=13),
        }

        self._build_layout()
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        if psutil is not None:
            psutil.cpu_percent(interval=None)  # first call always returns 0.0
        self._schedule("queue", 50, self._drain_queue)
        self._schedule("metrics", 600, self._poll_metrics)
        self._schedule("session", 1000, self._update_session)

        self._write_log("AuraShell OS interface ready.", "system")

    # -- layout ------------------------------------------------------------
    def _build_layout(self) -> None:
        root = self._root
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(root, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(16, 8))
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
        body.grid_columnconfigure(1, weight=0, minsize=270)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(1, weight=1)

        self._indicator = StateIndicator(left, self._fonts)
        self._indicator.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self._build_log_panel(left).grid(row=1, column=0, sticky="nsew")

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        self._cpu_card = MetricCard(right, "CPU", self._fonts)
        self._cpu_card.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self._ram_card = MetricCard(right, "RAM", self._fonts)
        self._ram_card.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        self._build_session_card(right).grid(row=2, column=0, sticky="ew")

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

    def _build_log_panel(self, master: tk.Misc) -> ctk.CTkFrame:
        panel = ctk.CTkFrame(
            master, fg_color=PANEL, corner_radius=14, border_width=1, border_color=BORDER
        )
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(panel, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 6))
        ctk.CTkLabel(
            top, text="LIVE LOG", font=self._fonts["small_bold"], text_color=MUTED
        ).pack(side="left")
        ctk.CTkButton(
            top,
            text="Clear",
            width=56,
            height=24,
            font=self._fonts["small"],
            fg_color="transparent",
            hover_color=BORDER,
            border_width=1,
            border_color=BORDER,
            text_color=MUTED,
            command=self._clear_log,
        ).pack(side="right")

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

        row = ctk.CTkFrame(panel, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew", padx=14, pady=12)
        row.grid_columnconfigure(0, weight=1)
        self._entry = ctk.CTkEntry(
            row,
            height=36,
            font=self._fonts["body"],
            fg_color=PANEL_ALT,
            border_color=BORDER,
            text_color=TEXT,
            placeholder_text="Type a command, e.g. open chrome / system status",
        )
        self._entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self._entry.bind("<Return>", lambda _e: self._submit_text())
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
        ).grid(row=0, column=1)
        return panel

    def _build_session_card(self, master: tk.Misc) -> ctk.CTkFrame:
        card = ctk.CTkFrame(
            master, fg_color=PANEL, corner_radius=14, border_width=1, border_color=BORDER
        )
        ctk.CTkLabel(
            card, text="SESSION", font=self._fonts["small_bold"], text_color=MUTED, anchor="w"
        ).pack(fill="x", padx=16, pady=(12, 4))
        self._session_label = ctk.CTkLabel(
            card, text="", font=self._fonts["body"], text_color=TEXT, anchor="w", justify="left"
        )
        self._session_label.pack(fill="x", padx=16, pady=(0, 12))
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
        """Log a recognised command and (optionally) how it was classified."""
        self._queue.put(("command", (_clean_message(raw_text), intent, target)))

    def close(self) -> None:
        self._queue.put(("close", None))

    @staticmethod
    def _norm_level(level: str) -> str:
        return level if level in LEVELS else "info"

    # -- GUI-thread internals ---------------------------------------------
    def _drain_queue(self) -> None:
        if self._closed:
            return
        for _ in range(QUEUE_BATCH):
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._dispatch(kind, payload)
            except Exception as exc:  # a bad event must not kill the UI loop
                print(f"[gui] failed to handle {kind!r}: {exc}", file=sys.stderr)
        self._schedule("queue", 40, self._drain_queue)

    def _dispatch(self, kind: str, payload: object) -> None:
        if kind == "state":
            self._indicator.set_state(payload)  # type: ignore[arg-type]
        elif kind == "level":
            self._indicator.set_level(payload)  # type: ignore[arg-type]
        elif kind == "log":
            message, level = payload  # type: ignore[misc]
            self._write_log(message, level)
        elif kind == "status":
            message, level = payload  # type: ignore[misc]
            self._set_status(message, level)
            self._write_log(message, level)
        elif kind == "command":
            raw, intent, target = payload  # type: ignore[misc]
            self._write_log(raw, "command")
            if intent:
                self._write_log(f"→ {intent}  ·  target: {target or '-'}", "info")
        elif kind == "close":
            self._on_close()

    def _set_status(self, message: str, level: str) -> None:
        color = {"info": CYAN, "success": GREEN, "warning": AMBER, "error": RED}.get(
            level, MUTED
        )
        self._status_dot.configure(text_color=color)
        self._status_text.configure(text=_display_text(message))

    def _write_log(self, message: str, level: str = "info") -> None:
        level = self._norm_level(level)
        badge = LEVELS[level][0]
        box = self._log
        at_bottom = box.yview()[1] >= 0.98
        box.configure(state="normal")
        box.insert("end", datetime.now().strftime("%H:%M:%S") + "  ", "ts")
        box.insert("end", f"{badge:<4} ", f"{level}_badge")
        box.insert("end", _display_text(message) + "\n", f"{level}_msg")
        lines = int(box.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            box.delete("1.0", f"{lines - MAX_LOG_LINES + 1}.0")
        box.configure(state="disabled")
        if at_bottom:
            box.see("end")
        if level == "command":
            self._command_count += 1

    def _clear_log(self) -> None:
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")

    def _poll_metrics(self) -> None:
        if self._closed:
            return
        if psutil is None:
            self._cpu_card.update_value(None, "psutil not installed")
            self._ram_card.update_value(None, "psutil not installed")
        else:
            try:
                cpu = psutil.cpu_percent(interval=None)
                cores = psutil.cpu_count(logical=True)
                self._cpu_card.update_value(cpu, f"{cores} logical cores" if cores else "")
                mem = psutil.virtual_memory()
                gb = 1024**3
                used = (mem.total - mem.available) / gb
                self._ram_card.update_value(
                    (mem.total - mem.available) / mem.total * 100,
                    f"{used:.1f} / {mem.total / gb:.1f} GB",
                )
            except Exception as exc:  # metrics are best-effort, show it honestly
                self._cpu_card.update_value(None, "read error")
                self._ram_card.update_value(None, str(exc)[:40])
        self._schedule("metrics", 1000, self._poll_metrics)

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

    def _submit_text(self) -> None:
        text = self._entry.get().strip()[:MAX_INPUT_CHARS]
        if not text:
            return
        self._entry.delete(0, "end")
        self._write_log(text, "command")
        handler = self._on_text_command
        if handler is None:
            self._write_log("No command handler is connected yet.", "warning")
            return
        self.set_state(AuraState.PROCESSING)
        with self._epoch_lock:
            epoch = self._epoch
        threading.Thread(
            target=self._run_handler, args=(handler, text, epoch), daemon=True
        ).start()

    def _run_handler(
        self, handler: Callable[[str], object], text: str, epoch: int
    ) -> None:
        """Worker thread: never touch Tk here, only the thread-safe API."""
        try:
            handler(text)
        except Exception as exc:
            self.append_log(f"Command handler failed: {exc}", "error")
            self.set_state(AuraState.IDLE)
            return
        with self._epoch_lock:
            unchanged = self._epoch == epoch
        if unchanged:  # handler never moved the state itself -> don't get stuck
            self.set_state(AuraState.IDLE)

    def _on_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for after_id in self._after_ids.values():
            try:
                self._root.after_cancel(after_id)
            except tk.TclError:
                pass
        self._root.destroy()


# --------------------------------------------------------------------------
# Standalone demo: python -m gui.main_window
# --------------------------------------------------------------------------
def _demo(window: AuraShellWindow) -> None:
    """Fake voice session on a background thread (uses the real parser if present)."""
    try:
        from core.intent_parser import IntentParser

        parser = IntentParser()
    except Exception:  # demo still works without the core package
        parser = None

    phrases = ["open chrome", "system status", "ram usage", "close vscode", "what is AI?"]
    time.sleep(1.0)
    window.show_status("Demo mode: simulated voice session", level="info")
    for phrase in phrases:
        if window.is_closed:
            return
        window.set_state(AuraState.LISTENING)
        end = time.monotonic() + 2.6
        while time.monotonic() < end and not window.is_closed:
            window.set_audio_level(0.35 + 0.55 * abs(math.sin(time.monotonic() * 5.3)))
            time.sleep(0.05)
        window.set_state(AuraState.PROCESSING)
        if parser is not None:
            parsed = parser.parse(phrase)
            window.log_command(phrase, parsed["intent"], parsed["target"])
        else:
            window.log_command(phrase)
        time.sleep(1.6)
        window.set_state(AuraState.SPEAKING)
        window.show_status(f"Done: {phrase}", level="success")
        time.sleep(2.2)
    window.set_state(AuraState.IDLE)
    window.show_status("Demo finished. Type a command below.", level="info")


def main() -> int:
    window = AuraShellWindow(
        on_text_command=lambda text: window.append_log(
            f"(demo) received: {text}", "system"
        )
    )
    if "--no-demo" not in sys.argv:
        threading.Thread(target=_demo, args=(window,), daemon=True).start()
    window.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

