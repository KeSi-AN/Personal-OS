"""What the island's cards show: Today, Notes, Projects, Music. Small, and only fetched while
the island is open on that page (never for the pill). ADR-082.

Everything here is already on the dashboard; the island shows a few lines of it. Nothing
captured is shown beyond names, times and to-do titles (no page text, no window titles).

It writes only your own items, as the dashboard does (ADR-077): ticking a to-do off (or back
on), adding a to-do (a time at the end, "call mum tomorrow 5pm", becomes its due time) and
adding a note. Actions still go through the executor; these aren't actions on your computer.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from personal_os.config.schema import Config
from personal_os.core.clock import Clock, to_ms


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
            "id": t.id, "text": t.title, "due": _clock(t.due, now) if t.due else "",
            "overdue": overdue, "project": names.get(t.project_id or "", ""),
            "due_ms": to_ms(t.due) if t.due else None,
        })  # fmt: skip
    # Overdue first, then by due time, then the ones with no time (newest first, as read).
    todos.sort(key=lambda d: (not d["overdue"], d["due_ms"] is None, d["due_ms"] or 0))
    for shown in todos:
        del shown["due_ms"]
    return {
        "worked": _dur(total) if total else "",
        "projects": [{"name": names.get(k, k), "time": _dur(v)} for k, v in top],
        "reminders": rem,
        "todos": todos[:TODOS_SHOWN],
        "more_todos": max(0, len(todos) - TODOS_SHOWN),
    }


TODOS_SHOWN = 6
NOTES_SHOWN = 8


def _recent_projects(conn: sqlite3.Connection, limit: int = 6) -> list[dict[str, str]]:
    """The projects worked on most recently, for the "which project" picker."""
    from personal_os.projects.registry import load_projects

    last = dict(conn.execute(
        "SELECT project_id, MAX(ts_end) FROM sessions WHERE project_id IS NOT NULL "
        "AND project_id != 'none' GROUP BY 1"
    ).fetchall())  # fmt: skip
    keep = [p for p in load_projects(conn) if p.id != "none"]
    keep.sort(key=lambda p: -(last.get(p.id) or 0))
    return [{"id": p.id, "name": p.name} for p in keep[:limit]]


def notes(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """The latest notes, in any project or the inbox, newest first; and projects to file to."""
    from personal_os.core.clock import from_ms

    names = _names(conn)
    rows = conn.execute(
        "SELECT id, project_id, title, body, created_at FROM project_items "
        "WHERE kind = 'note' AND source != 'auto' ORDER BY created_at DESC LIMIT ?",
        (NOTES_SHOWN + 1,),
    ).fetchall()
    shown = [
        {
            "id": r[0], "text": (r[3] or r[2])[:280], "project": names.get(r[1] or "", ""),
            "when": _ago(from_ms(int(r[4])), now),
        }
        for r in rows[:NOTES_SHOWN]
    ]  # fmt: skip
    return {"notes": shown, "more": len(rows) > NOTES_SHOWN, "projects": _recent_projects(conn)}


# --- what you change from the island -----------------------------------------------------------


def _project_id(conn: sqlite3.Connection, project: str) -> str | None:
    """A project id picked on the island ("" is the inbox); one that isn't known is the inbox."""
    from personal_os.projects.registry import get_project

    return project if project and get_project(conn, project) is not None else None


def set_todo(conn: sqlite3.Connection, clock: Clock, item_id: str, done: bool) -> bool:
    """Tick a to-do off, or back on (a mis-click is undone by clicking again)."""
    from personal_os.projects import items

    return items.set_done(conn, clock, str(item_id), bool(done))


def add_todo(conn: sqlite3.Connection, clock: Clock, text: str, project: str = "") -> str:
    """A to-do from the island's box. A time at the end ("tomorrow 5pm", "by friday")
    becomes its due time, so it's held to like a spoken commitment. Returns "" or why not."""
    from personal_os.context import commitments
    from personal_os.projects import items

    title, when = commitments.split_when(" ".join(str(text).split()))
    if not title:
        return "Type what to do."
    due = None
    if when:
        try:
            due = commitments.due_from(when, clock.now())
        except ValueError:
            title = f"{title} {when}"  # not a time after all: keep the words
    items.add(conn, clock, _project_id(conn, project), "todo", title=title[:300], due=due,
              source="overlay")  # fmt: skip
    return ""


def add_note(conn: sqlite3.Connection, clock: Clock, text: str, project: str = "") -> str:
    """A note from the island's box, to a project or the inbox. Returns "" or why not."""
    from personal_os.projects import items

    body = str(text).strip()
    if not body:
        return "Type a note."
    first = body.splitlines()[0]
    items.add(conn, clock, _project_id(conn, project), "note", title=first[:80], body=body[:5000],
              source="overlay")  # fmt: skip
    return ""


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


def _music_backend(cfg: Config) -> Any:
    from personal_os.actions.music import LiveBackend

    return LiveBackend(cfg.runtime_dir, cfg.actions.browser)


def music(cfg: Config, backend: Any = None) -> dict[str, Any]:
    """The Music page: your music source only (config [music], ADR-084); with "any", whatever
    plays in the browser, as before."""
    from personal_os.actions import music as m

    src = m.chosen(cfg.music.source)
    if src is None:
        return browser_music(cfg.runtime_dir, cfg.actions.browser)
    now = m.now(src, backend or _music_backend(cfg))
    return {
        "available": True, "source": src.name, "open": now.open,
        "title": now.title[:120], "artist": now.artist[:80], "playing": now.playing,
        "where": now.where, "why": "" if now.open else f"{src.name} isn't open.",
    }  # fmt: skip


def music_action(cfg: Config, action: str, backend: Any = None) -> dict[str, Any]:
    """Play, pause, next, previous (and "open") on the Music page: your source only. Returns
    {"ok", "text"}; "play" with nothing open starts it."""
    from personal_os.actions import music as m

    src = m.chosen(cfg.music.source)
    if src is None:
        ok = browser_media(cfg.runtime_dir, cfg.actions.browser, action)
        return {"ok": ok, "text": "" if ok else "The browser didn't answer."}
    b = backend or _music_backend(cfg)
    try:
        if action == "open":
            where = m.open_source(src, b, cfg.music.open_in)
            return {"ok": True, "text": f"Opened {src.name} in {where}."}
        r = m.control(src, b, action, open_in=cfg.music.open_in,
                      default_query=cfg.music.default_query)  # fmt: skip
    except Exception as exc:
        return {"ok": False, "text": f"Couldn't reach {src.name} ({type(exc).__name__})."}
    return {"ok": r.ok, "text": r.text}


def browser_music(runtime_dir: Path, browser: str) -> dict[str, Any]:
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


def browser_media(runtime_dir: Path, browser: str, action: str) -> bool:
    """Play, pause, next or previous in the browser's playing tab (music source "any")."""
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
