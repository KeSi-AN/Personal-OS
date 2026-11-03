"""The unassigned inbox, and corrections that teach the resolver (DESIGN.md §6.1)."""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from personal_os.core.clock import Clock, to_ms
from personal_os.projects.features import BROWSER_EXES, build
from personal_os.projects.resolver import _SELECT, Resolver, learn
from personal_os.storage.db import transaction

MIN_WINDOW_S = 30  # shorter window glimpses aren't worth your attention
GUESS_BELOW = 0.7  # weak guesses (temporal, keyword) are shown for review too


@dataclass
class InboxGroup:
    kind: str
    key: str
    count: int = 0
    seconds: float = 0.0
    samples: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    guesses: Counter[str] = field(default_factory=Counter)  # current weak guess per item

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.key}"


def inbox(conn: sqlite3.Connection, clock: Clock, days: int = 7) -> list[InboxGroup]:
    """Unassigned items worth a decision, grouped so one correction fixes many."""
    since = to_ms(clock.now() - timedelta(days=days))
    rows = conn.execute(
        _SELECT + "WHERE (e.assign_source = 'unassigned' OR (e.assign_source IN "
        "('temporal', 'keyword') AND e.project_confidence < ?)) AND e.ts_start >= ? "
        "AND (e.source IN ('terminal', 'window') "
        "OR (e.source = 'browser' AND e.type = 'page_visit') "
        "OR (e.source = 'fs' AND e.type = 'change')) ORDER BY e.ts_start",
        (GUESS_BELOW, since),
    ).fetchall()
    groups: dict[tuple[str, str], InboxGroup] = {}
    for row in rows:
        f = build(row, row["x_title"], row["x_headings"])
        if f.source == "window" and f.duration_s < MIN_WINDOW_S:
            continue
        if f.source == "window" and (f.exe or "") in BROWSER_EXES and _has_page_visit(conn, row):
            continue  # it follows its page visit, which is listed (or assigned) itself
        kind, key = f.inbox_key()
        g = groups.setdefault((kind, key), InboxGroup(kind, key))
        g.count += 1
        g.seconds += f.duration_s if f.source in ("window", "browser") else 0
        g.event_ids.append(f.id)
        g.guesses[f.project_id or "-"] += 1
        sample = f.title or (f.paths[0][0] if f.paths else "")
        if sample and sample not in g.samples and len(g.samples) < 3:
            g.samples.append(sample[:70])
    return sorted(groups.values(), key=lambda g: (-g.seconds, -g.count))


def _has_page_visit(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Does a stored page visit cover this browser window? (No: browser without the extension,
    such as Brave before it's connected; then the window itself needs a decision.)"""
    end = row["ts_end"] or row["last_seen"] or row["ts_start"]
    return bool(
        conn.execute(
            "SELECT 1 FROM events WHERE source = 'browser' AND type = 'page_visit' "
            "AND ts_start <= ? AND coalesce(ts_end, last_seen, ts_start) >= ? LIMIT 1",
            (end, row["ts_start"]),
        ).fetchone()
    )


def _set_user(
    conn: sqlite3.Connection, clock: Clock, event_ids: list[str], project_id: str
) -> None:
    now = to_ms(clock.now())
    with transaction(conn):
        conn.executemany(
            "UPDATE events SET project_id = ?, project_confidence = 1.0, assign_source = 'user', "
            "assigned_at = ? WHERE id = ?",
            [(project_id, now, eid) for eid in event_ids],
        )


def assign_group(conn: sqlite3.Connection, clock: Clock, group: InboxGroup, project_id: str) -> int:
    """Assign every item in the group, learn the group's key, and re-resolve so the rule
    applies at once to everything else (and to future events)."""
    _set_user(conn, clock, group.event_ids, project_id)
    learn(conn, clock, [(group.kind, group.key)], project_id)
    Resolver(conn, clock).resolve_all(since=clock.now() - timedelta(days=30))
    return len(group.event_ids)


def assign_event(
    conn: sqlite3.Connection,
    clock: Clock,
    event_id: str,
    project_id: str,
    *,
    learn_rule: bool = False,
) -> None:
    """Correct one event. By default it teaches nothing (one Stack Overflow page says little
    about the next); `learn_rule` stores its most specific key as a rule."""
    row = conn.execute(_SELECT + "WHERE e.id = ?", (event_id,)).fetchone()
    if row is None:
        raise KeyError(event_id)
    _set_user(conn, clock, [event_id], project_id)
    if learn_rule:
        keys = build(row, row["x_title"], row["x_headings"]).rule_keys()[:1]
        learn(conn, clock, keys, project_id)
    Resolver(conn, clock).resolve_all(since=clock.now() - timedelta(days=30))
