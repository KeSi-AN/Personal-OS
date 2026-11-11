"""Research worth keeping (phase 10): what you studied that isn't part of a project yet.

Learning time (tutorials, talks, docs, AI chats; see context/activity.py) that no project
claims is grouped by topic. A topic with enough of your time becomes an "R&D: <topic>" project,
with the videos and pages attached as sources (marked 'auto'), so it can be picked up later,
renamed, or merged into a real project. An existing project or R&D project on the same topic
gets the new links instead of a duplicate.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from personal_os.core.clock import Clock
from personal_os.projects import items
from personal_os.projects.registry import add_project, load_projects

MIN_MS = 10 * 60_000  # this much learning on a topic is worth keeping
MIN_ITEMS = 2  # ... or this many separate videos/pages about it
PREFIX = "R&D: "


@dataclass
class Kept:
    project: str
    created: bool
    added: int
    minutes: int


TOPIC_SYSTEM = """Give each numbered page or video title a short topic (2-4 words) naming what
is being learned ("Claude agents", "React hooks", "3D printing"). Titles about the same thing
must get exactly the same topic. Reply with JSON only."""


def topics_for(provider: Any, titles: list[str]) -> dict[str, str]:
    """title -> topic, from the local model (at most 30 titles); {} if it can't."""

    from pydantic import BaseModel

    todo = list(dict.fromkeys(t for t in titles if t))[:30]
    if not todo or provider is None:
        return {}

    class Item(BaseModel):
        i: int
        topic: str

    class Out(BaseModel):
        items: list[Item]

    user = "\n".join(f"{n}. {t[:160]}" for n, t in enumerate(todo, start=1))
    try:
        got = provider.chat_json(TOPIC_SYSTEM, user, Out, temperature=0, max_tokens=40 * len(todo))
    except Exception:
        return {}
    return {
        todo[x.i - 1]: x.topic.strip()[:40]
        for x in got.items
        if 1 <= x.i <= len(todo) and x.topic.strip()
    }


def _match(conn: sqlite3.Connection, topic: str) -> str | None:
    """An existing project about this topic, by name or keywords."""
    from rapidfuzz import fuzz

    t = topic.lower()
    best: str | None = None
    score = 0.0
    for p in load_projects(conn):
        if p.id == "none":
            continue
        name = p.name.lower().removeprefix(PREFIX.lower())
        s = max([fuzz.token_set_ratio(t, name), *(fuzz.ratio(t, k.lower()) for k in p.keywords)])
        if s > score:
            best, score = p.id, s
    return best if score >= 85 else None


def keep_research(
    conn: sqlite3.Connection, clock: Clock, research: list[dict[str, Any]], provider: Any = None
) -> list[Kept]:
    """File unclaimed learning into R&D projects. `research` is activity.Breakdown.research."""
    free = [r for r in research if not r.get("project") or r.get("project") == "none"]
    missing = [r["title"] for r in free if not r.get("topic")]
    named = topics_for(provider, missing) if missing else {}
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in free:
        topic = (r.get("topic") or named.get(r["title"]) or "").strip()
        if topic:
            by_topic[topic.title()].append(r)
    out: list[Kept] = []
    for topic, rs in sorted(by_topic.items(), key=lambda kv: -sum(r["ms"] for r in kv[1])):
        ms = sum(r["ms"] for r in rs)
        if ms < MIN_MS and len({r["url"] for r in rs}) < MIN_ITEMS:
            continue
        pid = _match(conn, topic)
        created = pid is None
        if pid is None:
            pid = add_project(conn, clock, f"{PREFIX}{topic}", [], [topic.lower()]).id
        before = len(items.items(conn, pid, ("link", "tab")))
        for r in sorted(rs, key=lambda r: -r["ms"])[:10]:
            items.add(conn, clock, pid, "link", url=r["url"], title=r["title"], source="auto",
                      body=f"{r['ms'] // 60_000} min of learning")  # fmt: skip
        added = len(items.items(conn, pid, ("link", "tab"))) - before
        if created or added:
            name = next(p.name for p in load_projects(conn) if p.id == pid)
            out.append(Kept(name, created, added, ms // 60_000))
    return out
