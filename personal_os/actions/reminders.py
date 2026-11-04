"""Reminders: stored by the set_reminder tool, fired by the daemon as a notification (§6.4)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from personal_os.core.clock import Clock, from_ms, to_ms
from personal_os.core.ids import uuid7

_AT = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.I)


@dataclass
class Reminder:
    id: str
    text: str
    due: datetime


def due_time(now: datetime, minutes: int | None, at: str | None) -> datetime:
    """`minutes` from now, or the next occurrence of a clock time like '17:30' or '5pm'."""
    if minutes is not None:
        return now + timedelta(minutes=minutes)
    m = _AT.match((at or "").strip())
    if not m:
        raise ValueError("give minutes, or a time like 17:30 or 5pm")
    hour, minute, ampm = int(m[1]), int(m[2] or 0), (m[3] or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise ValueError("that isn't a time of day")
    local = now.astimezone()
    due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return due if due > local else due + timedelta(days=1)


def add(conn: sqlite3.Connection, clock: Clock, text: str, due: datetime, action_id: str) -> str:
    rid = str(uuid7(clock.now()))
    conn.execute(
        "INSERT INTO reminders (id, text, due_at, created_at, action_id) VALUES (?, ?, ?, ?, ?)",
        (rid, text, to_ms(due), to_ms(clock.now()), action_id),
    )
    return rid


def cancel(conn: sqlite3.Connection, rid: str) -> bool:
    cur = conn.execute(
        "UPDATE reminders SET cancelled = 1 WHERE id = ? AND fired_at IS NULL", (rid,)
    )
    return cur.rowcount == 1


def pending(conn: sqlite3.Connection) -> list[Reminder]:
    return [
        Reminder(r[0], r[1], from_ms(r[2]))
        for r in conn.execute(
            "SELECT id, text, due_at FROM reminders WHERE fired_at IS NULL AND cancelled = 0 "
            "ORDER BY due_at"
        )
    ]


def take_due(conn: sqlite3.Connection, clock: Clock) -> list[Reminder]:
    """Reminders that are due now; each is marked fired so it shows only once."""
    now = to_ms(clock.now())
    due = [
        Reminder(r[0], r[1], from_ms(r[2]))
        for r in conn.execute(
            "SELECT id, text, due_at FROM reminders WHERE fired_at IS NULL AND cancelled = 0 "
            "AND due_at <= ?",
            (now,),
        )
    ]
    for r in due:
        conn.execute("UPDATE reminders SET fired_at = ? WHERE id = ?", (now, r.id))
    return due
