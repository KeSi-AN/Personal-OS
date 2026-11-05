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


def _spoken_time(at: str) -> str:
    """Times the way speech recognition writes them: "2.10" is 2:10, "5 p.m." is 5pm,
    "5 o'clock" is 5."""
    t = at.strip().lower()
    t = re.sub(r"\b([ap])\.?\s*m\.?", r"\1m", t)  # p.m. / p. m. / pm
    t = re.sub(r"(\d)\.(\d{2})\b", r"\1:\2", t)  # 2.10 -> 2:10
    t = re.sub(r"\s*o'?\s*clock\b", "", t)
    return t.strip()


def due_time(now: datetime, minutes: int | None, at: str | None) -> datetime:
    """`minutes` from now, or the next occurrence of a clock time like '17:30' or '5pm'."""
    if minutes is not None:
        return now + timedelta(minutes=minutes)
    m = _AT.match(_spoken_time(at or ""))
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


# Questions about reminders ("what reminders do I have?", "what did I ask you to remind me?")
# are answered from this table: they aren't in your captured activity, so `ask` would never
# find them there.
REMINDER_Q = re.compile(r"\b(reminders?|remind(?:ed|ing)? me|remind you)\b", re.I)
QUESTION = re.compile(
    r"^\s*(?:hey\s+)?(?:donna\W*\s*)?(?:what|which|when|did|do|does|have|has|any|list|show|tell|"
    r"how many|is there|are there|was there|what's|whats)\b",
    re.I,
)
RECENT = timedelta(hours=24)


def is_question(text: str) -> bool:
    return bool(REMINDER_Q.search(text) and QUESTION.match(text))


def _when(due: datetime, now: datetime) -> str:
    d, n = due.astimezone(), now.astimezone()
    if d.date() == n.date():
        return f"at {d:%H:%M} today"
    if (d.date() - n.date()).days == 1:
        return f"at {d:%H:%M} tomorrow"
    if (n.date() - d.date()).days == 1:
        return f"at {d:%H:%M} yesterday"
    return f"on {d:%a %d %b} at {d:%H:%M}"


def answer(conn: sqlite3.Connection, now: datetime) -> list[str]:
    """What's coming up, and what went off in the last day, in plain sentences."""
    upcoming = pending(conn)[:5]
    since = to_ms(now - RECENT)
    fired = [
        (r[0], from_ms(r[1]))
        for r in conn.execute(
            "SELECT text, due_at FROM reminders WHERE fired_at IS NOT NULL AND fired_at >= ? "
            "AND cancelled = 0 ORDER BY due_at DESC LIMIT 3",
            (since,),
        )
    ]
    out = []
    if upcoming:
        items = "; ".join(f'"{r.text}" {_when(r.due, now)}' for r in upcoming)
        n = len(upcoming)
        out.append(f"You have {n} reminder{'s' if n > 1 else ''} coming up: {items}.")
    else:
        out.append("You have no reminders coming up.")
    if fired:
        items = "; ".join(f'"{text}" {_when(due, now)}' for text, due in fired)
        out.append(f"In the last day: {items}.")
    return out
