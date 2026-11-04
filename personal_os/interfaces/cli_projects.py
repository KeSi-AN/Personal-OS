"""Phase 3 commands: projects, inbox, assign, resolve, sessions, and the labelled evaluation."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from personal_os.core.clock import SystemClock, to_ms
from personal_os.interfaces.cli import _config, _open, app, sim_app
from personal_os.projects import inbox as inbox_mod
from personal_os.projects import registry
from personal_os.projects.resolver import Resolver
from personal_os.projects.sessions import day_bounds, load_sessions, rebuild_sessions

projects_app = typer.Typer(
    help="Projects: list, add, rename, archive, keywords.", invoke_without_command=True
)
app.add_typer(projects_app, name="projects")

DbOption = Annotated[
    Path | None, typer.Option("--db", help="Database file (default: the one in the data dir).")
]
INBOX_CACHE = "inbox.json"


def _conn(db: Path | None) -> tuple[sqlite3.Connection, Path]:
    cfg = _config()
    conn, _ = _open(cfg, db)
    return conn, cfg.runtime_dir


def _project_or_exit(conn: sqlite3.Connection, text: str) -> registry.Project:
    p = registry.find_project(conn, text)
    if p is None:
        names = ", ".join(x.id for x in registry.load_projects(conn))
        raise typer.BadParameter(f"no single project matches {text!r}. Projects: {names}")
    return p


def _fmt_s(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


# --- projects ------------------------------------------------------------------------------


@projects_app.callback()
def projects_list(
    ctx: typer.Context,
    db: DbOption = None,
    days: Annotated[int, typer.Option(help="Activity window for the counts.")] = 7,
) -> None:
    """List projects with their activity over the last few days."""
    if ctx.invoked_subcommand is not None:
        return
    conn, _ = _conn(db)
    registry.ensure_repo_projects(conn, SystemClock())
    since = to_ms(SystemClock().now() - timedelta(days=days))
    counts = {
        pid: (n, s or 0)
        for pid, n, s in conn.execute(
            "SELECT project_id, count(*), sum(active_ms) / 1000.0 FROM ("
            "SELECT project_id, 0 AS active_ms FROM events WHERE ts_start >= ? "
            "UNION ALL SELECT project_id, active_ms FROM sessions WHERE ts_start >= ?) "
            "GROUP BY project_id",
            (since, since),
        )
    }
    typer.echo(f"{'id':<28}{'events':>8}{'time':>8}  source  roots")
    for p in registry.load_projects(conn):
        n, secs = counts.get(p.id, (0, 0))
        roots = ", ".join(p.roots) or "-"
        typer.echo(f"{p.id:<28}{n:>8}{_fmt_s(secs):>8}  {p.source:<7} {roots[:60]}")
    unassigned = conn.execute(
        "SELECT count(*) FROM events WHERE assign_source = 'unassigned' AND ts_start >= ?", (since,)
    ).fetchone()[0]
    typer.echo(f"\nunassigned events (last {days} days): {unassigned}. See `personal-os inbox`.")


@projects_app.command("add")
def projects_add(
    name: str,
    root: Annotated[list[str] | None, typer.Option("--root", help="Folder (repeatable).")] = None,
    keyword: Annotated[
        list[str] | None, typer.Option("--keyword", help="Word (repeatable).")
    ] = None,
    db: DbOption = None,
) -> None:
    """Add a project that isn't a git repo (or group several folders)."""
    conn, _ = _conn(db)
    p = registry.add_project(conn, SystemClock(), name, root or [], keyword or [])
    Resolver(conn, SystemClock()).resolve_all(since=SystemClock().now() - timedelta(days=30))
    typer.echo(f"Added project {p.id}. Re-assigned the last 30 days.")


@projects_app.command("rename")
def projects_rename(project: str, name: str, db: DbOption = None) -> None:
    conn, _ = _conn(db)
    p = _project_or_exit(conn, project)
    registry.update_project(conn, p.id, name=name)
    typer.echo(f"{p.id} is now called {name!r} (its id stays {p.id}).")


@projects_app.command("keyword")
def projects_keyword(project: str, words: list[str], db: DbOption = None) -> None:
    """Add words that identify a project (used to match pages and titles)."""
    conn, _ = _conn(db)
    p = _project_or_exit(conn, project)
    registry.update_project(conn, p.id, add_keywords=words)
    Resolver(conn, SystemClock()).resolve_all(since=SystemClock().now() - timedelta(days=30))
    typer.echo(f"Added {len(words)} keyword(s) to {p.id}. Re-assigned the last 30 days.")


@projects_app.command("archive")
def projects_archive(project: str, undo: bool = False, db: DbOption = None) -> None:
    """Archive a project (no new events go to it; history is kept)."""
    conn, _ = _conn(db)
    p = _project_or_exit(conn, project)
    registry.update_project(conn, p.id, archived=not undo)
    typer.echo(f"{p.id} {'restored' if undo else 'archived'}.")


# --- inbox and corrections ------------------------------------------------------------------


@app.command("inbox")
def inbox_cmd(
    days: Annotated[int, typer.Option(help="How far back to look.")] = 7,
    db: DbOption = None,
) -> None:
    """Unassigned items and weak guesses, grouped so one `assign` fixes many."""
    conn, runtime_dir = _conn(db)
    groups = inbox_mod.inbox(conn, SystemClock(), days=days)
    if not groups:
        typer.echo("Inbox is empty: everything has a confident project.")
        return
    cache = []
    for i, g in enumerate(groups, start=1):
        guess = ", ".join(f"{k}x{v}" for k, v in g.guesses.most_common(2) if k != "-")
        guess = f"  guess: {guess}" if guess else ""
        typer.echo(
            f"{i:>3}. {g.label}  ({g.count} item{'s' * (g.count != 1)}, {_fmt_s(g.seconds)}){guess}"
        )
        for sample in g.samples:
            typer.echo(f"       {sample}")
        cache.append({"kind": g.kind, "key": g.key, "event_ids": g.event_ids})
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / INBOX_CACHE).write_text(json.dumps(cache), encoding="utf-8")
    typer.echo("\nAssign with: personal-os assign <number> <project>   (use 'none' for not-work)")


@app.command("assign")
def assign_cmd(
    what: Annotated[
        str, typer.Argument(help="Inbox number, group label (domain:x.com) or event id.")
    ],
    project: Annotated[
        str, typer.Argument(help="Project id or name; 'none' for not project work.")
    ],
    learn: Annotated[
        bool, typer.Option("--learn", help="For a single event: also learn a rule.")
    ] = False,
    db: DbOption = None,
) -> None:
    """Correct the project of an inbox group (and learn from it) or of one event."""
    conn, runtime_dir = _conn(db)
    clock = SystemClock()
    p = _project_or_exit(conn, project)
    group: inbox_mod.InboxGroup | None = None
    if what.isdigit():
        try:
            cache = json.loads((runtime_dir / INBOX_CACHE).read_text("utf-8"))
            entry = cache[int(what) - 1]
        except (OSError, ValueError, IndexError):
            raise typer.BadParameter(
                "run `personal-os inbox` first, then use one of its numbers"
            ) from None
        group = inbox_mod.InboxGroup(entry["kind"], entry["key"], event_ids=entry["event_ids"])
    elif ":" in what and not _looks_like_uuid(what):
        kind, _, key = what.partition(":")
        current = {g.label: g for g in inbox_mod.inbox(conn, clock, days=30)}
        group = current.get(what) or inbox_mod.InboxGroup(kind, key)
    if group is not None:
        n = inbox_mod.assign_group(conn, clock, group, p.id)
        typer.echo(f"Assigned {n} item(s) of {group.label} to {p.id} and learned the rule.")
        return
    try:
        inbox_mod.assign_event(conn, clock, what, p.id, learn_rule=learn)
    except KeyError:
        raise typer.BadParameter(f"no event {what}") from None
    typer.echo(f"Assigned event {what} to {p.id}{' and learned a rule' if learn else ''}.")


def _looks_like_uuid(text: str) -> bool:
    return len(text) == 36 and text.count("-") == 4


@app.command("resolve")
def resolve_cmd(
    all_: Annotated[
        bool, typer.Option("--all", help="Re-decide everything you haven't corrected.")
    ] = False,
    db: DbOption = None,
) -> None:
    """Assign projects now (the daemon also does this every 30 seconds)."""
    conn, _ = _conn(db)
    clock = SystemClock()
    created = registry.ensure_repo_projects(conn, clock)
    resolver = Resolver(conn, clock)
    n = resolver.resolve_all() if all_ else resolver.resolve_pending()
    typer.echo(f"{created} new project(s); looked at {n} event(s).")


# --- sessions -------------------------------------------------------------------------------


@app.command("sessions")
def sessions_cmd(
    day: Annotated[str | None, typer.Option(help="YYYY-MM-DD (default: today).")] = None,
    rebuild: Annotated[bool, typer.Option(help="Rebuild from events first.")] = True,
    db: DbOption = None,
) -> None:
    """Work sessions of a day: when, which project, and what happened."""
    conn, _ = _conn(db)
    clock = SystemClock()
    when = datetime.fromisoformat(day).astimezone() if day else clock.now()
    start, end = day_bounds(when)
    sessions = (
        rebuild_sessions(conn, clock, start, end) if rebuild else load_sessions(conn, start, end)
    )
    if not sessions:
        typer.echo("No sessions for that day.")
        return
    from personal_os.context.summarizer import load_summaries

    summaries = {x.session_id: x for x in load_summaries(conn, start, end)}
    for s in sessions:
        a, b = s.start.astimezone().strftime("%H:%M"), s.end.astimezone().strftime("%H:%M")
        typer.echo(
            f"{a}-{b}  {s.project_id or 'unassigned':<26} active {_fmt_s(s.active_ms / 1000):>6}"
            f"  confidence {s.confidence:.0%}"
        )
        summary = summaries.get(s.id)
        if summary is not None:
            typer.echo(f"   goal:     {summary.body.goal}")
            if summary.body.next_step:
                typer.echo(f"   next:     {summary.body.next_step.text}")
        st = s.stats
        if st.get("apps"):
            typer.echo(
                "   apps:     "
                + ", ".join(f"{k} {_fmt_s(v)}" for k, v in list(st["apps"].items())[:4])
            )
        if st.get("files"):
            typer.echo("   files:    " + ", ".join(list(st["files"])[:5]))
        if st.get("pages"):
            typer.echo("   pages:    " + " | ".join(t[:40] for t in st["pages"][:3]))
        if st.get("commands"):
            typer.echo(f"   commands: {st['commands']} ({st.get('failed_commands', 0)} failed)")
        for c in st.get("commits", [])[:3]:
            typer.echo(f"   commit:   {c}")


# --- evaluation ------------------------------------------------------------------------------


@sim_app.command("eval-projects")
def eval_projects(
    scenario: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    show_mistakes: bool = True,
) -> None:
    """Project-assignment accuracy on a labelled scenario (phase 3 acceptance)."""
    from personal_os.projects.evaluate import evaluate

    cfg = _config()
    with tempfile.TemporaryDirectory() as d:
        report = evaluate(scenario, cfg, Path(d) / "eval.db")
    for name, s in (
        ("learning day (cold start)", report.learning_day),
        ("test day", report.test_day),
    ):
        typer.echo(
            f"{name}: {s.correct}/{s.total} = {s.accuracy:.1%} of events, "
            f"{s.time_accuracy:.1%} of time"
        )
        if show_mistakes:
            for m in s.mistakes:
                typer.echo(f"   x {m}")
    typer.echo("simulated corrections between the days:")
    for c in report.corrections:
        typer.echo(f"   {c}")
