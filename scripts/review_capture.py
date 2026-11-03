"""Print a human-readable review of captured events, for checking capture accuracy.

    uv run python scripts/review_capture.py [--minutes 60] [--db PATH]

Reads the database read-only. Output contains your own captured titles and commands, so
don't paste it anywhere you wouldn't paste them.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from personal_os.config.loader import load_config


def hhmm(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M:%S") if ms else "  open  "


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--db", type=Path)
    args = ap.parse_args()
    db = args.db or load_config().db_path
    c = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    since = int((time.time() - args.minutes * 60) * 1000)

    def rows(sql: str):
        return c.execute(sql, {"since": since})

    print("== counts (source, type, class, n)")
    for r in rows(
        "SELECT source, type, privacy_class, count(*) FROM events "
        "WHERE ts_start >= :since GROUP BY 1, 2, 3"
    ):
        print("  ", *r)

    print("\n== window / idle / system / meta timeline")
    for s, t, a, b, p in rows(
        "SELECT source, type, ts_start, ts_end, payload FROM events WHERE ts_start >= :since "
        "AND source IN ('window','idle','system','meta') ORDER BY ts_start"
    ):
        p = json.loads(p)
        dur = f"{(b - a) / 1000:6.0f}s" if b else "  open"
        if s == "window":
            extra = f" [alt-tab x{p['transient_switches']}]" if p.get("transient_switches") else ""
            desc = f"{p.get('app', '')} | {p.get('title', '')[:60]}{extra}"
        else:
            desc = p.get("reason") or ""
        print(f"  {hhmm(a)} {dur}  {s}.{t:<11} {desc}")

    print("\n== terminal")
    for a, p in rows(
        "SELECT ts_start, payload FROM events WHERE ts_start >= :since "
        "AND source = 'terminal' ORDER BY ts_start"
    ):
        p = json.loads(p)
        repo = (p.get("repo_root") or "-").split("/")[-1]
        print(
            f"  {hhmm(a)} {(p.get('duration_ms') or 0) / 1000:6.1f}s exit={p.get('exit_code')} "
            f"ok={p.get('success')} repo={repo} branch={p.get('branch')} | {p['command'][:70]}"
        )

    print("\n== git (commits, switches, operations)")
    for a, t, p in rows(
        "SELECT ts_start, type, payload FROM events WHERE ts_start >= :since "
        "AND source = 'git' AND type != 'state' ORDER BY ts_start"
    ):
        p = json.loads(p)
        repo = p.get("repo_root", "").split("/")[-1]
        what = p.get("message", "")[:60] or f"{p.get('from_branch')} -> {p.get('to_branch')}"
        print(f"  {hhmm(a)} {t:<14} {repo}: {what}")
    n, repos = rows(
        "SELECT count(*), count(DISTINCT json_extract(payload, '$.repo_root')) "
        "FROM events WHERE ts_start >= :since AND source = 'git' AND type = 'state'"
    ).fetchone()
    print(f"  git.state: {n} events across {repos} repos")

    print("\n== files")
    for a, p, cls in rows(
        "SELECT ts_start, payload, privacy_class FROM events WHERE ts_start >= :since "
        "AND source = 'fs' ORDER BY ts_start"
    ):
        p = json.loads(p)
        path = p.get("path") or p.get("dir", "")
        sym = ""
        if p.get("symbols_added") or p.get("symbols_changed"):
            sym = f" +sym {p.get('symbols_added')} ~sym {p.get('symbols_changed')}"
        print(
            f"  {hhmm(a)} {p.get('change', 'bulk'):<8} {cls:<9} …{path[-60:]} "
            f"+{p.get('lines_added')}/-{p.get('lines_removed')}{sym}"
            f"{' [git]' if p.get('via_git') else ''}"
        )

    print("\n== privacy counters (today)")
    for r in c.execute("SELECT scope, name, value FROM counters ORDER BY 1, 2"):
        print("  ", *r)


if __name__ == "__main__":
    main()
