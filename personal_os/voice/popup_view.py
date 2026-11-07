"""What the voice popup shows, and when it closes: pure functions, testable without a window.

The popup reads only the runtime folder: the voice status file (the live turn), and the pause
and kill-switch markers. It never reads captured activity. ADR-071.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

READ_S = 20.0  # an answer with details stays up at least this long
ACTIVE = ("listening", "thinking", "speaking")


@dataclass(frozen=True)
class View:
    voice_running: bool
    state: str  # listening | thinking | speaking | done | idle | not running
    turn_id: str = ""
    trigger: str = ""
    heard: str = ""
    reply: str = ""
    question: str = ""
    details: str = ""  # the full answer for the screen (lists, links, evidence)
    progress: str = ""  # what she's doing while thinking ("Using recall…")
    dismissed: bool = False  # you stopped this turn: the window closes now
    error: str = ""
    paused: bool = False
    killed: bool = False
    alerts: tuple[dict[str, Any], ...] = ()  # due reminders: shown until dismissed

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _paused(runtime_dir: Path, now: datetime) -> bool:
    marker = runtime_dir / "paused"
    if not marker.exists():
        return False
    try:
        until = marker.read_text("utf-8").strip()
    except OSError:
        return True
    if not until:
        return True  # paused until you resume
    try:
        return datetime.fromisoformat(until) > now
    except ValueError:
        return True


def view_from(
    status: dict[str, Any],
    *,
    paused: bool,
    killed: bool,
    alerts: tuple[dict[str, Any], ...] = (),
) -> View:
    """The popup's view of a voice status dict (as voice.main.status() returns it)."""
    running = bool(status.get("running"))
    turn = status.get("turn") or {}
    if not running:
        state = "not running"
    elif turn:
        state = str(turn.get("state") or "done")
    else:
        state = "idle"
    return View(
        voice_running=running,
        state=state,
        turn_id=str(turn.get("id", "")),
        trigger=str(turn.get("trigger", "")),
        heard=str(turn.get("heard", "")),
        reply=str(turn.get("reply", "")),
        question=str(turn.get("question", "")),
        details=str(turn.get("details", "")),
        progress=str(turn.get("progress", "")),
        dismissed=bool(turn.get("dismissed", False)),
        error=str(turn.get("error", "")) or str(status.get("error", "")),
        paused=paused,
        killed=killed,
        alerts=alerts,
    )


def read_view(runtime_dir: Path, now: datetime) -> View:
    from personal_os.actions import killswitch
    from personal_os.interfaces import alerts
    from personal_os.voice.main import status

    return view_from(
        status(runtime_dir),
        paused=_paused(runtime_dir, now),
        killed=killswitch.engaged(runtime_dir),
        alerts=tuple(alerts.pending(runtime_dir, now)),
    )


class AutoClose:
    """Closes `seconds` after a turn finishes, unless pinned. A new turn (a new id) while open
    starts over, so the same window is reused. Voice stopping also closes it."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.pinned = False
        self.turn_id = ""
        self.done_since: float | None = None

    def pin(self, on: bool) -> None:
        self.pinned = on
        if not on and self.done_since is not None:
            self.done_since = None  # unpinning gives the full delay again

    def step(self, view: View, now: float) -> tuple[bool, bool]:
        """(close now?, is this a new turn?) for the latest view at monotonic time `now`."""
        new_turn = bool(view.turn_id) and view.turn_id != self.turn_id
        if new_turn:
            self.turn_id = view.turn_id
            self.done_since = None
        finished = view.state in ("done", "idle", "not running") and not view.alerts
        if not finished:  # a turn in progress, or a reminder not yet dismissed
            self.done_since = None
            return False, new_turn
        if self.done_since is None:
            self.done_since = now
        if self.pinned:
            return False, new_turn
        if view.dismissed:  # stopped: gone at once, not after the usual few seconds
            return True, new_turn
        wait = max(self.seconds, READ_S) if view.details else self.seconds  # time to read it
        return now - self.done_since >= wait, new_turn
