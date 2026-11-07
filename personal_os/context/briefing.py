"""Briefings (brief §8): a morning briefing and an end-of-day summary, built from facts.

No free-form model text: every line comes from stored summaries, rollups, commands, reminders
or (if connected) Google, so a briefing can't invent anything. Each line is short enough to
be read aloud.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from personal_os.actions import reminders
from personal_os.context import rollups
from personal_os.context.evidence import OWN_COMMIT_SQL
from personal_os.context.summarizer import load_summaries
from personal_os.core.clock import Clock, to_ms
from personal_os.projects.sessions import day_bounds, load_sessions
from personal_os.storage import repo

Kind = Literal["morning", "evening"]
DELIVERED_KEY = "briefings.delivered"


@dataclass
class Briefing:
    kind: Kind
    title: str
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # e.g. "Google not connected"

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _dur(ms: int) -> str:
    m = ms // 60000
    if m >= 60:
        return f"{m // 60} hour{'s' * (m // 60 != 1)}" + (f" {m % 60} minutes" if m % 60 else "")
    return f"{m} minutes"


def _time_by_project(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> list[tuple[str, int]]:
    totals: dict[str, int] = {}
    for s in load_sessions(conn, start, end):
        if s.project_id and s.project_id != "none":
            totals[s.project_id] = totals.get(s.project_id, 0) + s.active_ms
    return sorted(totals.items(), key=lambda kv: -kv[1])


def _open_threads(conn: sqlite3.Connection, now: datetime, days: int = 2) -> list[str]:
    """Open threads from each project's latest summary (older ones may have been resolved
    since), plus commands whose last run in the past day failed. Personal time is left out."""
    out: list[str] = []
    seen_projects: set[str] = set()
    for s in reversed(load_summaries(conn, now - timedelta(days=days), now)):
        if not s.project_id or s.project_id == "none" or s.project_id in seen_projects:
            continue
        seen_projects.add(s.project_id)
        for item in s.body.open_threads:
            text = item.text.rstrip(".")
            if text not in out:
                out.append(text)
    rows = conn.execute(
        "SELECT payload, ts_start, project_id FROM events WHERE source = 'terminal' "
        "AND ts_start >= ? ORDER BY ts_start",
        (to_ms(now - timedelta(days=1)),),
    ).fetchall()
    last: dict[str, bool] = {}
    where: dict[str, str] = {}
    for payload, _ts, pid in rows:
        p = json.loads(payload)
        cmd = (p.get("command") or "").strip()
        if cmd:
            last[cmd] = p.get("success") is not False
            where[cmd] = pid or ""
    for cmd, ok in last.items():
        if not ok:
            short = cmd if len(cmd) <= 60 else cmd[:57] + "..."
            out.append(f"{short} was still failing" + (f" in {where[cmd]}" if where[cmd] else ""))
    return out[:3]


def _next_step(conn: sqlite3.Connection, now: datetime) -> str | None:
    recent = _time_by_project(conn, now - timedelta(days=2), now)
    for pid, _ms in recent[:1]:
        state = rollups.load(conn, f"project:{pid}")
        if state and state.body.get("next_steps"):
            step = state.body["next_steps"][0]
            return f"{step['text'] if isinstance(step, dict) else step} ({pid})"
        for s in reversed(load_summaries(conn, now - timedelta(days=2), now, project_id=pid)):
            if s.body.next_step:
                return f"{s.body.next_step.text} ({pid})"
    return None


def _google_lines(google: object | None, now: datetime) -> tuple[list[str], list[str]]:
    """(lines, notes) for today's calendar and unread mail."""
    if google is None:
        return [], ["Google isn't connected, so no calendar or mail."]
    from personal_os.integrations.google import Google, GoogleError, today_range

    assert isinstance(google, Google)
    lines: list[str] = []
    notes: list[str] = []
    try:
        start, end = today_range(now)
        events = [e for e in google.calendar(max(start, now - timedelta(hours=1)), end) if e.start]
        if events:
            parts = [
                ("all day: " if e.all_day else f"{e.start.astimezone():%H:%M} ") + e.title
                for e in events[:4]
                if e.start is not None
            ]
            lines.append(
                f"Today you have {len(events)} event{'s' * (len(events) != 1)}: "
                + "; ".join(parts)
                + "."
            )
        else:
            lines.append("Nothing else on your calendar today.")
    except GoogleError as exc:
        notes.append(f"Calendar unavailable ({exc}).")
    try:
        inbox = google.inbox(top=2)
        if inbox.unread:
            who = ", ".join(f"{m.sender} about {m.subject}" for m in inbox.top)
            lines.append(
                f"{inbox.unread} unread in your primary inbox"
                + (f", newest from {who}." if who else ".")
            )
    except GoogleError as exc:
        notes.append(f"Mail unavailable ({exc}).")
    return lines, notes


def build(
    conn: sqlite3.Connection, clock: Clock, kind: Kind, *, google: object | None = None
) -> Briefing:
    now = clock.now()
    local = now.astimezone()
    today_start, today_end = day_bounds(local)
    if kind == "morning":
        b = Briefing(kind, f"Good {'morning' if local.hour < 12 else 'day'}")
        y_start = today_start - timedelta(days=1)
        roll = rollups.load(conn, f"day:{y_start:%Y-%m-%d}")
        if roll and roll.body.get("headline"):
            b.lines.append(f"Yesterday: {roll.body['headline'].rstrip('.')}.")
        else:
            spent = _time_by_project(conn, y_start, today_start)
            if spent:
                b.lines.append(
                    "Yesterday you worked on "
                    + ", ".join(f"{p} for {_dur(ms)}" for p, ms in spent[:3])
                    + "."
                )
            else:
                b.lines.append("I didn't see any project work yesterday.")
        threads = _open_threads(conn, now)
        if threads:
            b.lines.append("Still open: " + "; ".join(threads) + ".")
        g_lines, g_notes = _google_lines(google, now)
        b.lines += g_lines
        b.notes += g_notes
        due = [r for r in reminders.pending(conn) if r.due < today_end]
        if due:
            b.lines.append(
                "Reminders today: "
                + "; ".join(f"{r.text} at {r.due.astimezone():%H:%M}" for r in due[:3])
                + "."
            )
        step = _next_step(conn, now)
        if step:
            b.lines.append(f"Suggested next step: {step}.")
    else:
        b = Briefing(kind, "End of the day")
        spent = _time_by_project(conn, today_start, now)
        if spent:
            total = sum(ms for _, ms in spent)
            b.lines.append(
                f"Today you worked {_dur(total)}: "
                + ", ".join(f"{p} {_dur(ms)}" for p, ms in spent[:4])
                + "."
            )
        else:
            b.lines.append("I didn't see any project work today.")
        commits = conn.execute(
            "SELECT count(*) FROM events WHERE source = 'git' AND type = 'commit' "
            f"AND ts_start >= ? AND {OWN_COMMIT_SQL}",
            (to_ms(today_start),),
        ).fetchone()[0]
        if commits:
            b.lines.append(f"You made {commits} commit{'s' * (commits != 1)}.")
        threads = _open_threads(conn, now, days=1)
        if threads:
            b.lines.append("Left open: " + "; ".join(threads) + ".")
        tomorrow = [
            r for r in reminders.pending(conn) if today_end <= r.due < today_end + timedelta(days=1)
        ]
        if tomorrow:
            b.lines.append("Tomorrow: " + "; ".join(r.text for r in tomorrow[:3]) + ".")
    return b


# --- scheduling ------------------------------------------------------------------------------


def due_kind(
    conn: sqlite3.Connection, clock: Clock, morning_after: str, evening_after: str
) -> Kind | None:
    """Which briefing is due now and not yet given today (each at most once a day)."""
    local = clock.now().astimezone()
    today = f"{local:%Y-%m-%d}"
    delivered = repo.get_setting(conn, DELIVERED_KEY, {}) or {}
    hm = f"{local:%H:%M}"
    if hm >= evening_after and delivered.get("evening") != today:
        return "evening"
    if morning_after <= hm < evening_after and delivered.get("morning") != today:
        return "morning"
    return None


def mark_delivered(conn: sqlite3.Connection, clock: Clock, kind: Kind) -> None:
    delivered = repo.get_setting(conn, DELIVERED_KEY, {}) or {}
    delivered[kind] = f"{clock.now().astimezone():%Y-%m-%d}"
    repo.set_setting(conn, DELIVERED_KEY, delivered, clock)
