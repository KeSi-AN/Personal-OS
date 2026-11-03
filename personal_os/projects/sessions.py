"""Work sessions: contiguous blocks of activity on one project (DESIGN.md §6.1).

The range is cut into one-minute buckets. Each bucket gets the project with the most time in
it (window time by the window's resolved project; a little for each other event). Fifteen
quiet minutes end a session; inside a block, project runs shorter than ten minutes are
absorbed by their neighbours, so a short detour doesn't split a coding session.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from personal_os.core.clock import Clock, from_ms, to_ms
from personal_os.storage.db import transaction

BUCKET_MS = 60_000
GAP_BUCKETS = 15
MIN_RUN_BUCKETS = 10
POINT_WEIGHT_MS = 10_000  # a commit, command or file save counts like 10 s of attention
UNKNOWN = "?"


@dataclass
class Session:
    start: datetime
    end: datetime
    project_id: str | None
    confidence: float
    active_ms: int
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return hashlib.sha1(f"{to_ms(self.start)}".encode()).hexdigest()[:16]


def _rows(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT source, type, ts_start, coalesce(ts_end, last_seen, ts_start) AS ts_stop, "
        "project_id, project_confidence, payload FROM events "
        "WHERE ts_start < ? AND coalesce(ts_end, last_seen, ts_start) >= ? AND source != 'meta' "
        "ORDER BY ts_start",
        (end_ms, start_ms),
    ).fetchall()


def build_sessions(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[Session]:
    start_ms = to_ms(start) // BUCKET_MS * BUCKET_MS
    end_ms = to_ms(end)
    n = max(0, (end_ms - start_ms) // BUCKET_MS + 1)
    weight: list[Counter[str]] = [Counter() for _ in range(n)]
    rows = _rows(conn, start_ms, end_ms)
    for r in rows:
        if r["source"] in ("system", "idle"):
            continue
        pid = r["project_id"] or UNKNOWN
        a, b = max(r["ts_start"], start_ms), min(r["ts_stop"], end_ms)
        if r["source"] == "window" and b > a:
            for i in range(
                (a - start_ms) // BUCKET_MS, min(n - 1, (b - start_ms) // BUCKET_MS) + 1
            ):
                lo = start_ms + i * BUCKET_MS
                overlap = min(b, lo + BUCKET_MS) - max(a, lo)
                if overlap > 0:
                    weight[i][pid] += overlap
        elif r["source"] in ("browser", "terminal", "git", "fs", "editor"):
            i = (r["ts_start"] - start_ms) // BUCKET_MS
            if 0 <= i < n:
                weight[i][pid] += POINT_WEIGHT_MS
    labels: list[str | None] = [w.most_common(1)[0][0] if w else None for w in weight]

    sessions: list[Session] = []
    for block in _blocks(labels):
        for run_start, run_end, pid in _smooth(labels, block):
            s = _session(rows, weight, start_ms, run_start, run_end, pid)
            if s.active_ms >= 60_000:
                sessions.append(s)
    return sessions


def _blocks(labels: list[str | None]) -> list[tuple[int, int]]:
    """Index ranges [a, b) of activity separated by >= GAP_BUCKETS empty buckets."""
    blocks, a, quiet = [], None, 0
    for i, label in enumerate(labels):
        if label is None:
            quiet += 1
            if a is not None and quiet >= GAP_BUCKETS:
                blocks.append((a, i - quiet + 1))
                a = None
        else:
            if a is None:
                a = i
            quiet = 0
    if a is not None:
        last = max(i for i, x in enumerate(labels) if x is not None)
        blocks.append((a, last + 1))
    return blocks


def _smooth(labels: list[str | None], block: tuple[int, int]) -> list[tuple[int, int, str]]:
    a, b = block
    runs: list[list[Any]] = []  # [start, end, label]
    for i in range(a, b):
        label = labels[i] or UNKNOWN
        if runs and runs[-1][2] == label:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1, label])
    # Absorb short runs and unknown stretches into a neighbour, shortest first, until stable.
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for idx, (s, e, label) in enumerate(runs):
            if label != UNKNOWN and e - s >= MIN_RUN_BUCKETS:
                continue
            prev = runs[idx - 1] if idx > 0 else None
            nxt = runs[idx + 1] if idx + 1 < len(runs) else None
            target = max(
                (r for r in (prev, nxt) if r), key=lambda r: (r[2] != UNKNOWN, r[1] - r[0])
            )
            if target[2] == label:
                continue
            target[0], target[1] = min(target[0], s), max(target[1], e)
            runs.pop(idx)
            merged: list[list[Any]] = []
            for r in runs:
                if merged and merged[-1][2] == r[2]:
                    merged[-1][1] = r[1]
                else:
                    merged.append(r)
            runs = merged
            changed = True
            break
    return [(s, e, label) for s, e, label in runs]


def _session(
    rows: list[sqlite3.Row], weight: list[Counter[str]], base: int, a: int, b: int, label: str
) -> Session:
    lo, hi = base + a * BUCKET_MS, base + b * BUCKET_MS
    total: Counter[str] = Counter()
    for w in weight[a:b]:
        total.update(w)
    active = sum(total.values())
    pid = None if label == UNKNOWN else label
    confidence = round(total[label] / active, 3) if active else 0.0
    apps: Counter[str] = Counter()
    files: Counter[str] = Counter()
    pages: dict[str, int] = {}
    commands = failed = 0
    commits: list[str] = []
    first = last = None
    for r in rows:
        if not (lo <= r["ts_start"] < hi) and not (r["ts_start"] < lo < r["ts_stop"]):
            continue
        if r["source"] in ("system", "idle"):
            continue
        first = r["ts_start"] if first is None else min(first, r["ts_start"])
        last = r["ts_stop"] if last is None else max(last, r["ts_stop"])
        p = json.loads(r["payload"])
        if r["source"] == "window":
            apps[p.get("app") or p.get("exe") or "?"] += min(r["ts_stop"], hi) - max(
                r["ts_start"], lo
            )
        elif r["source"] == "fs" and p.get("path"):
            files[PurePosixPath(p["path"].replace("\\", "/")).name] += 1
        elif r["source"] == "browser" and r["type"] == "page_visit":
            title = p.get("title") or p.get("domain") or ""
            pages[title] = pages.get(title, 0) + int(p.get("active_dwell_ms") or 0)
        elif r["source"] == "terminal":
            commands += 1
            failed += p.get("success") is False
        elif r["source"] == "git" and r["type"] == "commit":
            commits.append((p.get("message") or "").splitlines()[0][:120])
    stats = {
        "apps": {k: v // 1000 for k, v in apps.most_common(8)},
        "files": dict(files.most_common(10)),
        "pages": [t for t, _ in sorted(pages.items(), key=lambda kv: -kv[1])[:10]],
        "commands": commands,
        "failed_commands": failed,
        "commits": commits,
        "by_project_s": {k: v // 1000 for k, v in total.most_common(6)},
    }
    start = from_ms(max(lo, first or lo))
    end = from_ms(min(hi, last or hi))
    return Session(start, end, pid, confidence, active, stats)


def rebuild_sessions(
    conn: sqlite3.Connection, clock: Clock, start: datetime, end: datetime
) -> list[Session]:
    """Replace stored sessions overlapping [start, end) with freshly built ones."""
    sessions = build_sessions(conn, start, end)
    now = to_ms(clock.now())
    with transaction(conn):
        conn.execute(
            "DELETE FROM sessions WHERE ts_start < ? AND ts_end >= ?", (to_ms(end), to_ms(start))
        )
        conn.executemany(
            "INSERT OR REPLACE INTO sessions (id, ts_start, ts_end, project_id, confidence, "
            "active_ms, stats, built_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    s.id,
                    to_ms(s.start),
                    to_ms(s.end),
                    s.project_id,
                    s.confidence,
                    s.active_ms,
                    json.dumps(s.stats),
                    now,
                )
                for s in sessions
            ],
        )
    return sessions


def load_sessions(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[Session]:
    return [
        Session(from_ms(a), from_ms(b), pid, conf, active, json.loads(stats))
        for a, b, pid, conf, active, stats in conn.execute(
            "SELECT ts_start, ts_end, project_id, confidence, active_ms, stats FROM sessions "
            "WHERE ts_start < ? AND ts_end >= ? ORDER BY ts_start",
            (to_ms(end), to_ms(start)),
        )
    ]


def day_bounds(day: datetime) -> tuple[datetime, datetime]:
    local = day.astimezone()
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)
