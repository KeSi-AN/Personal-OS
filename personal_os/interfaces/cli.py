"""`personal-os` command line (phase 0 skeleton).

In phase 0 the CLI works directly on the database. From phase 1, commands that affect the
running daemon (pause, forget) also notify it over the local API.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from personal_os.config.loader import config_path, default_home, ensure_dirs, load_config
from personal_os.config.schema import Config
from personal_os.core import pause as pause_mod
from personal_os.core.clock import SystemClock, from_ms
from personal_os.core.events import Event
from personal_os.core.logging import setup_logging
from personal_os.core.wiring import build_pipeline
from personal_os.sim.generator import generate
from personal_os.sim.replay import dump_jsonl, feed_through_bus, load_jsonl
from personal_os.sim.scenario import load_scenario, parse_duration
from personal_os.storage import maintenance, repo
from personal_os.storage.db import open_db, schema_version

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Local-first personal context.")
sim_app = typer.Typer(no_args_is_help=True, help="Fake desktop simulator.")
config_app = typer.Typer(no_args_is_help=True, help="Show configuration.")
app.add_typer(sim_app, name="sim")
app.add_typer(config_app, name="config")

DbOption = Annotated[
    Path | None, typer.Option("--db", help="Database file (default: the one in the data dir).")
]


class _State:
    home: Path | None = None


_state = _State()


@app.callback()
def _root(
    home: Annotated[
        Path | None,
        typer.Option("--home", envvar="PERSONAL_OS_HOME", help="Data directory override."),
    ] = None,
) -> None:
    _state.home = home


def _config() -> Config:
    cfg = load_config(_state.home)
    ensure_dirs(cfg)
    setup_logging(cfg.logs_dir)
    return cfg


def _open(cfg: Config, db: Path | None) -> tuple[sqlite3.Connection, Path]:
    path = db or cfg.db_path
    return open_db(path), path


def _fmt_ts(ms: int | None, offset_min: int = 0) -> str:
    if ms is None:
        return "-"
    return from_ms(ms).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _summary(e: Event) -> str:
    p = e.payload
    for attr in ("title", "command", "message", "path", "url", "text", "repo_root", "app"):
        value = getattr(p, attr, None)
        if isinstance(value, str) and value:
            return value.replace("\n", " ")[:90]
    return ""


def _parse_when(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.astimezone()


# --- status / inspect ----------------------------------------------------------------------


@app.command()
def status(db: DbOption = None) -> None:
    """Data location, schema version, event counts and pause state."""
    cfg = _config()
    conn, path = _open(cfg, db)
    clock = SystemClock()
    total, last = conn.execute("SELECT count(*), max(ts_start) FROM events").fetchone()
    present, until = pause_mod.paused_until(conn)
    paused = pause_mod.is_paused(conn, clock)
    typer.echo(f"data dir      {cfg.data_dir}")
    typer.echo(f"database      {path}")
    typer.echo(f"schema        v{schema_version(conn)}")
    typer.echo(f"events        {total}")
    typer.echo(f"last event    {_fmt_ts(last)}")
    if paused:
        until_text = f" until {until.astimezone():%H:%M}" if until else ""
        typer.echo(f"capture       PAUSED{until_text}")
    else:
        typer.echo("capture       active" + (" (pause expired)" if present else ""))
    typer.echo("daemon        not built yet (phase 1)")


@app.command()
def inspect(db: DbOption = None) -> None:
    """What is stored: rows and bytes per table, source and privacy class."""
    cfg = _config()
    conn, path = _open(cfg, db)
    info = maintenance.inspect(conn, path)
    typer.echo(
        f"database {path}  ({info.db_bytes / 1024:.0f} KiB, WAL {info.wal_bytes / 1024:.0f} KiB)"
    )
    span = f"{_fmt_ts(info.oldest_ms)} -> {_fmt_ts(info.newest_ms)}"
    typer.echo(f"schema v{info.schema_version}   range {span}")
    typer.echo("")
    for table, rows in info.tables.items():
        typer.echo(f"  {table:<16}{rows:>8} rows")
    typer.echo("")
    typer.echo(f"  {'source':<12}{'class':<11}{'rows':>8}{'bytes':>10}")
    for source, cls, rows, size in info.by_source:
        typer.echo(f"  {source:<12}{cls:<11}{rows:>8}{size:>10}")
    counters = repo.get_counters(conn)
    if counters:
        typer.echo("")
        typer.echo("  counters (all days)")
        for scope, values in counters.items():
            joined = ", ".join(f"{k}={v}" for k, v in values.items())
            typer.echo(f"    {scope:<20}{joined}")


@app.command()
def events(
    db: DbOption = None,
    last: Annotated[int, typer.Option(help="How many of the newest events to show.")] = 20,
    source: Annotated[str | None, typer.Option(help="Only this source.")] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print full events as JSON lines.")
    ] = False,
) -> None:
    """List recent stored events."""
    cfg = _config()
    conn, _ = _open(cfg, db)
    rows = list(repo.iter_events(conn, source=source, limit=last, newest_first=True))
    for e in reversed(rows):
        if as_json:
            typer.echo(e.model_dump_json())
            continue
        start = e.ts_start.astimezone().strftime("%m-%d %H:%M:%S")
        dur = ""
        if e.ts_end is not None and e.ts_end != e.ts_start:
            dur = f"{(e.ts_end - e.ts_start).total_seconds():>6.0f}s"
        elif e.ts_end is None:
            dur = "  open"
        flag = " [S]" if e.privacy_class == "sensitive" else ""
        typer.echo(f"{start} {dur:>7}  {e.kind:<20}{flag} {_summary(e)}")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Words to look for.")],
    db: DbOption = None,
    limit: int = 10,
) -> None:
    """Full-text search over stored events and page text."""
    cfg = _config()
    conn, _ = _open(cfg, db)
    hits = repo.search(conn, query, limit)
    if not hits:
        typer.echo("No matches.")
        return
    for e, _score in hits:
        when = e.ts_start.astimezone().strftime("%Y-%m-%d %H:%M")
        typer.echo(f"{when}  {e.kind:<20} {_summary(e)}")


# --- privacy controls ----------------------------------------------------------------------


@app.command("pause")
def pause_cmd(
    for_: Annotated[
        str | None,
        typer.Option("--for", help="Pause for a duration, e.g. 30m. Default: until resume."),
    ] = None,
    db: DbOption = None,
) -> None:
    """Stop capturing until `resume` (or for a set time)."""
    cfg = _config()
    conn, _ = _open(cfg, db)
    clock = SystemClock()
    duration = timedelta(seconds=parse_duration(for_)) if for_ else None
    marker = pause_mod.pause(conn, clock, duration=duration, runtime_dir=cfg.runtime_dir)
    build_pipeline(cfg, conn, clock).process([marker])
    typer.echo(
        "Capture paused" + (f" for {for_}." if for_ else " until you run `personal-os resume`.")
    )


@app.command()
def resume(db: DbOption = None) -> None:
    """Resume capturing."""
    cfg = _config()
    conn, _ = _open(cfg, db)
    clock = SystemClock()
    marker = pause_mod.resume(conn, clock, runtime_dir=cfg.runtime_dir)
    build_pipeline(cfg, conn, clock).process([marker])
    typer.echo("Capture resumed.")


@app.command("forget")
def forget_cmd(
    duration: Annotated[
        str | None, typer.Argument(help="Forget this much recent time, e.g. 15m or 2h.")
    ] = None,
    since: Annotated[
        str | None, typer.Option(help="Start (ISO time), instead of a duration.")
    ] = None,
    until: Annotated[str | None, typer.Option(help="End (ISO time). Default: now.")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
    db: DbOption = None,
) -> None:
    """Permanently delete everything captured in a time window."""
    cfg = _config()
    conn, _ = _open(cfg, db)
    clock = SystemClock()
    end = _parse_when(until) if until else clock.now()
    if since:
        start = _parse_when(since)
    elif duration:
        start = end - timedelta(seconds=parse_duration(duration))
    else:
        raise typer.BadParameter("give a duration (e.g. 15m) or --since")
    local = "%Y-%m-%d %H:%M:%S"
    window = f"{start.astimezone().strftime(local)} -> {end.astimezone().strftime(local)}"
    if not yes and not typer.confirm(f"Permanently delete everything captured {window}?"):
        raise typer.Abort()
    report = maintenance.forget(conn, start, end, clock)
    typer.echo(
        f"Forgot {report.events} events and {report.page_extracts} page extracts ({window})."
    )


# --- simulator -----------------------------------------------------------------------------


def _sim_db(cfg: Config, db: Path | None) -> Path:
    return db or cfg.data_dir / "sim.db"


@sim_app.command("run")
def sim_run(
    scenario: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    db: Annotated[
        Path | None, typer.Option("--db", help="Target database (default: sim.db in the data dir).")
    ] = None,
    fresh: Annotated[bool, typer.Option(help="Delete the target database first.")] = False,
) -> None:
    """Run a scenario through the real bus -> privacy filter -> storage pipeline."""
    cfg = _config()
    path = _sim_db(cfg, db)
    if path == cfg.db_path:
        raise typer.BadParameter("refusing to write simulated events into your real database")
    if fresh:
        maintenance.wipe(path)
    events_ = generate(load_scenario(scenario))
    conn = open_db(path)
    pipeline = build_pipeline(cfg, conn, SystemClock(), honour_pause=False)
    stored = asyncio.run(feed_through_bus(events_, pipeline))
    c = pipeline.privacy.counters
    typer.echo(f"scenario   {scenario.name}: {len(events_)} events generated")
    typer.echo(f"stored     {stored} (upserts of the same interval count once per write)")
    typer.echo(f"blocked    {dict(c.blocked) or 0}")
    typer.echo(f"sensitive  {sum(c.sensitive.values())}")
    typer.echo(f"redacted   {dict(c.redactions) or 0}")
    typer.echo(f"database   {path}")


@sim_app.command("export")
def sim_export(
    scenario: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    out: Annotated[Path, typer.Argument(help="Output .jsonl file.")],
) -> None:
    """Write a scenario's raw (unfiltered) events to JSON lines, for replay or inspection."""
    n = dump_jsonl(generate(load_scenario(scenario)), out)
    typer.echo(
        f"Wrote {n} events to {out}. Note: these are pre-filter and may contain planted secrets."
    )


@app.command()
def replay(
    file: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="Events as JSON lines.")
    ],
    db: Annotated[
        Path | None, typer.Option("--db", help="Target database (default: sim.db in the data dir).")
    ] = None,
) -> None:
    """Replay a recorded JSONL event stream through the pipeline."""
    cfg = _config()
    path = _sim_db(cfg, db)
    if path == cfg.db_path:
        raise typer.BadParameter("refusing to replay into your real database in phase 0")
    conn = open_db(path)
    pipeline = build_pipeline(cfg, conn, SystemClock(), honour_pause=False)
    stored = asyncio.run(feed_through_bus(load_jsonl(file), pipeline))
    typer.echo(f"Replayed {pipeline.stats.received} events, stored {stored}, into {path}.")


# --- config --------------------------------------------------------------------------------


@config_app.command("path")
def config_path_cmd() -> None:
    """Where config.toml lives."""
    typer.echo(str(config_path(_state.home or default_home())))


@config_app.command("show")
def config_show() -> None:
    """The effective configuration (after defaults and validation)."""
    cfg = _config()
    typer.echo(json.dumps(cfg.model_dump(mode="json"), indent=2))


def main() -> None:
    # Piped output on Windows uses the ANSI code page; never crash on a title we can't encode.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    app()


if __name__ == "__main__":
    main()
