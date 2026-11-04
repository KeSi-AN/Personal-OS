"""Phase 4 acceptance: `ask` on a labelled scenario with a fixed question set.

The database is built the phase 3 way (cold first day, simulated inbox corrections, the rest
resolved), then sessions are built, summarised and embedded with the given provider, and each
question is asked at the question file's `now`. A question passes when the route, the citations
and the wording meet its expectations, or when it correctly says there's no data.
"""

from __future__ import annotations

import re
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from personal_os.config.schema import Config
from personal_os.context.ask import Answer, ask
from personal_os.context.embeddings import embed_pending
from personal_os.context.summarizer import pending_sessions, summarize_session
from personal_os.core.clock import SimClock
from personal_os.llm.provider import LLMProvider
from personal_os.projects.evaluate import evaluate as build_projects
from personal_os.projects.sessions import rebuild_sessions
from personal_os.storage.db import open_db


@dataclass
class QuestionResult:
    question: str
    expect: str
    passed: bool
    answer: Answer
    seconds: float
    problems: list[str] = field(default_factory=list)


@dataclass
class AskReport:
    results: list[QuestionResult]
    summaries: int
    summary_failures: int
    embedded: int
    build_seconds: float

    def rate(self, expect: str) -> tuple[int, int]:
        sel = [r for r in self.results if r.expect == expect]
        return sum(r.passed for r in sel), len(sel)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[-_]", " ", text.lower()).split())


def check(spec: dict[str, Any], answer: Answer) -> list[str]:
    problems = []
    expect = spec.get("expect", "answer")
    if expect == "no_data":
        if answer.answered:
            problems.append(f"expected no data, got an answer ({answer.mode})")
        return problems
    if not answer.answered:
        return [f"expected an answer, got no data: {answer.text[:120]}"]
    q = answer.question
    if spec.get("intent") and q.intent != spec["intent"]:
        problems.append(f"intent {q.intent}, expected {spec['intent']}")
    if spec.get("project") and (q.project is None or q.project.id != spec["project"]):
        problems.append(
            f"project {q.project.id if q.project else None}, expected {spec['project']}"
        )
    cited = [_norm(x.text + " " + x.detail) for x in answer.cited()]
    for needle in spec.get("cite", []):
        if not any(_norm(needle) in c for c in cited):
            problems.append(f"no cited evidence mentions {needle!r}")
    text = _norm(answer.text)
    if spec.get("mention_any") and not any(_norm(m) in text for m in spec["mention_any"]):
        problems.append(f"answer mentions none of {spec['mention_any']}")
    for m in spec.get("mention_all", []):
        if _norm(m) not in text:
            problems.append(f"answer doesn't mention {m!r}")
    for m in spec.get("never", []):
        if _norm(m) in text:
            problems.append(f"answer says {m!r} (must not)")
    if not answer.cited():
        problems.append("answer cites nothing")
    return problems


def evaluate_ask(
    questions_path: Path, config: Config, db_path: Path, provider: LLMProvider | None
) -> AskReport:
    spec = tomllib.loads(questions_path.read_text(encoding="utf-8"))
    scenario = (questions_path.parent / spec["scenario"]).resolve()
    now: datetime = spec["now"]
    t0 = time.monotonic()
    build_projects(scenario, config, db_path)
    conn = open_db(db_path)
    clock = SimClock(now)
    rebuild_sessions(conn, clock, now - timedelta(days=10), now)
    summaries = failures = embedded = 0
    if provider is not None:
        min_active = config.llm.summarize_min_active_s * 1000
        for session, pack in pending_sessions(conn, clock, min_active_ms=min_active, days=10):
            if summarize_session(conn, provider, clock, session, pack=pack) is None:
                failures += 1
            else:
                summaries += 1
        embedded = embed_pending(conn, provider, clock, config.llm.embedding_dims, limit=5000)
    build_s = time.monotonic() - t0

    results = []
    for q in spec["questions"]:
        started = time.monotonic()
        answer = ask(conn, provider, q["q"], now)
        took = time.monotonic() - started
        problems = check(q, answer)
        results.append(
            QuestionResult(q["q"], q.get("expect", "answer"), not problems, answer, took, problems)
        )
    conn.close()
    return AskReport(results, summaries, failures, embedded, build_s)
