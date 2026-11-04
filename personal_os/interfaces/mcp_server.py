"""`personal-os mcp`: your context as a read-only MCP server over stdio (DESIGN.md §6.5).

For assistants like Claude Desktop. Tools only read: answers, search, sessions, project
state. No actions: an outside agent is never "you asking directly" (ADR-017, ADR-064).
Whatever these tools return is your captured context, so only connect clients you trust.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer

from personal_os.config.loader import load_config
from personal_os.core.clock import SystemClock, to_ms
from personal_os.storage.db import connect

server = MCPServer(
    "personal-os", instructions="Read-only access to the user's own captured work context."
)


@contextmanager
def _db() -> Iterator[Any]:
    conn = connect(load_config().db_path)
    try:
        yield conn
    finally:
        conn.close()


@server.tool()
def ask(question: str) -> dict[str, Any]:
    """Ask about the user's own computer activity ("where did I leave off on X?", "what did I
    do yesterday?"). Answers are grounded in captured events and cite numbered evidence; if
    there's no data it says so."""
    from personal_os.context.ask import ask as ask_fn
    from personal_os.llm import make_provider

    cfg = load_config()
    with _db() as conn:
        provider = make_provider(cfg.llm) if cfg.llm.enabled else None
        a = ask_fn(conn, provider, question, SystemClock().now())
        return {
            "answered": a.answered,
            "answer": a.text,
            "confidence": a.confidence,
            "evidence": [x.render() for x in (a.cited() or a.gathered.pack.lines[:10])],
        }


@server.tool()
def search(query: str, project: str | None = None, days: int = 30) -> list[str]:
    """Search pages read, commands, files, commits and summaries (hybrid full-text + meaning)."""
    from personal_os.context.evidence import fetch_by_ids, finalize, lines_from_rows
    from personal_os.context.retrieval import hybrid_search

    now = SystemClock().now()
    with _db() as conn:
        res = hybrid_search(
            conn,
            None,
            query,
            start_ms=to_ms(now - timedelta(days=days)),
            end_ms=to_ms(now),
            project_id=project,
            include_summaries=False,
        )
        pack = finalize(lines_from_rows(fetch_by_ids(conn, [h.id for h in res.hits])), 15)
        return [x.render() for x in pack.lines]


@server.tool()
def sessions(day: str | None = None) -> list[dict[str, Any]]:
    """Work sessions of a day (YYYY-MM-DD, default today): time, project, summary goal."""
    from personal_os.context.summarizer import load_summaries
    from personal_os.projects.sessions import day_bounds, load_sessions

    when = datetime.fromisoformat(day).astimezone() if day else SystemClock().now()
    start, end = day_bounds(when)
    with _db() as conn:
        summaries = {s.session_id: s for s in load_summaries(conn, start, end)}
        return [
            {
                "start": s.start.astimezone().isoformat(timespec="minutes"),
                "end": s.end.astimezone().isoformat(timespec="minutes"),
                "project": s.project_id,
                "active_minutes": s.active_ms // 60000,
                "goal": summaries[s.id].body.goal if s.id in summaries else None,
                "commits": s.stats.get("commits", []),
            }
            for s in load_sessions(conn, start, end)
        ]


@server.tool()
def project_state(project: str) -> dict[str, Any]:
    """The current-state note of a project: goal, done, in progress, blockers, next steps."""
    from personal_os.context import rollups

    with _db() as conn:
        state = rollups.load(conn, f"project:{project}")
        if state is None:
            return {"project": project, "state": None, "note": "no current-state note yet"}
        return {"project": project, "updated": state.created.isoformat(), "state": state.body}


@server.tool()
def list_projects() -> list[dict[str, Any]]:
    """Projects known to Personal OS, with their folders."""
    from personal_os.projects.registry import load_projects

    with _db() as conn:
        return [
            {"id": p.id, "name": p.name, "folders": p.roots}
            for p in load_projects(conn)
            if p.id != "none"
        ]


def run() -> None:
    server.run()  # stdio
