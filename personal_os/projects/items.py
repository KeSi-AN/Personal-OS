"""Projects you curate (phase 9k): sources, notes and to-dos you attach yourself.

The tracker guesses which project a page belongs to; here you say so. A link you attach also
teaches the tracker (assignment rules): its GitHub repo, or its site when that site is
specific enough. Common sites (YouTube, Google, Stack Overflow...) never claim all their
pages for one project.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from personal_os.core.clock import Clock, from_ms, to_ms
from personal_os.core.ids import uuid7
from personal_os.projects.registry import Project, find_project, load_projects

Kind = Literal["link", "tab", "file", "note", "todo"]
SOURCE_KINDS = ("link", "tab", "file")
# Too general to say anything about a project: never learned as a rule.
GENERIC = {
    "google.com", "youtube.com", "music.youtube.com", "github.com", "stackoverflow.com",
    "reddit.com", "wikipedia.org", "en.wikipedia.org", "twitter.com", "x.com", "linkedin.com",
    "medium.com", "chatgpt.com", "claude.ai", "bing.com", "duckduckgo.com", "amazon.in",
    "amazon.com", "gmail.com", "mail.google.com", "docs.google.com", "drive.google.com",
    "notion.so", "web.whatsapp.com", "facebook.com", "instagram.com", "netflix.com",
}  # fmt: skip


@dataclass
class Item:
    id: str
    project_id: str | None
    kind: str
    title: str
    url: str
    body: str
    grp: str
    done: bool
    due: datetime | None
    source: str
    created: datetime


_COLS = "id, project_id, kind, title, url, body, grp, done, due_at, source, created_at"


def _item(r: sqlite3.Row | tuple) -> Item:  # type: ignore[type-arg]
    return Item(
        r[0], r[1], r[2], r[3], r[4], r[5], r[6], bool(r[7]),
        from_ms(r[8]) if r[8] is not None else None, r[9], from_ms(r[10]),
    )  # fmt: skip


def resolve(conn: sqlite3.Connection, name: str) -> Project | None:
    """A project by what you call it: its id, its name, a prefix, or close enough."""
    if not name.strip():
        return None
    p = find_project(conn, name.strip())
    if p is not None:
        return p
    from rapidfuzz import fuzz

    scored = sorted(
        ((max(fuzz.ratio(name.lower(), x.name.lower()), fuzz.ratio(name.lower(), x.id)), x)
         for x in load_projects(conn)),
        key=lambda s: -s[0],
    )  # fmt: skip
    return scored[0][1] if scored and scored[0][0] >= 80 else None


def add(
    conn: sqlite3.Connection,
    clock: Clock,
    project_id: str | None,
    kind: Kind,
    *,
    title: str = "",
    url: str = "",
    body: str = "",
    grp: str = "",
    due: datetime | None = None,
    source: str = "chat",
) -> str:
    """Add an item; a link already attached to this project is updated, not duplicated."""
    now = to_ms(clock.now())
    if kind in SOURCE_KINDS and url:
        row = conn.execute(
            "SELECT id FROM project_items WHERE project_id IS ? AND url = ? AND kind IN "
            "('link', 'tab', 'file')",
            (project_id, url),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE project_items SET title = COALESCE(NULLIF(?, ''), title), "
                "body = COALESCE(NULLIF(?, ''), body), grp = COALESCE(NULLIF(?, ''), grp), "
                "updated_at = ? WHERE id = ?",
                (title, body, grp, now, row[0]),
            )
            return str(row[0])
    iid = str(uuid7(clock.now()))
    conn.execute(
        "INSERT INTO project_items (id, project_id, kind, title, url, body, grp, due_at, "
        "source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (iid, project_id, kind, title[:300], url[:2000], body[:5000], grp[:80],
         to_ms(due) if due else None, source, now, now),
    )  # fmt: skip
    if project_id and kind in ("link", "tab") and url:
        learn_from(conn, clock, url, project_id)
    return iid


def rule_keys(url: str) -> list[tuple[str, str]]:
    """What a link teaches the tracker: its GitHub repo, or its site if it's specific."""
    u = urlparse(url)
    host = (u.hostname or "").lower().removeprefix("www.")
    parts = [p for p in u.path.split("/") if p]
    if host == "github.com" and len(parts) >= 2:
        return [("github", f"{parts[0]}/{parts[1]}".lower())]
    if host and host not in GENERIC and "." in host:
        return [("domain", host)]
    return []


def learn_from(conn: sqlite3.Connection, clock: Clock, url: str, project_id: str) -> None:
    from personal_os.projects.resolver import learn

    keys = rule_keys(url)
    if keys:
        learn(conn, clock, keys, project_id)


def items(
    conn: sqlite3.Connection,
    project_id: str | None,
    kinds: tuple[str, ...] = ("link", "tab", "file", "note", "todo"),
    *,
    open_only: bool = False,
    limit: int = 200,
) -> list[Item]:
    marks = ",".join("?" * len(kinds))
    sql = f"SELECT {_COLS} FROM project_items WHERE project_id IS ? AND kind IN ({marks})"
    if open_only:
        sql += " AND done = 0"
    sql += " ORDER BY done, created_at DESC LIMIT ?"
    return [_item(r) for r in conn.execute(sql, (project_id, *kinds, limit))]


def todos(conn: sqlite3.Connection, *, open_only: bool = True, limit: int = 50) -> list[Item]:
    """To-dos across all projects (and the inbox)."""
    sql = f"SELECT {_COLS} FROM project_items WHERE kind = 'todo'"
    if open_only:
        sql += " AND done = 0"
    sql += " ORDER BY COALESCE(due_at, 9e18), created_at DESC LIMIT ?"
    return [_item(r) for r in conn.execute(sql, (limit,))]


def find_todo(conn: sqlite3.Connection, words: str, project_id: str | None = None) -> Item | None:
    from rapidfuzz import fuzz

    pool = [t for t in todos(conn, open_only=True, limit=200) if project_id in (None, t.project_id)]
    if not words.strip():
        return pool[0] if pool else None
    scored = sorted(((fuzz.partial_ratio(words.lower(), t.title.lower()), t) for t in pool),
                    key=lambda s: -s[0])  # fmt: skip
    return scored[0][1] if scored and scored[0][0] >= 70 else None


def set_done(conn: sqlite3.Connection, clock: Clock, item_id: str, done: bool = True) -> bool:
    cur = conn.execute(
        "UPDATE project_items SET done = ?, updated_at = ? WHERE id = ? AND kind = 'todo'",
        (int(done), to_ms(clock.now()), item_id),
    )
    return cur.rowcount == 1


def update(conn: sqlite3.Connection, clock: Clock, item_id: str, **fields: object) -> bool:
    allowed = {"title", "body", "url", "project_id", "grp"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return False
    cols = ", ".join(f"{k} = ?" for k in sets)
    cur = conn.execute(
        f"UPDATE project_items SET {cols}, updated_at = ? WHERE id = ?",
        (*sets.values(), to_ms(clock.now()), item_id),
    )
    return cur.rowcount == 1


def delete(conn: sqlite3.Connection, item_id: str) -> bool:
    return conn.execute("DELETE FROM project_items WHERE id = ?", (item_id,)).rowcount == 1


def get(conn: sqlite3.Connection, item_id: str) -> Item | None:
    r = conn.execute(f"SELECT {_COLS} FROM project_items WHERE id = ?", (item_id,)).fetchone()
    return _item(r) if r else None


def counts(conn: sqlite3.Connection) -> dict[str | None, dict[str, int]]:
    """Per project: how many sources, notes and open to-dos."""
    out: dict[str | None, dict[str, int]] = {}
    for pid, kind, n in conn.execute(
        "SELECT project_id, CASE WHEN kind IN ('link','tab','file') THEN 'sources' "
        "WHEN kind = 'note' THEN 'notes' ELSE 'todos' END, COUNT(*) FROM project_items "
        "WHERE NOT (kind = 'todo' AND done = 1) GROUP BY 1, 2"
    ):
        out.setdefault(pid, {})[kind] = n
    return out


def sync_groups(conn: sqlite3.Connection, clock: Clock, tabs: list[dict[str, object]]) -> int:
    """A browser tab group named like a project keeps that project's sources up to date: its
    tabs are attached (source 'group'). Returns how many were added or refreshed."""
    projects = load_projects(conn)
    by_name = {p.name.lower(): p for p in projects} | {p.id.lower(): p for p in projects}
    n = 0
    for t in tabs:
        group = str(t.get("groupTitle") or "").strip()
        url = str(t.get("url") or "")
        p = by_name.get(group.lower()) if group else None
        if p is None or not url.startswith(("http://", "https://")):
            continue
        add(conn, clock, p.id, "tab", title=str(t.get("title") or ""), url=url, grp=group,
            source="group")  # fmt: skip
        n += 1
    return n
