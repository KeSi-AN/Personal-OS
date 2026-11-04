"""The append-only action audit log (DESIGN.md §6.3). One row per stage; never updated."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from personal_os.core.clock import Clock, from_ms, to_ms

DETAIL_MAX = 2000


@dataclass
class AuditEntry:
    action_id: str
    ts: datetime
    stage: str
    tool: str
    tier: int
    origin: str
    tainted: bool
    request: str | None
    args: dict[str, Any]
    detail: str
    undo: dict[str, Any] | None
    undo_of: str | None


class Audit:
    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self.conn = conn
        self.clock = clock

    def record(
        self,
        action_id: str,
        stage: str,
        *,
        tool: str,
        tier: int,
        origin: str,
        tainted: bool,
        args: dict[str, Any],
        request: str | None = None,
        detail: str = "",
        undo: dict[str, Any] | None = None,
        undo_of: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO action_audit (action_id, ts, stage, tool, tier, origin, tainted, request, "
            "args, detail, undo, undo_of) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action_id,
                to_ms(self.clock.now()),
                stage,
                tool,
                tier,
                origin,
                int(tainted),
                request,
                json.dumps(args, default=str),
                detail[:DETAIL_MAX],
                json.dumps(undo) if undo else None,
                undo_of,
            ),
        )

    def recent_count(self, since_ms: int, *, min_tier: int = 0) -> int:
        """Actions that started since `since_ms` (for rate limits; shared by every process)."""
        row = self.conn.execute(
            "SELECT count(*) FROM action_audit WHERE stage = 'started' AND ts >= ? AND tier >= ?",
            (since_ms, min_tier),
        ).fetchone()
        return int(row[0])


def _entry(r: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        r["action_id"],
        from_ms(r["ts"]),
        r["stage"],
        r["tool"],
        r["tier"],
        r["origin"],
        bool(r["tainted"]),
        r["request"],
        json.loads(r["args"]),
        r["detail"],
        json.loads(r["undo"]) if r["undo"] else None,
        r["undo_of"],
    )


def history(conn: sqlite3.Connection, action_id: str) -> list[AuditEntry]:
    rows = conn.execute(
        "SELECT * FROM action_audit WHERE action_id = ? ORDER BY seq", (action_id,)
    ).fetchall()
    return [_entry(r) for r in rows]


def find_action(conn: sqlite3.Connection, short: str) -> str | None:
    """By the short id shown (the id's last characters, which are random) or a prefix."""
    rows = conn.execute(
        "SELECT DISTINCT action_id FROM action_audit WHERE action_id LIKE ? OR action_id LIKE ? "
        "LIMIT 2",
        ("%" + short, short + "%"),
    ).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def latest(conn: sqlite3.Connection, limit: int = 20) -> list[list[AuditEntry]]:
    """The newest actions, each as its list of stages."""
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT action_id FROM action_audit GROUP BY action_id ORDER BY max(seq) DESC LIMIT ?",
            (limit,),
        )
    ]
    return [history(conn, i) for i in ids]


def forget_window(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> int:
    """ADR-011: keep who-did-what-when, replace what was captured in the arguments."""
    return conn.execute(
        "UPDATE action_audit SET args = '{\"forgotten\":true}', detail = '[forgotten]', "
        "request = NULL, undo = NULL WHERE ts >= ? AND ts < ? AND args != '{\"forgotten\":true}'",
        (start_ms, end_ms),
    ).rowcount
