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


def forget(conn: sqlite3.Connection, start: datetime, end: datetime, clock: Clock) -> ForgetReport:
    """Delete everything captured that overlaps [start, end), then leave a content-free marker.

    Open intervals count as lasting until now. With secure_delete and FTS secure-delete on,
    the deleted text is overwritten in the database file, and the WAL is truncated afterwards.
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
        marker = Event.point(
            MetaEvent(kind="meta.forget", range_start=start, range_end=end),
            clock.now(),
            observer_version=MAINTENANCE_VERSION,
        )
        conn.execute(
            "INSERT INTO events (id, source, type, ts_start, ts_end, privacy_class, retention, "
            "observer_version, payload, inserted_at, updated_at) "
            "VALUES (?, 'meta', 'forget', ?, ?, 'normal', 'summary', ?, ?, ?, ?)",
            (
                str(marker.id),
                now_ms,
                now_ms,
                MAINTENANCE_VERSION,
                marker.payload.model_dump_json(),
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
        for name in ("events", "page_extracts", "counters", "kv_settings")
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
