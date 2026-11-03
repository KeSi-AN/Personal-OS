"""Active window + idle observer (DESIGN.md §4.4).

`FocusTracker` is the pure state machine (no OS calls, fully unit-tested); `WindowObserver`
feeds it a sample from the platform probe once per poll interval.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from personal_os.config.schema import IdleObserverConfig, WindowObserverConfig
from personal_os.core.events import Event, Idle, WindowFocus
from personal_os.core.ids import uuid7
from personal_os.core.observer import BaseObserver, ObserverContext
from personal_os.observers.session import SessionState
from personal_os.platform.base import WindowProbe, WindowSample

VERSION = "1.0.0"

_UNREAD_RE = re.compile(r"^\(\d+\+?\)\s*")  # "(3) Inbox"
_DIRTY_RE = re.compile(r"^[●•*]\s*")  # editors' unsaved markers
_SPACE_RE = re.compile(r"\s+")
_LOCK_EXES = {"lockapp.exe", "logonui.exe"}


def normalize_title(title: str) -> str:
    t = _SPACE_RE.sub(" ", title).strip()
    t = _UNREAD_RE.sub("", t)
    t = _DIRTY_RE.sub("", t)
    return t[:512]


Key = tuple[int, int | None, str]


def _key(sample: WindowSample) -> Key:
    return (sample.hwnd, sample.pid, normalize_title(sample.title))


@dataclass
class _Open:
    id: UUID
    start: datetime
    payload: WindowFocus | Idle
    key: Key | None
    last_checkpoint: datetime


@dataclass
class _Candidate:
    sample: WindowSample
    first_seen: datetime
    title_change: bool  # same window, new title (vs. a different window)


@dataclass
class FocusTracker:
    min_focus_s: float = 2.0
    title_stable_s: float = 3.0
    idle_threshold_s: float = 300.0
    checkpoint_s: float = 60.0
    sleep_gap_s: float = 30.0
    version: str = VERSION
    focus: _Open | None = None
    idle: _Open | None = None
    candidate: _Candidate | None = None
    last_tick: datetime | None = None
    _out: list[Event] = field(default_factory=list)
    # Recent closed focus intervals (exe, title, start, end), in memory only, never stored as
    # such: lets other observers ask "was this window in front, and for how long?".
    history: deque[tuple[str, str, datetime, datetime]] = field(
        default_factory=lambda: deque(maxlen=5000)
    )

    def tick(
        self, now: datetime, sample: WindowSample | None, idle_s: float, locked: bool = False
    ) -> list[Event]:
        """Advance to `now` (timezone-aware, local) and return the events to emit."""
        self._out = []
        if self.last_tick is not None and (now - self.last_tick).total_seconds() > self.sleep_gap_s:
            self._suspended(self.last_tick, now)
        self.last_tick = now
        if sample is not None and sample.exe and sample.exe.lower() in _LOCK_EXES:
            locked = True

        if locked or idle_s >= self.idle_threshold_s:
            self._away(now, idle_s, locked)
        elif self.idle is not None:
            resumed = max(now - timedelta(seconds=idle_s), self.idle.start)
            self._close(self.idle, resumed)
            self.idle = None
            if sample is not None:
                self._open_focus(sample, resumed)
        elif sample is not None:
            self._active(now, sample)
        self._checkpoint(now)
        return self._out

    def close_all(self, at: datetime) -> list[Event]:
        """Shutdown/pause: close whatever is open at `at`."""
        self._out = []
        if self.focus:
            self._close(self.focus, at)
        if self.idle:
            self._close(self.idle, at)
        self.focus = self.idle = self.candidate = None
        return self._out

    def reset(self) -> None:
        """Forget: drop open intervals without emitting; the next tick starts fresh."""
        self.focus = self.idle = self.candidate = None

    # --- states ----------------------------------------------------------------------------

    def _active(self, now: datetime, sample: WindowSample) -> None:
        key = _key(sample)
        if self.focus is None:
            self._open_focus(sample, now)
            return
        if key == self.focus.key:
            if self.candidate is not None and not self.candidate.title_change:
                self._bump_transient()
            self.candidate = None
            return
        cand = self.candidate
        if cand is None or _key(cand.sample) != key:
            if cand is not None and not cand.title_change:
                self._bump_transient()
            same_window = self.focus.key is not None and self.focus.key[:2] == key[:2]
            cand = self.candidate = _Candidate(sample, now, same_window)
        else:
            cand.sample = sample
        need = self.title_stable_s if cand.title_change else self.min_focus_s
        if (now - cand.first_seen).total_seconds() >= need:
            switch_at = cand.first_seen
            self._close(self.focus, switch_at)
            self._open_focus(cand.sample, switch_at)
            self.candidate = None

    def _away(self, now: datetime, idle_s: float, locked: bool) -> None:
        reason = "locked" if locked else "no_input"
        if self.idle is None:
            # Back-date to the last input, but never before the focus interval it ends.
            since = now - timedelta(seconds=idle_s)
            if self.focus is not None:
                since = max(since, self.focus.start)
                self._close(self.focus, since)
                self.focus = None
            self.candidate = None
            payload = Idle(threshold_s=int(self.idle_threshold_s), reason=reason)
            self.idle = _Open(uuid7(since), since, payload, None, since)
            self._emit_open(self.idle, now)
        elif (
            locked and isinstance(self.idle.payload, Idle) and self.idle.payload.reason != "locked"
        ):
            self.idle.payload = self.idle.payload.model_copy(update={"reason": "locked"})

    def _suspended(self, last: datetime, now: datetime) -> None:
        """A gap between ticks means the machine slept: close at the last tick, record the gap."""
        if self.focus:
            self._close(self.focus, last)
        if self.idle:
            self._close(self.idle, last)
        self.focus = self.idle = self.candidate = None
        gap = Idle(threshold_s=int(self.idle_threshold_s), reason="asleep")
        self._out.append(Event.interval(gap, last, now, observer_version=self.version))

    # --- helpers ---------------------------------------------------------------------------

    def _open_focus(self, sample: WindowSample, at: datetime) -> None:
        payload = WindowFocus(
            app=sample.app,
            exe=sample.exe,
            exe_path=sample.exe_path,
            pid=sample.pid,
            title=normalize_title(sample.title),
            window_class=sample.window_class,
            cwd=sample.cwd,
            is_uwp=sample.is_uwp,
            elevated_unreadable=sample.elevated_unreadable,
        )
        self.focus = _Open(uuid7(at), at, payload, _key(sample), at)
        self._emit_open(self.focus, at)

    def _bump_transient(self) -> None:
        if self.focus is not None and isinstance(self.focus.payload, WindowFocus):
            p = self.focus.payload
            self.focus.payload = p.model_copy(
                update={"transient_switches": p.transient_switches + 1}
            )

    def _emit_open(self, o: _Open, now: datetime) -> None:
        self._out.append(
            Event.interval(
                o.payload, o.start, None, id=o.id, last_seen=now, observer_version=self.version
            )
        )

    def _close(self, o: _Open, at: datetime) -> None:
        end = max(at, o.start)
        if isinstance(o.payload, WindowFocus) and end > o.start:
            self.history.append(((o.payload.exe or "").lower(), o.payload.title, o.start, end))
        self._out.append(
            Event.interval(
                o.payload, o.start, end, id=o.id, last_seen=end, observer_version=self.version
            )
        )

    def _checkpoint(self, now: datetime) -> None:
        for o in (self.focus, self.idle):
            if o is not None and (now - o.last_checkpoint).total_seconds() >= self.checkpoint_s:
                o.last_checkpoint = now
                self._emit_open(o, now)


class WindowObserver(BaseObserver):
    name = "window"
    version = VERSION

    def __init__(
        self,
        probe: WindowProbe,
        session: SessionState,
        window_cfg: WindowObserverConfig,
        idle_cfg: IdleObserverConfig,
    ) -> None:
        super().__init__()
        self.probe = probe
        self.session = session
        self.poll_s = window_cfg.poll_interval_s
        self.tracker = FocusTracker(
            min_focus_s=window_cfg.min_focus_s,
            title_stable_s=window_cfg.title_stable_s,
            idle_threshold_s=idle_cfg.threshold_s,
        )
        self._was_paused = False

    async def run(self, ctx: ObserverContext) -> None:
        while True:
            now = ctx.clock.now().astimezone()
            if ctx.paused.is_set():
                if not self._was_paused:
                    self.tracker.reset()  # the daemon closes open rows at pause time
                    self._was_paused = True
            else:
                self._was_paused = False
                sample = self.probe.sample()
                for event in self.tracker.tick(
                    now, sample, self.probe.idle_seconds(), self.session.locked
                ):
                    self.emit(event)
            await ctx.clock.sleep(self.poll_s)

    async def stop(self) -> None:
        if self._ctx is not None:
            for event in self.tracker.close_all(self._ctx.clock.now().astimezone()):
                self.emit(event)

    def forget(self, start: datetime, end: datetime) -> None:
        self.tracker.reset()

    def foreground_ms(
        self, exes: set[str], titles: set[str], start: datetime, end: datetime
    ) -> int | None:
        """How long, within [start, end], a window of one of `exes` showing one of `titles`
        was in front (idle and locked time excluded: those close focus intervals).
        None when there's no window record covering `start` (e.g. the daemon started later)."""
        return foreground_overlap(self.tracker, exes, titles, start, end)

    def on_pause(self, at: datetime) -> None:
        for event in self.tracker.close_all(at.astimezone()):
            self.emit(event)


# Separator: hyphen, en dash or em dash. Edge's window title contains a zero-width space
# (U+200B) inside "Microsoft Edge". Code points via chr() keep the source plain ASCII.
_DASHES = "-" + chr(0x2013) + chr(0x2014)
_BROWSER_SUFFIX_RE = re.compile(
    r"\s+["
    + _DASHES
    + r"]\s+(?:Google Chrome|Brave|Microsoft"
    + chr(0x200B)
    + r"?\s?Edge|Chromium)$"
)


def browser_page_title(window_title: str) -> str:
    """ "What is GraphRAG? - Google Chrome" -> "What is GraphRAG?" (also strips a profile name)."""
    return _BROWSER_SUFFIX_RE.sub("", window_title).strip()


Interval = tuple[str, str, datetime, datetime]  # (exe lower-case, window title, start, end)


def overlap_ms(
    intervals: list[Interval], exes: set[str], titles: set[str], start: datetime, end: datetime
) -> int:
    """Milliseconds within [start, end] that a window of `exes` showing one of `titles` was in
    front. Window titles are compared without their " - Google Chrome"-style suffix."""
    # A redacted title could be any hidden page (mail, a private window): never match on it.
    wanted = {normalize_title(t).casefold() for t in titles if t and t != "[redacted]"}
    if not wanted:
        return 0
    total = 0.0
    for exe, title, a, b in intervals:
        if exe not in exes or normalize_title(browser_page_title(title)).casefold() not in wanted:
            continue
        overlap = (min(b, end) - max(a, start)).total_seconds()
        if overlap > 0:
            total += overlap
    return int(total * 1000)


def foreground_overlap(
    tracker: FocusTracker, exes: set[str], titles: set[str], start: datetime, end: datetime
) -> int | None:
    intervals: list[Interval] = list(tracker.history)
    if tracker.focus is not None and isinstance(tracker.focus.payload, WindowFocus):
        p = tracker.focus.payload
        now = tracker.last_tick or tracker.focus.start
        intervals.append(((p.exe or "").lower(), p.title, tracker.focus.start, now))
    if not intervals or min(i[2] for i in intervals) > start + timedelta(seconds=5):
        return None
    return overlap_ms(intervals, exes, titles, start, end)
