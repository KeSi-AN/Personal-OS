"""Evidence packs: raw events folded into short, numbered lines the model can cite.

Window time is grouped by app and title, file saves by file, repeated commands are folded; commits,
failures, searches and pages read keep their details. Each line keeps the ids of the events behind
it, so a citation [n] maps back to stored events. Sensitive events appear as "site, time" only.
Everything here is captured text, i.e. data: it's cleaned of control characters and of anything
that could close the prompt's data block.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PureWindowsPath
from typing import Any

from personal_os.core.clock import from_ms

MAX_LINES = 80
MIN_WINDOW_MS = 20_000
MIN_PAGE_MS = 5_000
MAX_FILE_LINES = 10  # more files than this: grouped by folder

# Higher = kept first when a pack is over its line budget.
PRIORITY = {
    "commit": 9,
    "failed_command": 8,
    "summary": 8,
    "git_state": 7,
    "search": 6,
    "page": 5,
    "file": 5,
    "command": 4,
    "branch": 4,
    "selection": 3,
    "editor": 3,
    "window": 2,
    "bulk": 1,
}

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_DELIM = re.compile(r"</?\s*data\b", re.I)


def clean(text: str | None, limit: int = 200) -> str:
    """One line of captured text, safe to put inside the prompt's data block."""
    text = _CTRL.sub(" ", (text or "").replace("\r", " ").replace("\n", " "))
    text = _DELIM.sub("[data]", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _name(path: str | None) -> str:
    return PureWindowsPath(path).name if path else ""


def _dur(ms: int) -> str:
    s = ms // 1000
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60:02d}m"
    return f"{s // 60}m" if s >= 60 else f"{s}s"


@dataclass
class EvidenceLine:
    ts: datetime
    kind: str
    text: str
    event_ids: list[str]
    detail: str = ""
    project_id: str | None = None
    weight: int = 0  # ms of attention or a count; breaks priority ties
    ref: int = 0

    def render(self) -> str:
        when = self.ts.astimezone().strftime("%m-%d %H:%M")
        out = f"[{self.ref}] {when} {self.kind}: {self.text}"
        return out + (f"\n    {self.detail}" if self.detail else "")


@dataclass
class EvidencePack:
    lines: list[EvidenceLine] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.lines)

    def render(self) -> str:
        return "\n".join(line.render() for line in self.lines)

    def refs(self) -> set[int]:
        return {line.ref for line in self.lines}

    def line(self, ref: int) -> EvidenceLine | None:
        return next((x for x in self.lines if x.ref == ref), None)

    def event_ids(self, refs: Iterable[int]) -> list[str]:
        out: list[str] = []
        for ref in refs:
            line = self.line(ref)
            for eid in line.event_ids if line else []:
                if eid not in out:
                    out.append(eid)
        return out

    def digest(self) -> str:
        """Changes when what the model would see changes (not when only the numbering does)."""
        blob = "\n".join(f"{x.ts.isoformat()}|{x.kind}|{x.text}|{x.detail}" for x in self.lines)
        return hashlib.sha256(blob.encode()).hexdigest()[:24]


def finalize(lines: list[EvidenceLine], max_lines: int = MAX_LINES) -> EvidencePack:
    """Keep the most informative lines within budget, then number them in time order."""
    keep = sorted(lines, key=lambda x: (-PRIORITY.get(x.kind, 0), -x.weight, x.ts))[:max_lines]
    keep.sort(key=lambda x: (x.ts, x.kind))
    for i, line in enumerate(keep, start=1):
        line.ref = i
    return EvidencePack(keep)


# --- events -> lines --------------------------------------------------------------------------

EVENT_SQL = """
SELECT e.id, e.source, e.type, e.ts_start, coalesce(e.ts_end, e.last_seen, e.ts_start) AS ts_stop,
       e.project_id, e.privacy_class, e.payload,
       x.meta_description AS x_meta, x.headings AS x_headings, x.main_text AS x_text
FROM events e LEFT JOIN page_extracts x ON x.event_id = e.id
"""


def fetch_rows(
    conn: sqlite3.Connection,
    start_ms: int,
    end_ms: int,
    *,
    project_id: str | None = None,
    sources: Sequence[str] | None = None,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[sqlite3.Row]:
    where = [
        "e.ts_start < ?",
        "coalesce(e.ts_end, e.last_seen, e.ts_start) >= ?",
        "e.source NOT IN ('meta', 'system', 'idle')",
    ]
    params: list[Any] = [end_ms, start_ms]
    if project_id is not None:
        where.append("e.project_id = ?")
        params.append(project_id)
    if sources:
        where.append(f"e.source IN ({','.join('?' * len(sources))})")
        params.extend(sources)
    sql = EVENT_SQL + " WHERE " + " AND ".join(where)
    sql += f" ORDER BY e.ts_start {'DESC' if newest_first else 'ASC'}"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def fetch_by_ids(conn: sqlite3.Connection, ids: Sequence[str]) -> list[sqlite3.Row]:
    if not ids:
        return []
    return conn.execute(
        EVENT_SQL + f" WHERE e.id IN ({','.join('?' * len(ids))}) ORDER BY e.ts_start", list(ids)
    ).fetchall()


def lines_from_rows(
    rows: Iterable[sqlite3.Row], *, git_state: bool = True, focus: Sequence[str] = ()
) -> list[EvidenceLine]:
    """Fold events into lines. Rows must come from EVENT_SQL.

    `focus`: the question's words. A page then carries the passages of its text that are about
    them (from the fullest capture of that page), not just its opening lines: an answer is
    rarely in the first 200 characters ("which STT model was suggested?" is in the middle of
    a search engine's AI answer).
    """
    windows: dict[tuple[str, str], EvidenceLine] = {}
    files: dict[str, tuple[EvidenceLine, dict[str, int]]] = {}
    commands: dict[tuple[str, str], tuple[EvidenceLine, dict[str, int]]] = {}
    pages: dict[str, EvidenceLine] = {}
    page_rows: dict[str, tuple[sqlite3.Row, dict[str, Any]]] = {}  # the fullest capture per page
    searches: dict[tuple[str, str], EvidenceLine] = {}
    editors: dict[str, EvidenceLine] = {}
    states: dict[str, EvidenceLine] = {}
    out: list[EvidenceLine] = []

    rows = list(rows)
    for r in rows:  # first pass: which visit of each page captured the most text
        if r["source"] == "browser" and r["type"] == "page_visit" and r["x_text"]:
            p = json.loads(r["payload"])
            key = p.get("url") or r["id"]
            have = page_rows.get(key)
            if have is None or len(r["x_text"]) > len(have[0]["x_text"] or ""):
                page_rows[key] = (r, p)

    for r in rows:
        p = json.loads(r["payload"])
        ts = from_ms(r["ts_start"])
        span = max(0, r["ts_stop"] - r["ts_start"])
        sensitive = r["privacy_class"] != "normal"
        eid, pid = r["id"], r["project_id"]
        kind = f"{r['source']}.{r['type']}"
        if _own(kind, p):
            continue  # Personal OS's own questions and dashboard aren't things you did

        if kind == "window.focus":
            app = clean(p.get("app") or p.get("exe") or "?", 40)
            hide = sensitive or p.get("title") == "[redacted]"
            title = "" if hide else clean(p.get("title"), 120)
            line = windows.get((app, title))
            if line is None:
                text = f"{app}: {title}" if title else app
                line = windows[(app, title)] = EvidenceLine(ts, "window", text, [], project_id=pid)
            line.weight += span
            if len(line.event_ids) < 5:
                line.event_ids.append(eid)

        elif kind == "browser.page_visit":
            active = int(p.get("active_dwell_ms") or 0)
            if active < MIN_PAGE_MS and not p.get("searched") and not p.get("bookmarked"):
                continue
            domain = clean(p.get("domain"), 60)
            hidden = sensitive or p.get("title") in ("", "[redacted]")
            key = domain if hidden else (p.get("url") or eid)
            line = pages.get(key)
            if line is None:
                title = "" if hidden else clean(p.get("title"), 140)
                text = f"'{title}' ({domain})" if title else f"visited {domain} (content hidden)"
                fullest = page_rows.get(key, (r, p))
                detail = "" if hidden else _page_detail(fullest[0], fullest[1], focus)
                line = pages[key] = EvidenceLine(ts, "page", text, [], detail, pid)
                out.append(line)
            line.weight += active
            line.event_ids.append(eid)

        elif r["source"] == "browser" and not sensitive:
            text = clean(p.get("text"), 160)
            site = clean(p.get("engine") or p.get("domain"), 40)
            if r["type"] == "search" and text:
                done = searches.get((text.lower(), site))
                if done is not None:  # the same search, re-sent as the page updates
                    done.event_ids.append(eid)
                    continue
                line = EvidenceLine(ts, "search", f"'{text}' on {site}", [eid], project_id=pid)
                searches[(text.lower(), site)] = line
                out.append(line)
            elif r["type"] in ("selection", "copy") and text:
                out.append(
                    EvidenceLine(ts, "selection", f"'{text}' from {site}", [eid], project_id=pid)
                )

        elif kind == "terminal.command":
            if sensitive:
                continue
            cmd = clean(p.get("command"), 160)
            folder = _name(p.get("cwd")) or "?"
            entry = commands.get((cmd, folder))
            failed = p.get("success") is False
            if entry is None:
                line = EvidenceLine(ts, "command", "", [], project_id=pid)
                entry = commands[(cmd, folder)] = (line, {"n": 0, "failed": 0, "exit": 0})
                out.append(line)
            line, c = entry
            c["n"] += 1
            if failed:
                c["failed"] += 1
                c["exit"] = int(p.get("exit_code") or 1)
                line.ts = ts  # a failure is the moment that matters
            line.event_ids.append(eid)
            line.weight = c["n"]
            line.kind = "failed_command" if c["failed"] else "command"
            result = (
                f" -> failed (exit {c['exit']})"
                if failed
                else (" -> ok" if p.get("success") else "")
            )
            if c["n"] > 1:
                result += f" [{c['n']} runs, {c['failed']} failed]"
            line.text = f"`{cmd}` in {folder}{result}"

        elif kind == "git.commit":
            msg = clean((p.get("message") or "").splitlines()[0] if p.get("message") else "", 140)
            repo = _name(p.get("repo_root"))
            stats = (
                f"{p.get('files_changed', 0)} files, "
                f"+{p.get('insertions', 0)}/-{p.get('deletions', 0)}"
            )
            names = ", ".join(_name(f) for f in (p.get("files") or [])[:6])
            out.append(
                EvidenceLine(
                    ts,
                    "commit",
                    f"'{msg}' in {repo}"
                    + (f" on {p['branch']}" if p.get("branch") else "")
                    + f" ({stats})",
                    [eid],
                    clean(f"files: {names}", 200) if names else "",
                    pid,
                )
            )

        elif kind == "git.state" and git_state:
            repo = _name(p.get("repo_root"))
            parts = [f"branch {p['branch']}"] if p.get("branch") else []
            for k in ("staged", "unstaged", "untracked"):
                if p.get(k):
                    parts.append(f"{p[k]} {k}")
            if not any(p.get(k) for k in ("staged", "unstaged", "untracked")):
                parts.append("no uncommitted changes")
            if p.get("ahead") or p.get("behind"):
                parts.append(f"ahead {p.get('ahead', 0)}, behind {p.get('behind', 0)}")
            if p.get("op") not in (None, "none"):
                parts.append(f"{p['op']} in progress")
            changed = [x if isinstance(x, str) else x.get("path", "") for x in p.get("files") or []]
            detail = clean("changed: " + ", ".join(_name(f) for f in changed[:8]), 200)
            states[repo] = EvidenceLine(
                ts,
                "git_state",
                f"{repo}: " + ", ".join(parts),
                [eid],
                detail if changed else "",
                pid,
            )

        elif kind == "git.branch_switch":
            repo = _name(p.get("repo_root"))
            text = f"{repo}: switched from {p.get('from_branch')} to {p.get('to_branch')}"
            out.append(EvidenceLine(ts, "branch", text, [eid], project_id=pid))

        elif kind == "fs.change":
            if p.get("via_git"):
                continue
            path = p.get("path") or ""
            entry2 = files.get(path)
            if entry2 is None:
                line = EvidenceLine(ts, "file", "", [], project_id=pid)
                entry2 = files[path] = (line, {"n": 0, "added": 0, "removed": 0})
                out.append(line)
            line, c = entry2
            c["n"] += 1
            c["added"] = max(c["added"], p.get("lines_added") or 0)
            c["removed"] = max(c["removed"], p.get("lines_removed") or 0)
            if len(line.event_ids) < 5:
                line.event_ids.append(eid)
            line.weight = c["n"]
            verb = {"created": "created", "deleted": "deleted", "moved": "moved"}.get(
                p.get("change", ""), "edited"
            )
            sym = [*p.get("symbols_added", []), *p.get("symbols_changed", [])][:4]
            diff = f", +{c['added']}/-{c['removed']} lines" if c["added"] or c["removed"] else ""
            name = clean(_name(path), 80) if sensitive or p.get("sensitive") else clean(path, 140)
            line.text = f"{verb} {name} ({c['n']} saves{diff})"
            line.detail = clean("symbols: " + ", ".join(sym), 160) if sym else ""

        elif kind == "fs.bulk_change":
            text = f"{p.get('count', 0)} files changed under {clean(p.get('dir'), 120)}"
            out.append(EvidenceLine(ts, "bulk", text, [eid], project_id=pid))

        elif r["source"] == "editor":
            target = _name(p.get("file")) or _name(p.get("workspace")) or p.get("name") or ""
            if not target:
                continue
            line = editors.get(target)
            if line is None:
                line = editors[target] = EvidenceLine(
                    ts, "editor", f"editor: {clean(target, 100)}", [], project_id=pid
                )
                out.append(line)
            if len(line.event_ids) < 5:
                line.event_ids.append(eid)
            line.weight += 1

    for line in windows.values():
        if line.weight >= MIN_WINDOW_MS:
            line.text += f" ({_dur(line.weight)})"
            out.append(line)
    for line in pages.values():
        if line.weight:
            line.text += f", {_dur(line.weight)}"
    out.extend(states.values())
    if len(files) > MAX_FILE_LINES:
        out = _fold_files(out, files)
    return out


def _fold_files(
    out: list[EvidenceLine], files: dict[str, tuple[EvidenceLine, dict[str, int]]]
) -> list[EvidenceLine]:
    """Many files touched: one line per folder (with two or more files) instead of one per file."""
    by_folder: dict[str, list[tuple[str, EvidenceLine, dict[str, int]]]] = {}
    for path, (line, c) in files.items():
        folder = str(PureWindowsPath(path).parent).replace("\\", "/")
        by_folder.setdefault(folder, []).append((path, line, c))
    folded: set[int] = set()
    for folder, entries in by_folder.items():
        if len(entries) < 2:
            continue
        entries.sort(key=lambda e: -e[2]["n"])
        names = ", ".join(f"{_name(p)} ({c['n']})" for p, _, c in entries[:8])
        more = f" and {len(entries) - 8} more" if len(entries) > 8 else ""
        ids = list(dict.fromkeys(i for _, line, _ in entries for i in line.event_ids[:2]))[:10]
        first = min(line.ts for _, line, _ in entries)
        text = f"edited {len(entries)} files in {clean(folder, 120)} (saves: {names}{more})"
        summary = EvidenceLine(first, "file", clean(text, 300), ids, "", entries[0][1].project_id)
        summary.weight = sum(c["n"] for _, _, c in entries)
        out.append(summary)
        folded.update(id(line) for _, line, _ in entries)
    return [x for x in out if id(x) not in folded]


_OWN_COMMAND = re.compile(r"\bpersonal-os\s+(?:ask|do|search|briefing|voice|sessions|inbox)\b")
_OWN_PAGES = re.compile(r"^https?://(?:127\.0\.0\.1|localhost):4782\d", re.I)


def own_trace(kind: str, p: dict[str, Any]) -> bool:
    return _own(kind, p)


def _own(kind: str, p: dict[str, Any]) -> bool:
    """Personal OS's own traces: asking it a question, its dashboard. Evidence about what you
    did must not include you asking about it ("You asked about the STT model ...")."""
    if kind == "terminal.command":
        return bool(_OWN_COMMAND.search(str(p.get("command") or "")))
    if kind.startswith("browser."):
        return bool(_OWN_PAGES.match(str(p.get("url") or "")))
    return False


PASSAGE_CHARS = 320
PASSAGES = 3


def relevant_passages(text: str, focus: Sequence[str], limit: int = PASSAGES) -> str:
    """The parts of a page's text that mention the question's words, in page order, joined
    with " … ". Windows of ~320 characters, scored by how many different focus words they
    contain (then how often). The opening is used when nothing matches."""
    words = [w.lower() for w in focus if len(w) >= 3]
    if not words or not text:
        return text[: PASSAGE_CHARS * 2]
    low = text.lower()
    step = PASSAGE_CHARS // 2
    scored = []
    for start in range(0, max(1, len(text) - step), step):
        window = low[start : start + PASSAGE_CHARS]
        distinct = sum(1 for w in words if w in window)
        if distinct:
            scored.append((distinct, sum(window.count(w) for w in words), start))
    if not scored:
        return text[: PASSAGE_CHARS * 2]
    chosen: list[int] = []
    for _d, _n, start in sorted(scored, key=lambda s: (-s[0], -s[1], s[2])):
        if all(abs(start - c) >= PASSAGE_CHARS for c in chosen):
            chosen.append(start)
        if len(chosen) == limit:
            break
    return " … ".join(text[s : s + PASSAGE_CHARS].strip() for s in sorted(chosen))


def _page_detail(r: sqlite3.Row, p: dict[str, Any], focus: Sequence[str] = ()) -> str:
    bits = []
    if focus and r["x_text"]:
        # Asked about something: what the page says about it.
        bits.append(relevant_passages(r["x_text"], focus))
        enrichment = p.get("enrichment") or {}
        if enrichment.get("repo"):
            bits.append(f"repo {enrichment['repo']}")
        return clean(" | ".join(bits), PASSAGE_CHARS * PASSAGES + 40)
    if r["x_meta"]:
        bits.append(r["x_meta"])
    elif r["x_text"]:
        bits.append(r["x_text"][:200])
    if r["x_headings"]:
        heads = json.loads(r["x_headings"])[:3]
        if heads:
            bits.append("headings: " + "; ".join(heads))
    enrichment = p.get("enrichment") or {}
    if enrichment.get("repo"):
        bits.append(f"repo {enrichment['repo']}")
    return clean(" | ".join(bits), 300)
