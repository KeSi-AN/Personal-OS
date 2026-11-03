"""Background LLM work: summarise finished sessions, then embed what's missing (§6.2).

Runs only while the job gate is open, one item at a time, and stops as soon as `should_stop`
says so (you came back, capture was paused, the daemon is stopping).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from personal_os.config.schema import LLMConfig
from personal_os.context.embeddings import embed_pending
from personal_os.context.summarizer import pending_sessions, summarize_session
from personal_os.core.clock import Clock
from personal_os.llm.provider import LLMProvider


@dataclass
class JobReport:
    summarized: int = 0
    failed: int = 0
    embedded: int = 0
    stopped_early: bool = False


def run_llm_jobs(
    conn: sqlite3.Connection,
    provider: LLMProvider,
    clock: Clock,
    cfg: LLMConfig,
    should_stop: Callable[[], bool],
    *,
    max_sessions: int = 5,
    max_embeddings: int = 256,
) -> JobReport:
    """LLMUnavailable propagates (the caller backs off); other model errors are per item."""
    report = JobReport()
    min_active = cfg.summarize_min_active_s * 1000
    for session, pack in pending_sessions(conn, clock, min_active_ms=min_active)[:max_sessions]:
        if should_stop():
            report.stopped_early = True
            return report
        if summarize_session(conn, provider, clock, session, pack=pack) is None:
            report.failed += 1
        else:
            report.summarized += 1
    if should_stop():
        report.stopped_early = True
        return report
    report.embedded = embed_pending(
        conn, provider, clock, cfg.embedding_dims, limit=max_embeddings, should_stop=should_stop
    )
    return report
