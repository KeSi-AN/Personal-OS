"""Assigns each event a project and a confidence (DESIGN.md §6.1).

Signals propose (project, confidence) candidates; the best one wins. Below MIN_CONFIDENCE the
event is left unassigned and shows up in the inbox. The temporal signal runs as a second pass
over what is still unassigned, so it can lean on neighbours resolved in the first pass.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from personal_os.core.clock import Clock, to_ms
from personal_os.observers.window import browser_page_title
from personal_os.projects.features import (
    EDITOR_EXES,
    NO_PROJECT_SOURCES,
    Features,
    build,
    title_segments,
    tokens,
)
from personal_os.projects.registry import NONE_PROJECT, Project, load_projects
from personal_os.storage.db import transaction

MIN_CONFIDENCE = 0.5
STRONG = 0.85  # evidence good enough to teach keywords and to anchor temporal smoothing
TEMPORAL_WINDOW = timedelta(minutes=10)
MIN_KEYWORD_MATCHES = 2
MIN_KEYWORD_SHARE = 0.25
DISTINCTIVE_SHARE = 0.25
TEMPORAL_MAX = 0.65  # the most the temporal signal can claim (all neighbours agree)
COMBINED_MAX = 0.84  # agreeing weak signals never reach STRONG (they don't anchor others)
PATH_CONFIDENCE = {"git": 1.0, "fs": 0.98, "terminal": 0.95, "editor": 0.95, "window": 0.6}
_SELECT = (
    "SELECT e.id, e.source, e.type, e.ts_start, e.ts_end, e.last_seen, e.privacy_class, "
    "e.assign_source, e.project_id, e.project_confidence, e.payload, x.title AS x_title, "
    "x.headings AS x_headings FROM events e LEFT JOIN page_extracts x ON x.event_id = e.id "
)


@dataclass(frozen=True)
class Decision:
    project_id: str | None
    confidence: float
    source: str  # path|title|remote|page|rule|keyword|temporal|unassigned|n/a|user


class Resolver:
    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self.conn = conn
        self.clock = clock
        self.projects: list[Project] = []
        self.rules: dict[tuple[str, str], dict[str, float]] = {}
        self.profiles: dict[str, Counter[str]] = {}
        self.idf: dict[str, float] = {}
        self.df: Counter[str] = Counter()  # in how many project vocabularies each word occurs

    # --- context --------------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload projects and rules, and rebuild keyword profiles from strong assignments."""
        self.projects = load_projects(self.conn)
        self.rules = defaultdict(dict)
        for kind, key, pid, weight in self.conn.execute(
            "SELECT kind, key, project_id, weight FROM assignment_rules"
        ):
            self.rules[(kind, key)][pid] = weight
        since = to_ms(self.clock.now() - timedelta(days=30))
        profiles: dict[str, Counter[str]] = defaultdict(Counter)
        for p in self.projects:
            if p.id == NONE_PROJECT:
                continue
            for word in [*tokens(p.name), *(w for k in p.keywords for w in tokens(k))]:
                profiles[p.id][word] += 5  # names and your keywords count more
            for root in p.norm_roots:
                for word in tokens(root.rsplit("/", 1)[-1]):
                    profiles[p.id][word] += 5
        rows = self.conn.execute(
            _SELECT + "WHERE e.project_id IS NOT NULL AND e.project_id != ? "
            "AND e.project_confidence >= ? AND e.assign_source != 'keyword' AND e.ts_start >= ?",
            (NONE_PROJECT, STRONG, since),
        ).fetchall()
        for row in rows:
            f = build(row, row["x_title"], row["x_headings"])
            if self._has_meaningful_words(f):
                profiles[row["project_id"]].update(set(f.words))  # once per event, not per word
        self.profiles = dict(profiles)
        df: Counter[str] = Counter()
        for counts in self.profiles.values():
            df.update(counts.keys())
        n = max(1, len(self.profiles))
        self.df = df
        self.idf = {w: math.log((n + 1) / (c + 1)) + 1 for w, c in df.items()}

    # --- signals --------------------------------------------------------------------------

    def _by_path(self, f: Features) -> Decision | None:
        best: tuple[int, Project] | None = None
        for path, _role in f.paths:
            for p in self.projects:
                for root in p.norm_roots:
                    if (path == root or path.startswith(root + "/")) and (
                        best is None or len(root) > best[0]
                    ):
                        best = (len(root), p)
        if best is not None:
            return Decision(best[1].id, PATH_CONFIDENCE.get(f.source, 0.6), "path")
        # Outside every root, a folder named like a project still says a lot:
        # ".../AppData/Local/PersonalOS" for the project "personal-os".
        for path, _role in f.paths:
            folders = {_squash(part) for part in path.split("/") if len(_squash(part)) >= 5}
            hits = [p for p in self.projects if folders & {_squash(n) for n in p.folder_names}]
            if len(hits) == 1:
                return Decision(hits[0].id, 0.7, "path_name")
        return None

    def _by_title(self, f: Features) -> Decision | None:
        if f.source != "window" or f.is_browser_window:
            return None
        segments = {s.lower() for s in title_segments(f.title)}
        hits = [p for p in self.projects if p.folder_names & segments]
        if len(hits) != 1:
            return None
        return Decision(hits[0].id, 0.9 if (f.exe or "") in EDITOR_EXES else 0.65, "title")

    def _by_remote(self, f: Features) -> Decision | None:
        if not f.github:
            return None
        for p in self.projects:
            if f.github in p.remotes:
                return Decision(p.id, 0.9, "remote")
        return None

    def _by_page(self, f: Features) -> Decision | None:
        """A browser window takes the project of the page visit it was showing: matched by
        title, or, when the window title was redacted (mail), by the hidden page visit it
        overlaps most. Hidden visits are only ever paired with hidden windows."""
        if not f.is_browser_window:
            return None
        page = browser_page_title(f.title).casefold()
        if not page:
            return None
        redacted = page == "[redacted]"
        best: tuple[float, Decision] | None = None
        for pid, conf, title, a, b in self.conn.execute(
            "SELECT project_id, project_confidence, json_extract(payload, '$.title'), ts_start, "
            "coalesce(ts_end, last_seen, ts_start) FROM events "
            "WHERE source = 'browser' AND type = 'page_visit' AND project_id IS NOT NULL "
            "AND ts_start <= ? AND coalesce(ts_end, last_seen, ts_start) >= ?",
            (to_ms(f.end), to_ms(f.start)),
        ):
            hidden = (title or "") == "[redacted]"
            if redacted != hidden or not conf:
                continue
            if not redacted and (title or "").casefold() != page:
                continue
            overlap = min(b, to_ms(f.end)) - max(a, to_ms(f.start))
            if best is None or overlap > best[0]:
                best = (overlap, Decision(pid, round(conf * 0.95, 3), "page"))
        return best[1] if best else None

    def _by_rules(self, f: Features) -> Decision | None:
        for kind, key in f.rule_keys():
            if kind == "path":
                matches = [
                    w
                    for (k, rk), w in self.rules.items()
                    if k == "path" and (key == rk or key.startswith(rk + "/"))
                ]
            else:
                matches = [self.rules[(kind, key)]] if (kind, key) in self.rules else []
            if not matches:
                continue
            weights: Counter[str] = Counter()
            for w in matches:
                weights.update(w)
            pid, top = weights.most_common(1)[0]
            total = sum(weights.values())
            share = top / total
            if kind == "app":
                # A whole app (terminal, Claude, Explorer) is usually shared between projects:
                # one correction ranks below the temporal signal and only agreement builds it up.
                cap = 0.55 if total < 2 else 0.65 if total < 3 else 0.8
            else:
                cap = {"github": 0.95, "path": 0.9, "app_title": 0.85, "domain": 0.8}[kind]
                cap = cap if total >= 2 else cap - 0.1
            conf = share * cap
            if conf >= MIN_CONFIDENCE:
                return Decision(pid, round(conf, 3), "rule")
        return None

    @staticmethod
    def _has_meaningful_words(f: Features) -> bool:
        """Window titles of generic apps (terminal, Claude, Meet) are just the app's name;
        only editor titles (file names) and other sources carry project words."""
        return f.source != "window" or (f.exe or "") in EDITOR_EXES

    def _by_keywords(self, f: Features) -> Decision | None:
        """Words the page/title shares with a project's vocabulary. Deliberately cautious: on
        real data a single shared word ("login" on a sign-in page, for a project named
        login-page) is noise, so at least two distinct words must match, each established in
        the project's vocabulary (seen twice, or part of its name or your keywords), and the
        winner must be clearly ahead. Confidence stays at or below 0.7, so most keyword
        guesses are shown in the inbox for review."""
        if not self._has_meaningful_words(f):
            return None
        words = set(f.words)
        if f.domain:  # the site's own name says where you are, not what you're working on
            words -= set(tokens(f.domain.replace(".", " ")))
        if not words or not self.profiles:
            return None
        scores: dict[str, tuple[float, int]] = {}
        for pid, counts in self.profiles.items():
            matched = {w for w in words if counts.get(w, 0) >= 2}
            # A real topical match is a decent share of the page's words; a busy page (a site's
            # home page with dozens of headings) matches a few words of any big vocabulary.
            # ...and at least one matched word must be distinctive: in at most a quarter of
            # all project vocabularies ("oauth", "httpx"), not everyday words ("data").
            distinctive_df = max(1.0, DISTINCTIVE_SHARE * len(self.profiles))
            if (
                len(matched) >= MIN_KEYWORD_MATCHES
                and len(matched) >= MIN_KEYWORD_SHARE * len(words)
                and any(self.df[w] <= distinctive_df for w in matched)
            ):
                scores[pid] = (sum(self.idf.get(w, 1.0) for w in matched), len(matched))
        if not scores:
            return None
        ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
        best_pid, (best, n) = ranked[0]
        second = ranked[1][1][0] if len(ranked) > 1 else 0.0
        if best < 1.6 * second:
            return None
        return Decision(best_pid, round(min(0.7, 0.45 + 0.05 * n), 3), "keyword")

    def candidates(self, f: Features) -> list[Decision]:
        if f.source in NO_PROJECT_SOURCES:
            return []
        found = (
            self._by_path(f),
            self._by_title(f),
            self._by_remote(f),
            self._by_page(f),
            self._by_rules(f),
            self._by_keywords(f),
        )
        return [d for d in found if d]

    def decide(self, f: Features) -> Decision:
        if f.source in NO_PROJECT_SOURCES:
            return Decision(None, 0.0, "n/a")
        candidates = self.candidates(f)
        if not candidates:
            return Decision(None, 0.0, "unassigned")
        best = max(candidates, key=lambda d: d.confidence)
        if best.confidence < MIN_CONFIDENCE:
            return Decision(None, best.confidence, "unassigned")
        return best

    # --- passes ---------------------------------------------------------------------------

    def _write(self, decisions: list[tuple[str, Decision]]) -> None:
        now = to_ms(self.clock.now())
        with transaction(self.conn):
            self.conn.executemany(
                "UPDATE events SET project_id = ?, project_confidence = ?, assign_source = ?, "
                "assigned_at = ? WHERE id = ? AND coalesce(assign_source, '') != 'user'",
                [(d.project_id, d.confidence, d.source, now, eid) for eid, d in decisions],
            )

    def resolve_pending(self, limit: int = 5000) -> int:
        """First pass over events never looked at; then the temporal pass."""
        self.refresh()
        rows = self.conn.execute(
            _SELECT + "WHERE e.assigned_at IS NULL ORDER BY e.ts_start LIMIT ?", (limit,)
        ).fetchall()
        # Pages first, so browser windows can take the project of the page they showed.
        first = [r for r in rows if r["source"] != "window"]
        windows = [r for r in rows if r["source"] == "window"]
        for group in (first, windows):
            self._write(
                [(r["id"], self.decide(build(r, r["x_title"], r["x_headings"]))) for r in group]
            )
        # Vocabularies grow from what was just assigned (on a fresh database they start as bare
        # project names), so give what's still unassigned a second look before the temporal pass.
        if rows:
            self.refresh()
            ids = [r["id"] for r in rows]
            again = self.conn.execute(
                _SELECT + "WHERE e.assign_source = 'unassigned' AND e.id IN "
                f"({','.join('?' * len(ids))})",
                ids,
            ).fetchall()
            self._write(
                [(r["id"], self.decide(build(r, r["x_title"], r["x_headings"]))) for r in again]
            )
        return len(rows) + self.temporal_pass()

    def resolve_all(self, since: datetime | None = None) -> int:
        """Re-decide everything you haven't corrected (after a new correction or project)."""
        params: tuple[object, ...] = ()
        where = "WHERE coalesce(assign_source, '') != 'user'"
        if since is not None:
            where += " AND ts_start >= ?"
            params = (to_ms(since),)
        with transaction(self.conn):
            self.conn.execute(f"UPDATE events SET assigned_at = NULL {where}", params)
        total = 0
        while True:
            n = self.resolve_pending()
            total += n
            if not self.conn.execute(
                "SELECT 1 FROM events WHERE assigned_at IS NULL LIMIT 1"
            ).fetchone():
                return total

    def temporal_pass(self, horizon: timedelta = timedelta(days=2)) -> int:
        """Second pass for browser/window/terminal events that are unassigned or only weakly
        assigned. Adds the temporal signal (what you were clearly working on in the 10 minutes
        around them) and combines it with the event's own weak evidence: independent signals
        that agree add up (noisy-OR), so a page whose words match the project you were working
        on can outweigh a rule learned from a single correction ("YouTube is personal"), while
        context alone (0.65 at most) cannot."""
        since = to_ms(self.clock.now() - horizon)
        pending = self.conn.execute(
            _SELECT + "WHERE (e.assign_source IN ('unassigned', 'temporal', 'combined') OR "
            "(e.assign_source IN ('rule', 'keyword') AND e.project_confidence < ?)) "
            "AND e.source IN ('browser', 'window', 'terminal') AND e.ts_start >= ?",
            (STRONG, since),
        ).fetchall()
        if not pending:
            return 0
        window_ms = int(TEMPORAL_WINDOW.total_seconds() * 1000)
        lo = min(r["ts_start"] for r in pending) - window_ms
        hi = max(r["ts_start"] for r in pending) + window_ms
        anchors = self.conn.execute(
            "SELECT ts_start, coalesce(ts_end, last_seen, ts_start), project_id FROM events "
            "WHERE project_confidence >= ? AND assign_source NOT IN ('temporal', 'keyword', "
            "'combined') AND project_id IS NOT NULL AND project_id != ? "
            "AND ts_start BETWEEN ? AND ?",
            (STRONG, NONE_PROJECT, lo, hi),
        ).fetchall()
        decisions = []
        for r in pending:
            f = build(r, r["x_title"], r["x_headings"])
            evidence: dict[str, list[Decision]] = defaultdict(list)
            for d in self.candidates(f):
                if d.project_id:
                    evidence[d.project_id].append(d)
            t = r["ts_start"]
            weight: Counter[str] = Counter()
            for a, b, pid in anchors:
                if a - window_ms <= t <= b + window_ms:
                    weight[pid] += max(1.0, (b - a) / 60_000)  # longer anchors weigh more
            if weight:
                pid, top = weight.most_common(1)[0]
                share = top / sum(weight.values())
                if share >= 0.7:
                    conf = round((TEMPORAL_MAX - 0.05) * share + 0.05, 3)
                    evidence[pid].append(Decision(pid, conf, "temporal"))
            if not evidence:
                if r["assign_source"] in ("temporal", "combined"):
                    decisions.append((r["id"], Decision(None, 0.0, "unassigned")))
                continue
            best = max(evidence.items(), key=lambda kv: _combine(kv[1]))
            pid, ds = best
            conf = _combine(ds)
            if conf < MIN_CONFIDENCE:
                d = Decision(None, round(conf, 3), "unassigned")
            elif len(ds) == 1:
                d = ds[0]
            else:
                d = Decision(pid, round(min(COMBINED_MAX, conf), 3), "combined")
            if (d.project_id, d.source) != (r["project_id"], r["assign_source"]) or abs(
                d.confidence - (r["project_confidence"] or 0)
            ) > 1e-6:
                decisions.append((r["id"], d))
        self._write(decisions)
        return len(decisions)


def _squash(name: str) -> str:
    """'Personal-OS', 'personal_os' and 'PersonalOS' all become 'personalos'."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _combine(decisions: list[Decision]) -> float:
    """Noisy-OR of independent signals for the same project."""
    miss = 1.0
    for d in decisions:
        miss *= 1 - d.confidence
    return 1 - miss


def learn(
    conn: sqlite3.Connection, clock: Clock, keys: list[tuple[str, str]], project_id: str
) -> None:
    """Store a correction as rules (one weight per correction, not per event)."""
    now = to_ms(clock.now())
    with transaction(conn):
        conn.executemany(
            "INSERT INTO assignment_rules (kind, key, project_id, weight, updated_at) "
            "VALUES (?, ?, ?, 1, ?) ON CONFLICT (kind, key, project_id) "
            "DO UPDATE SET weight = weight + 1, updated_at = excluded.updated_at",
            [(k, v, project_id, now) for k, v in keys],
        )
