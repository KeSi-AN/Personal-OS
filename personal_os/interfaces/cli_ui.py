"""Phase 6 commands: dashboard, setup, export, memory, reminders, llm warm/unload,
project delete."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from personal_os.config.loader import set_value
from personal_os.context import memory as memory_mod
from personal_os.core import runtime
from personal_os.core.clock import SystemClock
from personal_os.interfaces.cli import _config, _open, _state, app
from personal_os.interfaces.cli_projects import projects_app
from personal_os.storage import repo

memory_app = typer.Typer(no_args_is_help=True, help="Facts you've asked me to remember.")
app.add_typer(memory_app, name="memory")
reminders_app = typer.Typer(no_args_is_help=True, help="Reminders set with `do`.")
app.add_typer(reminders_app, name="reminders")

DbOption = Annotated[
    Path | None, typer.Option("--db", help="Database file (default: the one in the data dir).")
]
SETUP_KEY = "setup.reviewed"


@app.command("app")
def desktop_app(
    page: Annotated[str, typer.Argument(help="Page to show: /, /donna, /brief, /p ...")] = "/",
) -> None:
    """Open the dashboard in its own window (the desktop app)."""
    from personal_os.interfaces import app as desktop

    cfg = _config()
    if not runtime.daemon_info(cfg.runtime_dir).running:
        typer.echo("Capture isn't running; start it with `personal-os daemon start`.")
        raise typer.Exit(1)
    how = desktop.open_app(cfg, page)
    said = {"shown": "Shown in the open app.", "opened": "Opening the app."}
    typer.echo(said.get(how, "No app runtime here (WebView2), so it opened in your browser."))


@app.command("dashboard")
def dashboard(
    print_only: Annotated[bool, typer.Option("--print", help="Print the link only.")] = False,
) -> None:
    """Open the dashboard in your browser (signed in by a one-time link)."""
    from personal_os.web import auth

    cfg = _config()
    if not runtime.daemon_info(cfg.runtime_dir).running:
        typer.echo("Capture isn't running; start it with `personal-os daemon start`.")
        raise typer.Exit(1)
    conn, _ = _open(cfg, None)
    code = auth.new_login_code(conn, SystemClock())
    url = f"http://127.0.0.1:{cfg.api.port}/login?code={code}"
    if print_only or sys.platform != "win32":
        typer.echo(url)
        return
    import os

    os.startfile(url)  # type: ignore[attr-defined,unused-ignore]
    typer.echo("Opened the dashboard (the link works once, for 2 minutes).")


@app.command("setup")
def setup() -> None:
    """First-run wizard: what each source captures, and whether you want it."""
    from personal_os.web.pages import SOURCES

    cfg = _config()
    typer.echo("Personal OS captures context on this laptop only. For each source, here's what it")
    typer.echo("records; answer y to turn it on, n to turn it off, Enter to keep it as is.\n")
    for name, label, text in SOURCES:
        on = getattr(cfg.observers, name).enabled
        typer.echo(f"{label}  (now {'on' if on else 'off'})\n  {text}")
        if not sys.stdin.isatty():
            continue
        answer = typer.prompt("  Turn on?", default="y" if on else "n", show_default=True)
        want = answer.strip().lower().startswith("y")
        if want != on:
            cfg = set_value(f"observers.{name}.enabled", want, _state.home)
        typer.echo("")
    conn, _ = _open(cfg, None)
    repo.set_setting(conn, SETUP_KEY, True, SystemClock())
    typer.echo(
        "Done. Changes apply when capture restarts: "
        "`personal-os daemon stop; personal-os daemon start`."
    )
    typer.echo(
        "Also: `personal-os install-hooks` (terminal), `personal-os browser install` (browser)."
    )


@app.command("export")
def export(
    out: Annotated[Path, typer.Argument(help="Folder to write into (created if needed).")],
    fmt: Annotated[str, typer.Option("--format", help="json or markdown.")] = "json",
    project: Annotated[str | None, typer.Option(help="Only this project.")] = None,
    db: DbOption = None,
) -> None:
    """Export what's stored: JSON lines per table, or readable Markdown per project."""
    from personal_os.storage.export import export_json, export_markdown

    cfg = _config()
    conn, _ = _open(cfg, db)
    if fmt == "json":
        counts = export_json(conn, out, project)
        typer.echo(f"Wrote {sum(counts.values())} rows in {len(counts)} files to {out}.")
    elif fmt == "markdown":
        files = export_markdown(conn, out, project)
        typer.echo(f"Wrote {len(files)} Markdown file(s) to {out}.")
    else:
        raise typer.BadParameter("--format is json or markdown")


@projects_app.command("delete")
def projects_delete(
    project: str,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask.")] = False,
    db: DbOption = None,
) -> None:
    """Delete everything captured for a project (events, page text, summaries, rules)."""
    from personal_os.storage.export import delete_project_data

    cfg = _config()
    conn, _ = _open(cfg, db)
    n = conn.execute("SELECT count(*) FROM events WHERE project_id = ?", (project,)).fetchone()[0]
    if not yes and not typer.confirm(
        f"Permanently delete {n} events of {project} and everything made from them?"
    ):
        raise typer.Exit(1)
    counts = delete_project_data(conn, project)
    typer.echo(
        f"Deleted {counts['events']} events, {counts['sessions']} sessions, "
        f"{counts['summaries']} summaries. {project} is archived."
    )


# --- memory ---------------------------------------------------------------------------------


@memory_app.command("list")
def memory_list(db: DbOption = None) -> None:
    cfg = _config()
    conn, _ = _open(cfg, db)
    facts = memory_mod.all_facts(conn)
    if not facts:
        typer.echo(
            'Nothing yet. Add with `personal-os memory add "..."` or "remember that ..." in Ask.'
        )
    for f in facts:
        typer.echo(f"{f.id[-8:]}  {f.created.astimezone():%Y-%m-%d}  {f.text}")


@memory_app.command("add")
def memory_add(text: str, db: DbOption = None) -> None:
    cfg = _config()
    conn, _ = _open(cfg, db)
    f = memory_mod.add(conn, SystemClock(), text, "cli")
    typer.echo(f"Remembered ({f.id[-8:]}).")


@memory_app.command("edit")
def memory_edit(fact: str, text: str, db: DbOption = None) -> None:
    cfg = _config()
    conn, _ = _open(cfg, db)
    f = memory_mod.find(conn, fact)
    if f is None or not memory_mod.update(conn, SystemClock(), f.id, text):
        raise typer.BadParameter(f"no single fact matches {fact!r}")
    typer.echo("Updated.")


@memory_app.command("forget")
def memory_forget(fact: str, db: DbOption = None) -> None:
    cfg = _config()
    conn, _ = _open(cfg, db)
    f = memory_mod.find(conn, fact)
    if f is None or not memory_mod.delete(conn, f.id):
        raise typer.BadParameter(f"no single fact matches {fact!r}")
    typer.echo("Forgotten.")


# --- reminders ---------------------------------------------------------------------------------


@reminders_app.command("list")
def reminders_list(db: DbOption = None) -> None:
    from personal_os.actions import reminders

    cfg = _config()
    conn, _ = _open(cfg, db)
    items = reminders.pending(conn)
    if not items:
        typer.echo("No reminders waiting.")
    for r in items:
        typer.echo(f"{r.id[-8:]}  {r.due.astimezone():%a %d %b %H:%M}  {r.text}")


@reminders_app.command("cancel")
def reminders_cancel(reminder: str, db: DbOption = None) -> None:
    from personal_os.actions import reminders

    cfg = _config()
    conn, _ = _open(cfg, db)
    match = [r for r in reminders.pending(conn) if r.id.endswith(reminder)]
    if len(match) != 1 or not reminders.cancel(conn, match[0].id):
        raise typer.BadParameter(f"no single waiting reminder matches {reminder!r}")
    typer.echo("Cancelled.")


# --- llm warm/unload ------------------------------------------------------------------------------


def _llm_app() -> typer.Typer:
    from personal_os.interfaces.cli_context import llm_app

    return llm_app


@_llm_app().command("warm")
def llm_warm(
    minutes: Annotated[int, typer.Option("--for", help="Keep it loaded this many minutes.")] = 30,
) -> None:
    """Load the chat model now, so your next questions skip the cold start."""
    from personal_os.llm import LLMError, make_provider

    cfg = _config()
    started = datetime.now()
    try:
        make_provider(cfg.llm).warm(f"{minutes}m")
    except LLMError as exc:
        typer.echo(f"Couldn't load it: {exc}")
        raise typer.Exit(1) from None
    took = (datetime.now() - started).total_seconds()
    typer.echo(f"{cfg.llm.fast_model} is loaded for {minutes} minutes ({took:.0f} s).")


@_llm_app().command("unload")
def llm_unload() -> None:
    """Free the models' memory (GPU) now."""
    from personal_os.llm import LLMError, make_provider

    cfg = _config()
    try:
        make_provider(cfg.llm).unload()
    except LLMError as exc:
        typer.echo(f"Couldn't reach Ollama: {exc}")
        raise typer.Exit(1) from None
    typer.echo("Models unloaded.")
