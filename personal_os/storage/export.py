"""Export everything stored, as JSON lines or Markdown (brief §5). Nothing leaves the machine:
the files are written where you say."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from personal_os.core.clock import from_ms

TABLES = (
    "events", "page_extracts", "projects", "assignment_rules", "sessions", "summaries",
    "rollups", "memory_facts", "reminders", "action_audit",
)  # fmt: skip
# Never exported: embeddings (derived numbers), devices (token hashes), counters, settings.


def _rows(conn: sqlite3.Connection, table: str, project: str | None) -> list[dict[str, Any]]:
    sql = f"SELECT * FROM {table}"
    params: list[Any] = []
    if project is not None:
        if table in ("events", "sessions", "summaries", "rollups"):
            sql += " WHERE project_id = ?"
            params.append(project)
        elif table == "page_extracts":
            sql += " WHERE event_id IN (SELECT id FROM events WHERE project_id = ?)"
            params.append(project)
        elif table in ("projects",):
            sql += " WHERE id = ?"
            params.append(project)
        elif table == "assignment_rules":
            sql += " WHERE project_id = ?"
            params.append(project)
        else:
            return []
    cur = conn.execute(sql, params)
    cols = [c[0] for c in cur.description]
    out = []
    for r in cur:
        row = dict(zip(cols, r, strict=True))
        for k in ("payload", "stats", "body", "headings", "roots", "remotes", "keywords",
                  "args", "undo", "evidence_event_ids"):  # fmt: skip
            if isinstance(row.get(k), str):
                with contextlib.suppress(ValueError):
                    row[k] = json.loads(row[k])
        out.append(row)
    return out


def export_json(
    conn: sqlite3.Connection, out_dir: Path, project: str | None = None
) -> dict[str, int]:
    """One JSON-lines file per table. Returns rows written per table."""
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    for table in TABLES:
        rows = _rows(conn, table, project)
        if project is not None and not rows:
            continue
        with (out_dir / f"{table}.jsonl").open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        counts[table] = len(rows)
    return counts


def _when(ms: int | None) -> str:
    return from_ms(ms).astimezone().strftime("%Y-%m-%d %H:%M") if ms else "-"


def export_markdown(
    conn: sqlite3.Connection, out_dir: Path, project: str | None = None
) -> list[Path]:
    """Readable notes: one file per project (sessions with their summaries), plus memory."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    projects = [r["id"] for r in _rows(conn, "projects", project)]
    summaries = {
        s["session_id"]: s for s in _rows(conn, "summaries", project) if s["status"] == "ok"
    }
    for pid in projects:
        sessions = [s for s in _rows(conn, "sessions", pid)]
        if not sessions:
            continue
        lines = [f"# {pid}", "", f"Exported {datetime.now().astimezone():%Y-%m-%d %H:%M}", ""]
        state = conn.execute(
            "SELECT body FROM rollups WHERE id = ? AND status = 'ok'", (f"project:{pid}",)
        ).fetchone()
        if state:
            body = json.loads(state[0])
            lines += ["## Current state", ""]
            for key in ("goal", "done", "in_progress", "blockers", "next_steps"):
                value = body.get(key)
                if value:
                    items = value if isinstance(value, list) else [value]
                    texts = [
                        i.get("text", str(i)) if isinstance(i, dict) else str(i) for i in items
                    ]
                    lines.append(f"**{key.replace('_', ' ')}:** " + "; ".join(texts))
            lines.append("")
        lines += ["## Sessions", ""]
        for s in sorted(sessions, key=lambda s: s["ts_start"]):
            lines.append(f"### {_when(s['ts_start'])} - {_when(s['ts_end'])[11:]}")
            summ = summaries.get(s["id"])
            if summ:
                body = summ["body"]
                lines.append(f"- Goal: {body.get('goal', '')}")
                for item in body.get("actions", []):
                    lines.append(f"- {item.get('text', '')}")
                if body.get("next_step"):
                    lines.append(f"- Next: {body['next_step'].get('text', '')}")
            stats = s["stats"] if isinstance(s["stats"], dict) else {}
            for c in stats.get("commits", []):
                lines.append(f"- Commit: {c}")
            lines.append("")
        path = out_dir / f"{pid}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        written.append(path)
    if project is None:
        facts = _rows(conn, "memory_facts", None)
        if facts:
            path = out_dir / "memory.md"
            body = "\n".join(f"- {f['text']} ({_when(f['created_at'])})" for f in facts)
            path.write_text(f"# Things you asked me to remember\n\n{body}\n", encoding="utf-8")
            written.append(path)
    return written


def delete_project_data(conn: sqlite3.Connection, project: str) -> dict[str, int]:
    """Everything assigned to a project: events (their extracts and vectors go by trigger),
    sessions, summaries, rollups and learned rules. The project entry itself stays (archived)
    so its repo isn't immediately re-added; the audit log is kept (who-did-what-when)."""
    from personal_os.storage.db import transaction
    from personal_os.storage.maintenance import checkpoint

    with transaction(conn):
        extracts = conn.execute(
            "DELETE FROM page_extracts WHERE event_id IN "
            "(SELECT id FROM events WHERE project_id = ?)",
            (project,),
        ).rowcount
        counts = {"page_extracts": extracts}
        for table in ("events", "sessions", "summaries", "rollups", "assignment_rules"):
            counts[table] = conn.execute(
                f"DELETE FROM {table} WHERE project_id = ?", (project,)
            ).rowcount
        conn.execute("UPDATE projects SET archived = 1 WHERE id = ?", (project,))
    checkpoint(conn)
    return counts
