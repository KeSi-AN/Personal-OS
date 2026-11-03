"""Forget, retention, recovery of open intervals, inspection and wipe (DESIGN.md §4.10, §5.4)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from personal_os.core.clock import Clock, to_ms
from personal_os.core.events import Event, MetaEvent
from personal_os.storage.db import transaction

MAINTENANCE_VERSION = "0.1.0"


@dataclass(frozen=True)
class ForgetReport:
    start: datetime
    end: datetime
    events: int
    page_extracts: int


def forget(
    conn: sqlite3.Connection, start: datetime, end: datetime, clock: Clock, *, marker: bool = True
) -> ForgetReport:
    """Delete everything captured that overlaps [start, end), then leave a content-free marker.

    Open intervals count as lasting until now. With secure_delete and FTS secure-delete on,
    the deleted text is overwritten in the database file, and the WAL is truncated afterwards.
    The running daemon watches for the marker, purges its queue and observers' open intervals,
    and repeats the delete once (marker=False) to catch a checkpoint written in between.
    """
    if end <= start:
        raise ValueError("forget window must have end > start")
    now_ms = to_ms(clock.now())
    params = {"start": to_ms(start), "end": to_ms(end), "now": now_ms}
    overlap = "ts_start < :end AND coalesce(ts_end, :now) >= :start AND source != 'meta'"
    with transaction(conn):
        n_extracts = conn.execute(
            f"SELECT count(*) FROM page_extracts WHERE event_id IN "
            f"(SELECT id FROM events WHERE {overlap})",
            params,
        ).fetchone()[0]
        n_events = conn.execute(f"DELETE FROM events WHERE {overlap}", params).rowcount
        conn.execute("DELETE FROM sessions WHERE ts_start < :end AND ts_end >= :start", params)
        # Summaries are derived from what's being forgotten; their vectors go with them (trigger).
        conn.execute("DELETE FROM summaries WHERE ts_start < :end AND ts_end >= :start", params)
        if marker:
            event = Event.point(
                MetaEvent(kind="meta.forget", range_start=start, range_end=end),
                clock.now(),
                observer_version=MAINTENANCE_VERSION,
            )
            conn.execute(
                "INSERT INTO events (id, source, type, ts_start, ts_end, privacy_class, retention, "
                "observer_version, payload, inserted_at, updated_at) "
                "VALUES (?, 'meta', 'forget', ?, ?, 'normal', 'summary', ?, ?, ?, ?)",
                (
                    str(event.id),
                    now_ms,
                    now_ms,
                    MAINTENANCE_VERSION,
                    event.payload.model_dump_json(),
                    now_ms,
                    now_ms,
                ),
            )
    checkpoint(conn)
    return ForgetReport(start, end, n_events, int(n_extracts))


def purge_expired(conn: sqlite3.Connection, now: datetime) -> dict[str, int]:
    now_ms = to_ms(now)
    with transaction(conn):
        extracts = conn.execute(
            "DELETE FROM page_extracts WHERE expires_at IS NOT NULL AND expires_at < ?", (now_ms,)
        ).rowcount
        events = conn.execute(
            "DELETE FROM events WHERE expires_at IS NOT NULL AND expires_at < ?", (now_ms,)
        ).rowcount
    conn.execute("PRAGMA incremental_vacuum")
    checkpoint(conn)
    return {"events": events, "page_extracts": extracts}


def close_open_intervals(conn: sqlite3.Connection) -> int:
    """At daemon start: intervals left open by a previous run end at their last checkpoint."""
    with transaction(conn):
        return conn.execute(
            "UPDATE events SET ts_end = coalesce(last_seen, ts_start), truncated = 1 "
            "WHERE ts_end IS NULL"
        ).rowcount


def close_stale(conn: sqlite3.Connection, source: str, older_than: datetime) -> int:
    """End open intervals of `source` whose producer stopped re-sending them (the browser
    extension re-sends open visits every minute) at their last checkpoint."""
    with transaction(conn):
        return conn.execute(
            "UPDATE events SET ts_end = coalesce(last_seen, ts_start), truncated = 1 "
            "WHERE source = ? AND ts_end IS NULL AND coalesce(last_seen, ts_start) < ?",
            (source, to_ms(older_than)),
        ).rowcount


def close_open_at(conn: sqlite3.Connection, at: datetime) -> int:
    """On pause: end every open interval now (observers drop them from memory)."""
    with transaction(conn):
        return conn.execute(
            "UPDATE events SET ts_end = max(ts_start, ?) WHERE ts_end IS NULL", (to_ms(at),)
        ).rowcount


def forget_markers_since(
    conn: sqlite3.Connection, after_rowid: int
) -> list[tuple[int, datetime, datetime]]:
    """meta.forget markers newer than `after_rowid`: (rowid, start, end)."""
    out = []
    for row in conn.execute(
        "SELECT rowid, payload FROM events WHERE source = 'meta' AND type = 'forget' AND rowid > ? "
        "ORDER BY rowid",
        (after_rowid,),
    ):
        p = MetaEvent.model_validate_json(row[1])
        if p.range_start and p.range_end:
            out.append((int(row[0]), p.range_start, p.range_end))
    return out


def max_rowid(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT coalesce(max(rowid), 0) FROM events").fetchone()[0])


def checkpoint(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


@dataclass(frozen=True)
class Inspection:
    schema_version: int
    db_bytes: int
    wal_bytes: int
    tables: dict[str, int]
    by_source: list[tuple[str, str, int, int]]  # source, privacy_class, rows, payload bytes
    oldest_ms: int | None
    newest_ms: int | None


def inspect(conn: sqlite3.Connection, db_path: Path | None) -> Inspection:
    tables = {
        name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        for name in (
            "events",
            "page_extracts",
            "projects",
            "assignment_rules",
            "sessions",
            "summaries",
            "embeddings",
            "counters",
            "kv_settings",
        )
    }
    by_source = [
        (r[0], r[1], r[2], r[3] or 0)
        for r in conn.execute(
            "SELECT source, privacy_class, count(*), sum(length(payload) + length(search_text)) "
            "FROM events GROUP BY source, privacy_class ORDER BY source, privacy_class"
        )
    ]
    oldest, newest = conn.execute("SELECT min(ts_start), max(ts_start) FROM events").fetchone()
    db_bytes = db_path.stat().st_size if db_path and db_path.exists() else 0
    wal = db_path.with_name(db_path.name + "-wal") if db_path else None
    wal_bytes = wal.stat().st_size if wal and wal.exists() else 0
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    return Inspection(version, db_bytes, wal_bytes, tables, by_source, oldest, newest)


def wipe(db_path: Path) -> list[Path]:
    """Delete the database and its WAL/SHM files. The caller must have closed all connections."""
    removed = []
    for suffix in ("", "-wal", "-shm"):
        p = db_path.with_name(db_path.name + suffix)
        if p.exists():
            p.unlink()
            removed.append(p)
    return removed
