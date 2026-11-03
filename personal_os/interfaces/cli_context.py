"""Phase 4 commands: ask, summarize, llm status/embed."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from personal_os.context import ask as ask_mod
from personal_os.context.embeddings import count_pending, embed_pending
from personal_os.context.summarizer import (
    load_summaries,
    needs_summary,
    session_pack,
    summarize_session,
)
from personal_os.core.clock import SystemClock, from_ms
from personal_os.interfaces.cli import _config, _open, app, sim_app
from personal_os.llm import LLMError, LLMUnavailable, make_provider
from personal_os.projects.sessions import day_bounds, load_sessions, rebuild_sessions

llm_app = typer.Typer(no_args_is_help=True, help="Local model status and maintenance.")
app.add_typer(llm_app, name="llm")

DbOption = Annotated[
    Path | None, typer.Option("--db", help="Database file (default: the one in the data dir).")
]


@app.command("ask")
def ask_cmd(
    question: Annotated[str, typer.Argument(help='e.g. "where did I leave off on personal-os?"')],
    think: Annotated[
        bool, typer.Option("--think", help="Let the model reason first (about a minute).")
    ] = False,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Don't use the model: show the facts found.")
    ] = False,
    all_evidence: Annotated[
        bool, typer.Option("--all", help="Show every evidence line, not only the cited ones.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    db: DbOption = None,
) -> None:
    """Ask about your own activity. Answers cite numbered evidence from what was captured."""
    cfg = _config()
    conn, _ = _open(cfg, db)
    provider = None if (no_llm or not cfg.llm.enabled) else make_provider(cfg.llm)
    started = time.monotonic()
    answer = ask_mod.ask(conn, provider, question, SystemClock().now(), think=think)
    took = time.monotonic() - started
    lines = answer.gathered.pack.lines if all_evidence else answer.cited()
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "question": question,
                    "intent": answer.question.intent,
                    "project": answer.question.project.id if answer.question.project else None,
                    "range": answer.question.range_label,
                    "mode": answer.mode,
                    "answered": answer.answered,
                    "sentences": [{"text": s, "refs": r} for s, r in answer.sentences],
                    "confidence": answer.confidence,
                    "evidence": [
                        {
                            "ref": x.ref,
                            "ts": x.ts.isoformat(),
                            "kind": x.kind,
                            "text": x.text,
                            "event_ids": x.event_ids,
                        }
                        for x in lines
                    ],
                    "notes": answer.notes,
                },
                indent=2,
            )
        )
        return
    typer.echo(answer.text)
    if not answer.answered:
        return
    typer.echo(f"\nConfidence: {answer.confidence}.")
    if answer.gathered.facts and answer.mode == "llm":
        typer.echo("Facts: " + " ".join(answer.gathered.facts))
    if lines:
        typer.echo("\nEvidence:")
        for x in lines:
            ids = ", ".join(i[:8] for i in x.event_ids[:3])
            more = f" +{len(x.event_ids) - 3}" if len(x.event_ids) > 3 else ""
            typer.echo(f" [{x.ref}] {x.ts.astimezone():%m-%d %H:%M} {x.kind:<14} {x.text}")
            if ids:
                typer.echo(f"      events {ids}{more}")
    for note in answer.notes:
        typer.echo(f"\nnote: {note}")
    typer.echo(f"\n({answer.question.intent}, {took:.0f} s, {answer.mode})")


@app.command("summarize")
def summarize_cmd(
    day: Annotated[str | None, typer.Option(help="YYYY-MM-DD (default: today).")] = None,
    force: Annotated[bool, typer.Option(help="Summarise again even if up to date.")] = False,
    db: DbOption = None,
) -> None:
    """Summarise a day's finished sessions now (the daemon does this when you're away)."""
    cfg = _config()
    conn, _ = _open(cfg, db)
    clock = SystemClock()
    provider = make_provider(cfg.llm)
    when = datetime.fromisoformat(day).astimezone() if day else clock.now()
    start, end = day_bounds(when)
    rebuild_sessions(conn, clock, start, min(end, clock.now()))
    done = 0
    for s in load_sessions(conn, start, end):
        if s.active_ms < cfg.llm.summarize_min_active_s * 1000:
            continue
        pack = session_pack(conn, s)
        if not pack or not (force or needs_summary(conn, s, pack)):
            continue
        span = f"{s.start.astimezone():%H:%M}-{s.end.astimezone():%H:%M}"
        t0 = time.monotonic()
        try:
            summary = summarize_session(conn, provider, clock, s, pack=pack)
        except LLMUnavailable as exc:
            typer.echo(f"Ollama isn't available: {exc}")
            raise typer.Exit(1) from None
        took = time.monotonic() - t0
        call = getattr(provider, "last_call", {}) or {}
        how = f"{took:.0f} s, {call.get('attempts', 1)} attempt(s)"
        if summary is None:
            reason = call.get("error") or "no grounded items"
            typer.echo(f"{span}  {s.project_id or '-'}: couldn't be grounded ({how}: {reason})")
            continue
        done += 1
        typer.echo(f"{span}  {s.project_id or '-'}  ({how})\n   goal: {summary.goal}")
        if summary.next_step:
            typer.echo(f"   next: {summary.next_step.text}")
    typer.echo(f"\n{done} session(s) summarised." if done else "Nothing to summarise.")


@llm_app.command("status")
def llm_status(db: DbOption = None) -> None:
    """Is Ollama up, are the models there, and how much is waiting to be summarised/embedded."""
    cfg = _config()
    conn, _ = _open(cfg, db)
    provider = make_provider(cfg.llm)
    h = provider.health()
    typer.echo(
        f"ollama       {'reachable' if h.reachable else 'NOT reachable'} ({cfg.llm.base_url})"
    )
    if h.error:
        typer.echo(f"             {h.error}")
    for model in (cfg.llm.fast_model, cfg.llm.embedding_model):
        state = "missing" if model in h.missing else ("installed" if h.reachable else "?")
        typer.echo(f"model        {model:<24} {state}")
    for name, vram in h.loaded:
        typer.echo(f"loaded       {name:<24} {vram / 1e9:.1f} GB in VRAM")
    now = SystemClock().now()
    week = load_summaries(conn, now - timedelta(days=7), now)
    total = conn.execute("SELECT count(*) FROM summaries WHERE status = 'ok'").fetchone()[0]
    failed = conn.execute("SELECT count(*) FROM summaries WHERE status = 'failed'").fetchone()[0]
    typer.echo(f"summaries    {total} stored ({len(week)} this week, {failed} failed)")
    vectors = conn.execute("SELECT count(*) FROM embeddings").fetchone()[0]
    pending = count_pending(conn, cfg.llm.embedding_model, cfg.llm.embedding_dims)
    typer.echo(f"embeddings   {vectors} stored, {pending} waiting")
    last = conn.execute("SELECT max(created_at) FROM summaries").fetchone()[0]
    if last:
        typer.echo(f"last summary {from_ms(last).astimezone():%Y-%m-%d %H:%M}")


@llm_app.command("embed")
def llm_embed(
    limit: Annotated[int, typer.Option(help="At most this many items.")] = 2000,
    db: DbOption = None,
) -> None:
    """Embed waiting pages, searches, commits and summaries now (the daemon does this idle)."""
    cfg = _config()
    conn, _ = _open(cfg, db)
    provider = make_provider(cfg.llm)
    t0 = time.monotonic()
    try:
        n = embed_pending(conn, provider, SystemClock(), cfg.llm.embedding_dims, limit=limit)
    except LLMError as exc:
        typer.echo(f"Embedding failed: {exc}")
        raise typer.Exit(1) from None
    typer.echo(f"Embedded {n} item(s) in {time.monotonic() - t0:.0f} s.")


@sim_app.command("eval-ask")
def eval_ask(
    questions: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Route and gather only; answers from facts.")
    ] = False,
) -> None:
    """Phase 4 acceptance: ask a labelled question set about a simulated scenario."""
    import tempfile

    from personal_os.context.evaluate import evaluate_ask

    cfg = _config()
    provider = None if no_llm else make_provider(cfg.llm)
    with tempfile.TemporaryDirectory() as d:
        report = evaluate_ask(questions, cfg, Path(d) / "ask.db", provider)
    typer.echo(
        f"built in {report.build_seconds:.0f} s: {report.summaries} summaries "
        f"({report.summary_failures} failed), {report.embedded} embeddings\n"
    )
    for r in report.results:
        mark = "PASS" if r.passed else "FAIL"
        a = r.answer
        typer.echo(f"{mark}  {r.question}  [{a.question.intent}, {a.mode}, {r.seconds:.0f} s]")
        typer.echo(f"      {a.text[:400]}")
        for p in r.problems:
            typer.echo(f"      x {p}")
    ok_a, n_a = report.rate("answer")
    ok_n, n_n = report.rate("no_data")
    typer.echo(f"\nanswerable: {ok_a}/{n_a}   no-data: {ok_n}/{n_n}")
