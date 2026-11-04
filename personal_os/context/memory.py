"""Facts you told the assistant to remember (brief §8). Stored separately, editable, and only
ever added by you ("remember that …"), never inferred from captured content."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from personal_os.core.clock import Clock, from_ms, to_ms
from personal_os.core.ids import uuid7

MAX_FACT = 500
PROMPT_FACTS = 20
_REMEMBER = re.compile(r"^\s*(?:please\s+)?remember(?:\s+that)?\s*[:,-]?\s*(.+)$", re.I | re.S)


@dataclass
class Fact:
    id: str
    text: str
    source: str
    created: datetime
    updated: datetime


def parse_remember(text: str) -> str | None:
    """'remember that the staging db is on port 5433' -> the fact, else None."""
    m = _REMEMBER.match(text)
    if not m:
        return None
    fact = " ".join(m.group(1).split()).rstrip(".")
    return fact[:MAX_FACT] if fact else None


def add(conn: sqlite3.Connection, clock: Clock, text: str, source: str) -> Fact:
    text = " ".join(text.split())[:MAX_FACT]
    if not text:
        raise ValueError("empty fact")
    now = to_ms(clock.now())
    fid = str(uuid7(clock.now()))
    conn.execute(
        "INSERT INTO memory_facts (id, text, source, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (fid, text, source, now, now),
    )
    return Fact(fid, text, source, from_ms(now), from_ms(now))


def all_facts(conn: sqlite3.Connection) -> list[Fact]:
    return [
        Fact(r[0], r[1], r[2], from_ms(r[3]), from_ms(r[4]))
        for r in conn.execute(
            "SELECT id, text, source, created_at, updated_at FROM memory_facts ORDER BY created_at"
        )
    ]


def update(conn: sqlite3.Connection, clock: Clock, fact_id: str, text: str) -> bool:
    text = " ".join(text.split())[:MAX_FACT]
    if not text:
        return False
    return (
        conn.execute(
            "UPDATE memory_facts SET text = ?, updated_at = ? WHERE id = ?",
            (text, to_ms(clock.now()), fact_id),
        ).rowcount
        == 1
    )


def delete(conn: sqlite3.Connection, fact_id: str) -> bool:
    return conn.execute("DELETE FROM memory_facts WHERE id = ?", (fact_id,)).rowcount == 1


def find(conn: sqlite3.Connection, short: str) -> Fact | None:
    matches = [f for f in all_facts(conn) if f.id.endswith(short) or f.id.startswith(short)]
    return matches[0] if len(matches) == 1 else None


def prompt_lines(conn: sqlite3.Connection) -> list[str]:
    """The newest facts, for the model's context (facts you gave, so they're trusted)."""
    facts = all_facts(conn)[-PROMPT_FACTS:]
    return [f"You told me: {f.text}" for f in facts]
