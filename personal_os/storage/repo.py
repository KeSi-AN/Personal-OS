"""Read access, settings and counters."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any

from personal_os.core.clock import Clock, from_ms, to_ms
from personal_os.core.events import Event, PageExtract
from personal_os.storage.db import transaction

_EVENT_COLUMNS = (
    "e.id, e.source, e.type, e.ts_start, e.ts_end, e.last_seen, e.tz_offset_min, e.project_hint, "
    "e.confidence, e.privacy_class, e.retention, e.observer_version, e.truncated, e.payload"
)


def row_to_event(row: sqlite3.Row, extract: PageExtract | None = None) -> Event:
    payload = json.loads(row["payload"])
    if extract is not None:
        payload["extract"] = extract.model_dump()
    return Event.model_validate(
        {
            "id": row["id"],
            "source": row["source"],
            "type": row["type"],
            "ts_start": from_ms(row["ts_start"]),
            "ts_end": from_ms(row["ts_end"]) if row["ts_end"] is not None else None,
            "last_seen": from_ms(row["last_seen"]) if row["last_seen"] is not None else None,
            "tz_offset_min": row["tz_offset_min"],
            "project_hint": row["project_hint"],
            "confidence": row["confidence"],
            "privacy_class": row["privacy_class"],
            "retention": row["retention"],
            "observer_version": row["observer_version"],
            "truncated": bool(row["truncated"]),
            "payload": payload,
        }
    )


def _extract_from_row(row: sqlite3.Row) -> PageExtract | None:
    if row["x_event_id"] is None:
        return None
    return PageExtract(
        title=row["x_title"],
        meta_description=row["x_meta"],
        headings=json.loads(row["x_headings"]),
        main_text=row["x_text"],
        word_count=row["x_words"],
        content_hash=row["x_hash"],
        lang=row["x_lang"],
    )


_SELECT_WITH_EXTRACT = f"""
SELECT {_EVENT_COLUMNS},
       x.event_id AS x_event_id, x.title AS x_title, x.meta_description AS x_meta,
       x.headings AS x_headings, x.main_text AS x_text, x.word_count AS x_words,
       x.content_hash AS x_hash, x.lang AS x_lang
FROM events e LEFT JOIN page_extracts x ON x.event_id = e.id
"""


def get_event(conn: sqlite3.Connection, event_id: str) -> Event | None:
    row = conn.execute(_SELECT_WITH_EXTRACT + " WHERE e.id = ?", (event_id,)).fetchone()
    return row_to_event(row, _extract_from_row(row)) if row else None


def iter_events(
    conn: sqlite3.Connection,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    source: str | None = None,
    limit: int | None = None,
    newest_first: bool = False,
) -> Iterator[Event]:
    where: list[str] = []
    params: list[Any] = []
    if start is not None:
        where.append("coalesce(e.ts_end, e.last_seen, e.ts_start) >= ?")
        params.append(to_ms(start))
    if end is not None:
        where.append("e.ts_start < ?")
        params.append(to_ms(end))
    if source is not None:
        where.append("e.source = ?")
        params.append(source)
    sql = _SELECT_WITH_EXTRACT
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY e.ts_start " + ("DESC" if newest_first else "ASC") + ", e.id"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    for row in conn.execute(sql, params):
        yield row_to_event(row, _extract_from_row(row))


def search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[tuple[Event, float]]:
    """Full-text search over events and page extracts, best match first."""
    fts_query = _fts_query(query)
    if not fts_query:
        return []
    rows = conn.execute(
        f"""
        WITH hits AS (
            SELECT e.rowid AS rid, bm25(events_fts) AS score
            FROM events_fts JOIN events e ON e.rowid = events_fts.rowid
            WHERE events_fts MATCH :q
            UNION ALL
            SELECT e.rowid AS rid, bm25(page_extracts_fts) AS score
            FROM page_extracts_fts
            JOIN page_extracts x ON x.rowid = page_extracts_fts.rowid
            JOIN events e ON e.id = x.event_id
            WHERE page_extracts_fts MATCH :q
        ), best AS (SELECT rid, min(score) AS score FROM hits GROUP BY rid)
        {_SELECT_WITH_EXTRACT.replace("SELECT ", "SELECT best.score AS score, ", 1)}
        JOIN best ON best.rid = e.rowid
        ORDER BY best.score LIMIT :limit
        """,
        {"q": fts_query, "limit": limit},
    ).fetchall()
    return [(row_to_event(r, _extract_from_row(r)), float(r["score"])) for r in rows]


def _fts_query(text: str) -> str:
    """Treat user input as plain words (no FTS syntax): each word becomes a quoted term."""
    words = [w for w in "".join(c if c.isalnum() else " " for c in text).split() if w]
    return " ".join(f'"{w}"' for w in words)


# --- settings ------------------------------------------------------------------------------


def get_setting(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM kv_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: Any, clock: Clock) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO kv_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value), to_ms(clock.now())),
        )


def delete_setting(conn: sqlite3.Connection, key: str) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM kv_settings WHERE key = ?", (key,))


# --- counters ------------------------------------------------------------------------------


def add_counters(conn: sqlite3.Connection, day: str, scope: str, values: Mapping[str, int]) -> None:
    items = [(day, scope, str(name), int(v)) for name, v in values.items() if v]
    if not items:
        return
    with transaction(conn):
        conn.executemany(
            "INSERT INTO counters (day, scope, name, value) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (day, scope, name) DO UPDATE SET value = value + excluded.value",
            items,
        )


def get_counters(conn: sqlite3.Connection, day: str | None = None) -> dict[str, dict[str, int]]:
    sql = "SELECT scope, name, sum(value) AS v FROM counters"
    params: tuple[str, ...] = ()
    if day:
        sql += " WHERE day = ?"
        params = (day,)
    sql += " GROUP BY scope, name ORDER BY scope, name"
    out: dict[str, dict[str, int]] = {}
    for row in conn.execute(sql, params):
        out.setdefault(row["scope"], {})[row["name"]] = int(row["v"])
    return out
