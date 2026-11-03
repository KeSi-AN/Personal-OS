"""Project-resolution accuracy on a labelled simulated scenario (phase 3 acceptance).

Protocol: ingest the learning day and resolve it cold; a simulated user then corrects the
inbox once, the way a careful person would (a group is assigned only if at least 80% of it
truly belongs to one project, otherwise its items are fixed one by one without teaching a
rule); then the test day is ingested and resolved, and scored against the step labels.
An unassigned event counts as wrong.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from personal_os.config.schema import Config
from personal_os.core.clock import SimClock, to_ms
from personal_os.core.events import Event
from personal_os.core.wiring import build_pipeline
from personal_os.projects.inbox import assign_event, assign_group, inbox
from personal_os.projects.registry import ensure_repo_projects
from personal_os.projects.resolver import Resolver
from personal_os.sim.generator import Generator
from personal_os.sim.scenario import load_scenario
from personal_os.storage.db import open_db

SCORED_SOURCES = ("window", "browser", "terminal", "fs", "git")


@dataclass
class Score:
    correct: int = 0
    total: int = 0
    correct_s: float = 0.0
    total_s: float = 0.0
    by_source: dict[str, list[int]] = field(default_factory=dict)  # source -> [correct, total]
    mistakes: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def time_accuracy(self) -> float:
        return self.correct_s / self.total_s if self.total_s else 0.0


@dataclass
class Report:
    learning_day: Score
    test_day: Score
    corrections: list[str]


def _score(conn, labels: dict[UUID, str], events: list[Event]) -> Score:  # type: ignore[no-untyped-def]
    s = Score()
    stored = {
        r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, project_id, assign_source FROM events")
    }
    seen: set[UUID] = set()
    for e in events:
        if e.id in seen or e.id not in labels or e.source not in SCORED_SOURCES:
            continue
        seen.add(e.id)
        if str(e.id) not in stored:
            continue  # blocked by the privacy filter (never stored): nothing to assign
        got, how = stored[str(e.id)]
        truth = labels[e.id]
        ok = got == truth
        last = max((x for x in events if x.id == e.id), key=lambda x: x.ts_end or x.ts_start)
        secs = ((last.ts_end or last.ts_start) - e.ts_start).total_seconds()
        if e.source == "browser" and e.type == "page_visit":
            secs = getattr(e.payload, "active_dwell_ms", 0) / 1000
        s.total += 1
        s.correct += ok
        s.total_s += secs
        s.correct_s += secs * ok
        c = s.by_source.setdefault(e.source, [0, 0])
        c[0] += ok
        c[1] += 1
        if not ok:
            title = getattr(e.payload, "title", None) or getattr(e.payload, "command", None) or ""
            s.mistakes.append(
                f"{e.kind:<20} truth={truth:<15} got={got or '-':<15} ({how}) {title[:50]}"
            )
    return s


def evaluate(scenario_path: Path, config: Config, db_path: Path) -> Report:
    scenario = load_scenario(scenario_path)
    gen = Generator(scenario)
    events = gen.run()
    split = scenario.evaluate_from
    assert split is not None, "scenario needs evaluate_from"
    day1 = [e for e in events if e.ts_start < split]
    day2 = [e for e in events if e.ts_start >= split]

    conn = open_db(db_path)
    clock = SimClock(split)
    pipeline = build_pipeline(config, conn, clock, honour_pause=False)
    pipeline.process(day1)
    ensure_repo_projects(conn, clock)
    Resolver(conn, clock).resolve_all()
    learning = _score(conn, gen.labels, day1)

    corrections: list[str] = []
    truth = {str(k): v for k, v in gen.labels.items()}
    for group in inbox(conn, clock, days=7):
        labels = Counter(truth.get(eid, "none") for eid in group.event_ids)
        project, n = labels.most_common(1)[0]
        if n / len(group.event_ids) >= 0.8:
            assign_group(conn, clock, group, project)
            corrections.append(f"group {group.label} -> {project} ({len(group.event_ids)} items)")
        else:
            for eid in group.event_ids:
                assign_event(conn, clock, eid, truth.get(eid, "none"))
            corrections.append(f"items of {group.label} one by one (mixed: {dict(labels)})")

    end = max(e.ts_end or e.ts_start for e in events)
    clock = SimClock(end)
    pipeline = build_pipeline(config, conn, clock, honour_pause=False)
    pipeline.process(day2)
    ensure_repo_projects(conn, clock)
    resolver = Resolver(conn, clock)
    while conn.execute("SELECT 1 FROM events WHERE assigned_at IS NULL LIMIT 1").fetchone():
        resolver.resolve_pending()
    test = _score(conn, gen.labels, day2)
    conn.close()
    _ = to_ms
    return Report(learning, test, corrections)
