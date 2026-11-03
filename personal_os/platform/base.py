"""OS-neutral probe interfaces. Observers depend on these, never on an OS module directly."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

SessionSignal = Literal["lock", "unlock", "logon", "logoff", "sleep", "wake", "hotkey"]


class NotSupportedError(RuntimeError):
    """The current OS has no implementation for this probe (see DESIGN.md §4.4 OS notes)."""


@dataclass(frozen=True)
class WindowSample:
    """What is in the foreground right now. Never contains window contents."""

    hwnd: int
    pid: int | None
    app: str
    exe: str | None = None
    exe_path: str | None = None
    title: str = ""
    window_class: str | None = None
    cwd: str | None = None
    is_uwp: bool = False
    elevated_unreadable: bool = False


class WindowProbe(Protocol):
    def sample(self) -> WindowSample | None:
        """The foreground window, or None during transitions (no foreground window)."""
        ...

    def idle_seconds(self) -> float:
        """Seconds since the last keyboard/mouse input anywhere in the session."""
        ...

    def fullscreen(self) -> bool:
        """A full-screen app, game or presentation is in the foreground."""
        ...


class SessionWatcher(Protocol):
    """Delivers lock/unlock, sleep/wake, logon/logoff and hotkey signals from a worker thread."""

    def start(self, on_signal: Callable[[SessionSignal], None]) -> None: ...

    def stop(self) -> None: ...
