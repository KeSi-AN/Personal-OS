"""Embedding vectors for pages, searches, commits and summaries (ADR-044).

Vectors are unit-length float32 rows in an ordinary table, searched by brute force: at personal
scale (thousands of items) that's fast enough, needs no native extension, and a deleted row is
zeroed on disk by secure_delete. Triggers delete a vector with its event, extract or summary.
Sensitive events are never embedded; open intervals wait until they close.
"""

from __future__ import annotations

import hashlib
import json
import operator
import sqlite3
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from personal_os.core.clock import Clock, to_ms
from personal_os.llm.provider import LLMProvider
from personal_os.storage.db import transaction

BATCH = 16
PAGE_TEXT_CHARS = 1500

_PENDING_EVENTS = """
SELECT e.id, e.source, e.type, e.ts_start, e.payload, x.meta_description AS x_meta,
       x.headings AS x_headings, x.main_text AS x_text, m.content_hash AS m_hash
FROM events e
LEFT JOIN page_extracts x ON x.event_id = e.id
LEFT JOIN embeddings m ON m.item_kind = 'event' AND m.item_id = e.id
WHERE e.privacy_class = 'normal' AND e.ts_end IS NOT NULL
  AND ((e.source = 'browser' AND e.type IN ('page_visit', 'search'))
       OR (e.source = 'git' AND e.type = 'commit'))
  AND (m.item_id IS NULL OR m.created_at < e.updated_at OR m.model != :model OR m.dims != :dims)
ORDER BY e.ts_start DESC LIMIT :limit
"""

_PENDING_SUMMARIES = """
SELECT s.id, s.ts_start, s.search_text, m.content_hash AS m_hash
FROM summaries s
LEFT JOIN embeddings m ON m.item_kind = 'summary' AND m.item_id = s.id
WHERE s.status = 'ok' AND s.search_text != ''
  AND (m.item_id IS NULL OR m.model != :model OR m.dims != :dims)
ORDER BY s.ts_start DESC LIMIT :limit
"""


def event_text(row: sqlite3.Row) -> str | None:
    """What a stored event contributes to vector search, or None."""
    p = json.loads(row["payload"])
    if row["source"] == "browser" and row["type"] == "page_visit":
        title = p.get("title") or ""
        if not title or title == "[redacted]":
            return None
        parts = [title, p.get("domain") or ""]
        if row["x_meta"]:
            parts.append(row["x_meta"])
        if row["x_headings"]:
            parts.append("; ".join(json.loads(row["x_headings"])[:8]))
        if row["x_text"]:
            parts.append(row["x_text"][:PAGE_TEXT_CHARS])
        return "\n".join(x for x in parts if x)
    if row["source"] == "browser" and row["type"] == "search":
        return f"Searched the web for: {p.get('text')}" if p.get("text") else None
    if row["source"] == "git" and row["type"] == "commit":
        repo = (p.get("repo_root") or "").replace("\\", "/").rsplit("/", 1)[-1]
        files = ", ".join((p.get("files") or [])[:10])
        return f"Commit in {repo}: {p.get('message') or ''}\nFiles: {files}"
    return None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def _pack(vec: Sequence[float]) -> bytes:
    return array("f", vec).tobytes()


def _unpack(blob: bytes) -> array[float]:
    a = array("f")
    a.frombytes(blob)
    return a


@dataclass
class _Item:
    kind: str
    id: str
    ts: int
    text: str
    old_hash: str | None


def pending(conn: sqlite3.Connection, model: str, dims: int, limit: int) -> list[_Item]:
    params = {"model": model, "dims": dims, "limit": limit}
    items = []
    for r in conn.execute(_PENDING_SUMMARIES, params):
        items.append(_Item("summary", r["id"], r["ts_start"], r["search_text"], r["m_hash"]))
    for r in conn.execute(_PENDING_EVENTS, params):
        text = event_text(r)
        if text:
            items.append(_Item("event", r["id"], r["ts_start"], text, r["m_hash"]))
    return items[:limit]


def embed_pending(
    conn: sqlite3.Connection,
    provider: LLMProvider,
    clock: Clock,
    dims: int,
    *,
    limit: int = 256,
    should_stop: Callable[[], bool] = lambda: False,
) -> int:
    """Embed what's missing or stale, newest first, in small batches. Returns how many."""
    items = pending(conn, provider.embed_model, dims, limit)
    done = 0
    for i in range(0, len(items), BATCH):
        if should_stop():
            break
        batch = items[i : i + BATCH]
        unchanged = [x for x in batch if x.old_hash == _hash(x.text)]
        fresh = [x for x in batch if x.old_hash != _hash(x.text)]
        vectors = provider.embed([x.text for x in fresh]) if fresh else []
        now = to_ms(clock.now())
        with transaction(conn):
            for x in unchanged:  # same text, only the event row was touched: keep the vector
                conn.execute(
                    "UPDATE embeddings SET created_at = ?, model = ?, dims = ? "
                    "WHERE item_kind = ? AND item_id = ?",
                    (now, provider.embed_model, dims, x.kind, x.id),
                )
            for x, vec in zip(fresh, vectors, strict=True):
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings (item_kind, item_id, model, dims, "
                    "content_hash, vector, ts, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        x.kind,
                        x.id,
                        provider.embed_model,
                        len(vec),
                        _hash(x.text),
                        _pack(vec),
                        x.ts,
                        now,
                    ),
                )
        done += len(batch)
    return done


def count_pending(conn: sqlite3.Connection, model: str, dims: int) -> int:
    return len(pending(conn, model, dims, 100_000))


def nearest(
    conn: sqlite3.Connection,
    query_vec: Sequence[float],
    model: str,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    limit: int = 30,
    min_score: float = 0.0,
) -> list[tuple[str, str, float]]:
    """(item_kind, item_id, cosine) of the closest vectors, best first."""
    q = array("f", query_vec)
    where = ["model = ?", "dims = ?"]
    params: list[Any] = [model, len(q)]
    if start_ms is not None:
        where.append("ts >= ?")
        params.append(start_ms)
    if end_ms is not None:
        where.append("ts < ?")
        params.append(end_ms)
    scored = []
    mul = operator.mul
    for kind, item_id, blob in conn.execute(
        f"SELECT item_kind, item_id, vector FROM embeddings WHERE {' AND '.join(where)}", params
    ):
        score = sum(map(mul, q, _unpack(blob)))
        if score >= min_score:
            scored.append((kind, item_id, score))
    scored.sort(key=lambda x: -x[2])
    return scored[:limit]
