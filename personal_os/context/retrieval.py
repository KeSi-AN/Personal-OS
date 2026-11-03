"""Hybrid retrieval: full-text (events, page text, summaries) + vectors + recency (§6.2).

Each method ranks its own hits; the lists are fused by reciprocal rank (k=60), which needs no
score calibration between bm25 and cosine. A small recency list breaks near-ties toward newer
items. Without the embedding model the result says so and uses the other two.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from personal_os.context.embeddings import nearest
from personal_os.llm.provider import LLMError, LLMProvider
from personal_os.projects.features import STOPWORDS

RRF_K = 60
RECENCY_WEIGHT = 0.5
MIN_COSINE = 0.3
_QUESTION_WORDS = (
    "i me my mine we did do does done doing was were am is are be been have has had about on "
    "in at to of a an or it its which who whom whose where when what how why there their then "
    "than any some can could would should will may might shall into onto over last ago since "
    "between yesterday today week weeks day days month time times tell show find look looking "
    "working worked work research researching researched read reading left leave off"
)
QUESTION_WORDS = frozenset(_QUESTION_WORDS.split())
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+#-]*")


@dataclass
class Hit:
    kind: str  # "event" | "summary"
    id: str
    ts_ms: int
    score: float = 0.0
    via: list[str] = field(default_factory=list)


@dataclass
class SearchResult:
    hits: list[Hit]
    used_vectors: bool
    note: str = ""


def query_terms(text: str) -> list[str]:
    out = []
    for w in _WORD.findall(text):
        w = w.strip(".-+_").lower()
        if len(w) >= 2 and w not in STOPWORDS and w not in QUESTION_WORDS and w not in out:
            out.append(w)
    return out


def fts_query(terms: list[str]) -> str:
    """Natural-language questions: any term may match (OR), as quoted literals."""
    return " OR ".join('"' + t.replace('"', "") + '"' for t in terms)


def _filters(
    alias: str, start_ms: int | None, end_ms: int | None, project_id: str | None
) -> tuple[str, dict[str, Any]]:
    where: list[str] = []
    params: dict[str, Any] = {}
    if start_ms is not None:
        where.append(f"coalesce({alias}.ts_end, {alias}.ts_start) >= :start")
        params["start"] = start_ms
    if end_ms is not None:
        where.append(f"{alias}.ts_start < :end")
        params["end"] = end_ms
    if project_id is not None:
        where.append(f"{alias}.project_id = :project")
        params["project"] = project_id
    return "".join(f" AND {w}" for w in where), params


def fts_events(
    conn: sqlite3.Connection,
    terms: list[str],
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    project_id: str | None = None,
    sources: tuple[str, ...] | None = None,
    limit: int = 40,
) -> list[tuple[str, int]]:
    if not terms:
        return []
    extra, params = _filters("e", start_ms, end_ms, project_id)
    if sources:
        extra += f" AND e.source IN ({','.join(repr(s) for s in sources)})"
    params |= {"q": fts_query(terms), "limit": limit}
    rows = conn.execute(
        f"""
        WITH hits AS (
            SELECT e.rowid AS rid, bm25(events_fts) AS score
            FROM events_fts JOIN events e ON e.rowid = events_fts.rowid
            WHERE events_fts MATCH :q AND e.source NOT IN ('meta', 'system', 'idle') {extra}
            UNION ALL
            SELECT e.rowid AS rid, bm25(page_extracts_fts) AS score
            FROM page_extracts_fts
            JOIN page_extracts x ON x.rowid = page_extracts_fts.rowid
            JOIN events e ON e.id = x.event_id
            WHERE page_extracts_fts MATCH :q {extra}
        )
        SELECT e.id, e.ts_start FROM (SELECT rid, min(score) AS score FROM hits GROUP BY rid) b
        JOIN events e ON e.rowid = b.rid ORDER BY b.score LIMIT :limit
        """,
        params,
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def fts_summaries(
    conn: sqlite3.Connection,
    terms: list[str],
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    project_id: str | None = None,
    limit: int = 20,
) -> list[tuple[str, int]]:
    if not terms:
        return []
    extra, params = _filters("s", start_ms, end_ms, project_id)
    params |= {"q": fts_query(terms), "limit": limit}
    rows = conn.execute(
        f"SELECT s.id, s.ts_start FROM summaries_fts JOIN summaries s "
        f"ON s.rowid = summaries_fts.rowid WHERE summaries_fts MATCH :q AND s.status = 'ok' "
        f"{extra} ORDER BY bm25(summaries_fts) LIMIT :limit",
        params,
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def _vector_hits(
    conn: sqlite3.Connection,
    provider: LLMProvider,
    question: str,
    start_ms: int | None,
    end_ms: int | None,
    project_id: str | None,
    sources: tuple[str, ...] | None,
) -> list[tuple[str, str, int]]:
    qvec = provider.embed([question], query=True)[0]
    near = nearest(
        conn,
        qvec,
        provider.embed_model,
        start_ms=start_ms,
        end_ms=end_ms,
        limit=60,
        min_score=MIN_COSINE,
    )
    if not near:
        return []
    out = []
    for kind, item_id, _score in near:
        table = "events" if kind == "event" else "summaries"
        row = conn.execute(
            f"SELECT ts_start, project_id{', source' if kind == 'event' else ''} "
            f"FROM {table} WHERE id = ?",
            (item_id,),
        ).fetchone()
        if row is None or (project_id is not None and row[1] != project_id):
            continue
        if kind == "event" and sources and row[2] not in sources:
            continue
        out.append((kind, item_id, row[0]))
    return out[:30]


def hybrid_search(
    conn: sqlite3.Connection,
    provider: LLMProvider | None,
    question: str,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    project_id: str | None = None,
    sources: tuple[str, ...] | None = None,
    include_summaries: bool = True,
    limit: int = 15,
) -> SearchResult:
    terms = query_terms(question)
    ranked: dict[str, list[tuple[str, str, int]]] = {}
    ranked["text"] = [
        ("event", i, ts)
        for i, ts in fts_events(
            conn,
            terms,
            start_ms=start_ms,
            end_ms=end_ms,
            project_id=project_id,
            sources=sources,
        )
    ]
    if include_summaries:
        ranked["text"] += [
            ("summary", i, ts)
            for i, ts in fts_summaries(
                conn, terms, start_ms=start_ms, end_ms=end_ms, project_id=project_id
            )
        ]
    used_vectors, note = False, ""
    if provider is not None:
        try:
            vec = _vector_hits(conn, provider, question, start_ms, end_ms, project_id, sources)
            ranked["vector"] = [x for x in vec if include_summaries or x[0] == "event"]
            used_vectors = True
        except LLMError as exc:
            note = f"vector search unavailable ({str(exc)[:80]}); used full-text and recency only"
    else:
        note = "vector search off; used full-text and recency only"

    hits: dict[tuple[str, str], Hit] = {}
    for method, items in ranked.items():
        for rank, (kind, item_id, ts) in enumerate(items):
            hit = hits.setdefault((kind, item_id), Hit(kind, item_id, ts))
            hit.score += 1.0 / (RRF_K + rank)
            if method not in hit.via:
                hit.via.append(method)
    for rank, hit in enumerate(sorted(hits.values(), key=lambda h: -h.ts_ms)):
        hit.score += RECENCY_WEIGHT / (RRF_K + rank)
    best = sorted(hits.values(), key=lambda h: -h.score)[:limit]
    return SearchResult(best, used_vectors, note)
