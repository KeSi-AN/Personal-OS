"""Ducking: other apps turned down while Donna listens and talks, as smart speakers do.

Music on the same speakers is more than the echo canceller can take out cleanly, so with a
song playing she misheard ("claim Maranamas on YouTube") or missed what was said. For the
length of a turn, every other app that's making sound is turned down to `voice.duck` of its
volume (each app's own volume in the Windows mixer; the master volume is left alone), and put
back afterwards. The volumes it changed are kept in the runtime folder until restored, so a
crash mid-turn never leaves your music quiet: the next start puts them back.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from personal_os.core import runtime
from personal_os.core.logging import get_logger

_log = get_logger("voice.duck")
FILE = "voice_ducked.json"


def _sessions() -> list[Any]:
    """Audio sessions of other apps that are playing now (pycaw), or [] if unknown."""
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities

        with contextlib.suppress(OSError):
            comtypes.CoInitialize()
        own = os.getpid()
        return [
            s
            for s in AudioUtilities.GetAllSessions()
            if s.Process is not None and s.Process.pid != own and s.State == 1
        ]
    except Exception:
        return []


def _key(session: Any) -> str:
    return f"{session.Process.pid}:{session.Process.name()}"


class Ducker:
    def __init__(
        self,
        runtime_dir: Path,
        level: float,
        sessions: Callable[[], list[Any]] | None = None,
    ) -> None:
        self.runtime_dir = runtime_dir
        self.level = level
        self.sessions = sessions or (lambda: _sessions())  # looked up when used
        self.saved: dict[str, float] = {}  # session key -> its volume before
        self._live: dict[str, Any] = {}

    @contextlib.contextmanager
    def turn(self) -> Iterator[None]:
        """Other apps down for the duration; nested use keeps the first (outer) duck."""
        if self.saved or self.level >= 1.0:
            yield
            return
        self.duck()
        try:
            yield
        finally:
            self.restore()

    def duck(self) -> None:
        for s in self.sessions():
            try:
                vol = s.SimpleAudioVolume
                before = float(vol.GetMasterVolume())
                if before <= 0.0:
                    continue
                key = _key(s)
                self.saved[key], self._live[key] = before, s
                vol.SetMasterVolume(before * self.level, None)
            except Exception:
                continue
        if self.saved:
            with contextlib.suppress(OSError):
                runtime.write_json_atomic(self.runtime_dir / FILE, self.saved)

    def restore(self) -> None:
        for key, before in self.saved.items():
            with contextlib.suppress(Exception):
                self._live[key].SimpleAudioVolume.SetMasterVolume(before, None)
        self.saved.clear()
        self._live.clear()
        (self.runtime_dir / FILE).unlink(missing_ok=True)

    def recover(self) -> None:
        """Put back volumes a crashed turn left turned down (at startup)."""
        left = runtime.read_json(self.runtime_dir / FILE) or {}
        if not left:
            return
        for s in self.sessions():
            with contextlib.suppress(Exception):
                if _key(s) in left:
                    s.SimpleAudioVolume.SetMasterVolume(float(left[_key(s)]), None)
        (self.runtime_dir / FILE).unlink(missing_ok=True)
        _log.info("restored volumes left turned down", extra={"fields": {"apps": len(left)}})
