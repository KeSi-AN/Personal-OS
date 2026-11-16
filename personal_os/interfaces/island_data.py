"""What the island's cards show: Today, Projects, Music. Read-only, small, and only fetched
while the island is open on that page (never for the pill). ADR-082.

Everything here is already on the dashboard; the island shows a few lines of it. Nothing is
written, and nothing captured is shown beyond names, times and to-do titles (no page text,
no window titles).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from personal_os.core.clock import to_ms


def _dur(ms: int) -> str:
    m = int(ms // 60_000)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def _ago(then: datetime, now: datetime) -> str:
    s = (now - then).total_seconds()
    if s < 3600:
        return f"{max(1, int(s // 60))} min ago"
    if s < 86_400:
        return f"{int(s // 3600)} h ago"
    days = int(s // 86_400)
    return "yesterday" if days == 1 else f"{days} days ago"


def _clock(when: datetime, now: datetime) -> str:
    local, today = when.astimezone(), now.astimezone().date()
    if local.date() == today:
        return local.strftime("%H:%M")
    if local.date() == today + timedelta(days=1):
        return "tomorrow " + local.strftime("%H:%M")
    return local.strftime("%a %H:%M")


def _names(conn: sqlite3.Connection) -> dict[str, str]:
    from personal_os.projects.registry import load_projects

    return {p.id: p.name for p in load_projects(conn)}


def today(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """Time worked today by project, the next reminders, and to-dos (overdue first)."""
    from personal_os.actions import reminders
    from personal_os.projects import items
    from personal_os.projects.sessions import day_bounds, load_sessions

    start, _end = day_bounds(now.astimezone())
    names = _names(conn)
    by: dict[str, int] = {}
    for s in load_sessions(conn, start, now):
        key = s.project_id if s.project_id and s.project_id != "none" else ""
        by[key] = by.get(key, 0) + s.active_ms
    total = sum(by.values())
    top = sorted(((k, v) for k, v in by.items() if k), key=lambda kv: -kv[1])[:3]
    rem = [
        {"text": r.text, "when": _clock(r.due, now)}
        for r in reminders.pending(conn)
        if r.due >= now - timedelta(minutes=5)
    ][:3]
    todos = []
    for t in items.todos(conn, limit=20):
        overdue = t.due is not None and t.due < now
        todos.append({
            "text": t.title, "due": _clock(t.due, now) if t.due else "",
            "overdue": overdue, "project": names.get(t.project_id or "", ""),
        })  # fmt: skip
    todos.sort(key=lambda t: (not t["overdue"], t["due"] == "", t["due"]))
    return {
        "worked": _dur(total) if total else "",
        "projects": [{"name": names.get(k, k), "time": _dur(v)} for k, v in top],
        "reminders": rem,
        "todos": todos[:4],
        "more_todos": max(0, len(todos) - 4),
    }


def projects(conn: sqlite3.Connection, now: datetime, limit: int = 4) -> list[dict[str, Any]]:
    """The projects you worked on most recently: when, where you left off, the next to-do."""
    from personal_os.context.summarizer import load_summaries
    from personal_os.projects import items
    from personal_os.projects.registry import load_projects

    last = dict(conn.execute(
        "SELECT project_id, MAX(ts_end) FROM sessions WHERE project_id IS NOT NULL "
        "AND project_id != 'none' GROUP BY 1"
    ).fetchall())  # fmt: skip
    keep = [p for p in load_projects(conn) if p.id in last]
    keep.sort(key=lambda p: -last[p.id])
    open_todos: dict[str, list[str]] = {}
    for t in items.todos(conn, limit=200):
        if t.project_id:
            open_todos.setdefault(t.project_id, []).append(t.title)
    out = []
    for p in keep[:limit]:
        from personal_os.core.clock import from_ms

        when = from_ms(int(last[p.id]))
        summaries = load_summaries(conn, now - timedelta(days=14), now, project_id=p.id)
        left = ""
        if summaries:
            body = summaries[-1].body
            left = (body.next_step.text if body.next_step else "") or body.goal
        todo = open_todos.get(p.id, [])
        out.append({
            "id": p.id, "name": p.name, "when": _ago(when, now), "left_off": left[:160],
            "next_todo": todo[0] if todo else "", "todos": len(todo),
        })  # fmt: skip
    return out


def music(runtime_dir: Path, browser: str) -> dict[str, Any]:
    """What's playing in the browser (title, artist, playing), from the extension."""
    from personal_os.browser import control

    if not control.connected(runtime_dir):
        return {"available": False, "why": "Open Brave or Chrome with the Personal OS extension."}
    try:
        s = control.send(runtime_dir, "media", {"action": "status"}, browser=browser, timeout_s=3)
    except Exception:
        return {"available": False, "why": "The browser didn't answer."}
    s = s or {}
    if not s.get("title"):
        return {"available": True, "title": "", "playing": False}
    return {
        "available": True,
        "title": str(s.get("title", ""))[:120],
        "artist": str(s.get("artist", "") or "")[:80],
        "playing": bool(s.get("playing")),
    }


def media(runtime_dir: Path, browser: str, action: str) -> bool:
    """Play, pause, next or previous in the browser's music tab."""
    from personal_os.browser import control

    if action not in ("play", "pause", "toggle", "next", "previous"):
        return False
    try:
        control.send(runtime_dir, "media", {"action": action}, browser=browser, timeout_s=4)
    except Exception:
        return False
    return True


def since_ms(now: datetime, hours: int) -> int:
    return to_ms(now - timedelta(hours=hours))
