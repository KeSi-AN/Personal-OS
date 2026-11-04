"""Phase 5 commands: do, and actions list/run/log/undo/kill/resume/unlock/lock."""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from personal_os.actions import killswitch
from personal_os.actions.audit import find_action, latest
from personal_os.actions.base import Origin
from personal_os.actions.desktop import default_desktop
from personal_os.actions.executor import Executor, Outcome
from personal_os.actions.tools import build_registry
from personal_os.config.schema import Config
from personal_os.core.clock import SystemClock
from personal_os.interfaces.cli import _config, _open, app

actions_app = typer.Typer(no_args_is_help=True, help="Tools the assistant can use, and their log.")
app.add_typer(actions_app, name="actions")

DbOption = Annotated[
    Path | None, typer.Option("--db", help="Database file (default: the one in the data dir).")
]


def keyboard_confirm(prompt: str) -> bool:
    """Only a 'y' typed at a real keyboard counts. Anything else, or no keyboard, is a no."""
    if not sys.stdin.isatty():
        typer.echo(prompt + "(no keyboard: declined)")
        return False
    answer = typer.prompt(prompt, default="n", show_default=False, prompt_suffix="")
    return answer.strip().lower() in ("y", "yes")


def _executor(cfg: Config, db: Path | None, *, with_model: bool = False) -> Executor:
    conn, _ = _open(cfg, db)
    provider = None
    if with_model and cfg.llm.enabled:
        from personal_os.llm import make_provider

        provider = make_provider(cfg.llm)
    return Executor(
        cfg, conn, SystemClock(), build_registry(cfg), default_desktop(), keyboard_confirm,
        provider=provider,
    )  # fmt: skip


def _show(out: Outcome, *, show_preview: bool = False) -> None:
    mark = {"succeeded": "done", "dry_run": "would run", "declined": "declined",
            "blocked": "BLOCKED", "failed": "FAILED", "cancelled": "CANCELLED",
            "invalid": "invalid", "pending": "waiting for approval"}[out.status]  # fmt: skip
    typer.echo(f"[{mark}] {out.tool}: {out.message}  (action {out.action_id[-8:]})")
    if show_preview and out.preview:
        typer.echo("\n".join(f"    {line}" for line in out.preview.splitlines()))
    if out.result is not None and out.result.data and out.status == "succeeded":
        text = str(out.result.data)
        typer.echo("\n".join(f"    {line}" for line in text.splitlines()[:40]))


@app.command("do")
def do_cmd(
    request: Annotated[str, typer.Argument(help='e.g. "open auth-service in vscode"')],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the plan and what would happen; do nothing.")
    ] = False,
    db: DbOption = None,
) -> None:
    """Ask the assistant to do something. Tier 2+ steps always ask you first."""
    from personal_os.actions.planner import run_request
    from personal_os.llm import make_provider

    cfg = _config()
    if killswitch.engaged(cfg.runtime_dir):
        typer.echo("The kill switch is on (observe-only). `personal-os actions resume` to undo it.")
        raise typer.Exit(1)
    ex = _executor(cfg, db, with_model=True)
    report = run_request(request, ex, make_provider(cfg.llm), dry_run=dry_run)
    if report.reply:
        typer.echo(report.reply)
    for out in report.outcomes:
        _show(out, show_preview=dry_run)
    if report.tainted:
        typer.echo("(steps after a lookup were treated as content-derived: tier 1 asked first)")
    if report.error:
        typer.echo(f"Stopped: {report.error}")
        raise typer.Exit(1)
    if not report.outcomes and not report.reply:
        typer.echo("Nothing to do.")


@actions_app.command("list")
def actions_list() -> None:
    """Every tool, its tier, and what it does."""
    cfg = _config()
    unlocked = killswitch.unlocks(cfg.runtime_dir, SystemClock().now())
    tiers = {0: "read-only, runs", 1: "low-risk, runs when you ask", 2: "asks every time",
             3: "blocked"}  # fmt: skip
    for tool in build_registry(cfg).all():
        if getattr(tool, "internal", False):
            continue
        state = tiers[int(tool.tier)]
        if tool.name in unlocked:
            state = f"unlocked until {unlocked[tool.name].astimezone():%H:%M}, asks"
        typer.echo(f"{tool.signature():<46} tier {int(tool.tier)}  {state:<28} {tool.summary}")
    if killswitch.engaged(cfg.runtime_dir):
        typer.echo("\nKill switch is ON: nothing runs until `personal-os actions resume`.")


def _parse_args(pairs: list[str] | None, raw_json: str | None) -> dict[str, Any]:
    args: dict[str, Any] = json.loads(raw_json) if raw_json else {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep:
            raise typer.BadParameter(f"--arg needs key=value, got {pair!r}")
        try:
            args[key] = json.loads(value)  # numbers, booleans, lists
        except ValueError:
            args[key] = value
    return args


@actions_app.command("run")
def actions_run(
    tool: str,
    arg: Annotated[list[str] | None, typer.Option("--arg", help="key=value (repeatable).")] = None,
    args_json: Annotated[str | None, typer.Option("--json", help="Arguments as JSON.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would happen.")] = False,
    db: DbOption = None,
) -> None:
    """Run one tool directly, e.g. `actions run open_path --arg path=~/Documents`."""
    cfg = _config()
    ex = _executor(cfg, db)
    out = ex.request(tool, _parse_args(arg, args_json), origin=Origin.USER, dry_run=dry_run)
    _show(out, show_preview=dry_run)
    if out.status not in ("succeeded", "dry_run"):
        raise typer.Exit(1)


@actions_app.command("log")
def actions_log(
    last: Annotated[int, typer.Option(help="How many actions.")] = 15,
    db: DbOption = None,
) -> None:
    """The audit log: every action proposed, what was decided, and what happened."""
    cfg = _config()
    conn, _ = _open(cfg, db)
    actions = latest(conn, last)
    if not actions:
        typer.echo("No actions yet.")
        return
    for stages in reversed(actions):
        first, final = stages[0], stages[-1]
        flow = " > ".join(s.stage for s in stages)
        who = first.origin + (", after captured content" if first.tainted else "")
        typer.echo(
            f"{first.ts.astimezone():%m-%d %H:%M:%S}  {first.action_id[-8:]}  {first.tool:<17} "
            f"tier {first.tier}  {who}"
        )
        typer.echo(f"    {flow}")
        args = json.dumps(first.args)
        typer.echo(f"    args: {args[:160]}")
        if final.detail:
            typer.echo(f"    {final.stage}: {final.detail.splitlines()[0][:160]}")
        if first.request:
            typer.echo(f"    request: {first.request[:120]}")
        if any(s.undo for s in stages) and not any(s.stage == "undone" for s in stages):
            typer.echo(f"    undo with: personal-os actions undo {first.action_id[-8:]}")


@actions_app.command("undo")
def actions_undo(action: str, db: DbOption = None) -> None:
    """Reverse an action (its undo step goes through the same checks and confirmations)."""
    cfg = _config()
    ex = _executor(cfg, db)
    full = find_action(ex.conn, action)
    if full is None:
        raise typer.BadParameter(f"no single action starts with {action!r}")
    out = ex.undo(full)
    _show(out)
    if not out.ok:
        raise typer.Exit(1)


@actions_app.command("kill")
def actions_kill() -> None:
    """Stop running actions now and go observe-only (also Ctrl+Alt+Shift+K)."""
    cfg = _config()
    killswitch.engage(cfg.runtime_dir, SystemClock().now(), "cli")
    killswitch.relock_all(cfg.runtime_dir)
    typer.echo("Kill switch ON: running actions are being stopped, nothing new will run.")
    typer.echo("Undo with `personal-os actions resume`.")


@actions_app.command("resume")
def actions_resume() -> None:
    """Leave observe-only mode."""
    cfg = _config()
    was = killswitch.release(cfg.runtime_dir)
    typer.echo("Kill switch off: actions can run again." if was else "The kill switch wasn't on.")


@actions_app.command("unlock")
def actions_unlock(
    tool: str,
    minutes: Annotated[int, typer.Option(min=1, max=60, help="For how long.")] = 10,
) -> None:
    """Allow a blocked (tier 3) tool for a while. Each use still asks."""
    cfg = _config()
    t = build_registry().get(tool)
    if t is None or int(t.tier) != 3:
        raise typer.BadParameter(f"{tool} isn't a tier 3 tool")
    if not keyboard_confirm(f"Unlock {tool} ({t.summary}) for {minutes} minutes? [y/N] "):
        typer.echo("Not unlocked.")
        raise typer.Exit(1)
    now = SystemClock().now()
    killswitch.unlock(cfg.runtime_dir, tool, now + timedelta(minutes=minutes), now)
    typer.echo(f"{tool} unlocked for {minutes} minutes; each use still asks.")


@actions_app.command("lock")
def actions_lock() -> None:
    """Re-block every unlocked tier 3 tool now."""
    killswitch.relock_all(_config().runtime_dir)
    typer.echo("All tier 3 tools are blocked again.")
