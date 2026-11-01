"""Global pause, persisted in kv_settings so every process (daemon, CLI, hooks) agrees.

A marker file in the runtime dir mirrors the state for the shell hooks, which check it with a
single file-exists test instead of talking to the daemon.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from personal_os.core.clock import Clock
from personal_os.core.events import Event, MetaEvent
from personal_os.storage import repo

PAUSE_KEY = "pause"
MARKER_NAME = "paused"
PAUSE_VERSION = "0.1.0"


def pause(
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    duration: timedelta | None = None,
    runtime_dir: Path | None = None,
) -> Event:
    """Pause capture (indefinitely, or for `duration`). Returns the meta.pause marker event."""
    until = clock.now() + duration if duration else None
    repo.set_setting(conn, PAUSE_KEY, {"until": until.isoformat() if until else None}, clock)
    if runtime_dir is not None:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / MARKER_NAME).write_text(until.isoformat() if until else "", "utf-8")
    return Event.point(
        MetaEvent(kind="meta.pause", until=until), clock.now(), observer_version=PAUSE_VERSION
    )


def resume(conn: sqlite3.Connection, clock: Clock, *, runtime_dir: Path | None = None) -> Event:
    repo.delete_setting(conn, PAUSE_KEY)
    if runtime_dir is not None:
        (runtime_dir / MARKER_NAME).unlink(missing_ok=True)
    return Event.point(MetaEvent(kind="meta.resume"), clock.now(), observer_version=PAUSE_VERSION)


def paused_until(conn: sqlite3.Connection) -> tuple[bool, datetime | None]:
    """(is_paused_setting_present, until). until=None with True means paused indefinitely."""
    value = repo.get_setting(conn, PAUSE_KEY)
    if value is None:
        return False, None
    until = value.get("until")
    return True, datetime.fromisoformat(until) if until else None


def is_paused(conn: sqlite3.Connection, clock: Clock) -> bool:
    present, until = paused_until(conn)
    if not present:
        return False
    return until is None or clock.now() < until


class PauseGate:
    """Cached pause check for the pipeline (re-reads the setting at most once per second)."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock, ttl_s: float = 1.0) -> None:
        self._conn = conn
        self._clock = clock
        self._ttl = ttl_s
        self._checked_at: float | None = None
        self._value = False

    def __call__(self) -> bool:
        now = self._clock.monotonic()
        if self._checked_at is None or now - self._checked_at >= self._ttl:
            self._value = is_paused(self._conn, self._clock)
            self._checked_at = now
        return self._value
