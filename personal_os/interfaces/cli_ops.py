"""Phase 1 commands: daemon control, doctor, shell hooks, autostart, config set."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import psutil
import typer

from personal_os.browser import install as browser_install
from personal_os.config.loader import set_value
from personal_os.core import pause as pause_mod
from personal_os.core import runtime
from personal_os.core.clock import SystemClock
from personal_os.hooks import installer
from personal_os.interfaces import autostart
from personal_os.interfaces.cli import _config, _state, app, config_app
from personal_os.storage.db import connect

daemon_app = typer.Typer(no_args_is_help=True, help="Start, stop and inspect the capture daemon.")
autostart_app = typer.Typer(no_args_is_help=True, help="Start the daemon when you log in.")
app.add_typer(daemon_app, name="daemon")
app.add_typer(autostart_app, name="autostart")
browser_app = typer.Typer(no_args_is_help=True, help="Connect the browser extension.")
app.add_typer(browser_app, name="browser")

ShellOpt = Annotated[str, typer.Option("--shell", help="powershell, bash or all.")]


def _shells(value: str) -> list[installer.Shell]:
    if value == "all":
        return ["powershell", "bash"]
    if value not in ("powershell", "bash"):
        raise typer.BadParameter("use powershell, bash or all")
    return [value]  # type: ignore[list-item]


def _ago(iso: str | None) -> str:
    if not iso:
        return "never"
    seconds = (datetime.now().astimezone() - datetime.fromisoformat(iso)).total_seconds()
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


# --- daemon --------------------------------------------------------------------------------


@daemon_app.command("run")
def daemon_run() -> None:
    """Run the daemon in this terminal (Ctrl+C to stop)."""
    from personal_os.daemon import main

    if _state.home:
        os.environ["PERSONAL_OS_HOME"] = str(_state.home)
    raise typer.Exit(main())


@daemon_app.command("start")
def daemon_start() -> None:
    """Start the daemon in the background (restarted automatically if it crashes)."""
    cfg = _config()
    info = runtime.daemon_info(cfg.runtime_dir)
    if info.running:
        typer.echo(f"Already running (pid {info.pid}).")
        return
    runtime.clear_stop(cfg.runtime_dir)
    env = os.environ.copy()
    if _state.home:
        env["PERSONAL_OS_HOME"] = str(_state.home)
    flags = 0
    if sys.platform == "win32":
        flags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    subprocess.Popen(
        [str(autostart.pythonw()), "-m", "personal_os.launcher"],
        env=env,
        creationflags=flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        time.sleep(0.25)
        info = runtime.daemon_info(cfg.runtime_dir)
        if info.running:
            typer.echo(f"Started (pid {info.pid}). Check it with `personal-os doctor`.")
            return
    typer.echo(
        "Started the launcher, but the daemon hasn't reported in yet. Run `personal-os doctor`."
    )


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the background daemon."""
    cfg = _config()
    info = runtime.daemon_info(cfg.runtime_dir)
    if not info.running:
        typer.echo("Not running.")
        return
    runtime.request_stop(cfg.runtime_dir)
    for _ in range(60):
        time.sleep(0.25)
        if not psutil.pid_exists(info.pid or -1):
            typer.echo("Stopped.")
            return
    typer.echo(
        "The daemon didn't stop within 15 s; it may be busy. Try again or end it in Task Manager."
    )
    raise typer.Exit(1)


# --- doctor --------------------------------------------------------------------------------


def _ollama() -> str:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=1.0) as resp:
            names = [m.get("name") for m in json.load(resp).get("models", [])]
        return "reachable (" + ", ".join(n for n in names if n) + ")"
    except Exception:
        return "not reachable (not needed until phase 4)"


@app.command()
def doctor() -> None:
    """Health of the daemon, each observer, hooks and storage."""
    cfg = _config()
    problems: list[str] = []
    info = runtime.daemon_info(cfg.runtime_dir)
    h: dict[str, Any] = info.health or {}
    typer.echo("Personal OS doctor\n")
    if info.running:
        cpu = h.get("cpu_percent")
        rss = (h.get("rss_bytes") or 0) / 1e6
        started = h.get("started_at")
        since = datetime.fromisoformat(started).astimezone().strftime("%H:%M") if started else "?"
        typer.echo(
            f"daemon     running, pid {info.pid}, up since {since}, "
            f"heartbeat {_ago(h.get('heartbeat'))}"
        )
        typer.echo(
            f"           CPU {cpu:.2f}% of machine, memory {rss:.0f} MB"
            if cpu is not None
            else "           CPU/memory unavailable"
        )
        hb = h.get("heartbeat")
        if hb and (datetime.now().astimezone() - datetime.fromisoformat(hb)).total_seconds() > 60:
            problems.append("daemon heartbeat is stale")
    else:
        typer.echo("daemon     NOT RUNNING. Start it with `personal-os daemon start`.")
        problems.append("daemon not running")

    conn = connect(cfg.db_path)
    paused = pause_mod.is_paused(conn, SystemClock())
    typer.echo(f"capture    {'PAUSED' if paused else 'active'}")
    db_mb = cfg.db_path.stat().st_size / 1e6 if cfg.db_path.exists() else 0
    typer.echo(f"database   {cfg.db_path} ({db_mb:.1f} MB)")

    typer.echo("\nobservers")
    for name, o in (h.get("observers") or {}).items():
        state = o.get("state", "?")
        line = f"  {name:<9} {state:<9}"
        if state in ("running", "paused", "degraded", "crashed"):
            line += (
                f" last event {_ago(o.get('last_event_at')):<9} {o.get('events_last_hour', 0):>5}/h"
            )
            line += f"  restarts {o.get('restarts', 0)}"
        detail = o.get("detail") or {}
        if detail.get("repos") is not None:
            line += f"  repos {detail['repos']}"
            if detail.get("unreadable"):
                line += f" ({len(detail['unreadable'])} unreadable)"
        if o.get("last_error"):
            line += f"\n            {o['last_error']}"
        typer.echo(line)
        if state in ("degraded", "crashed"):
            problems.append(f"observer {name} is {state}")
    if not h.get("observers"):
        typer.echo("  (no data: the daemon hasn't written a health snapshot)")

    bus = h.get("bus") or {}
    flt = h.get("filter") or {}
    typer.echo(
        f"\nbus        queued {bus.get('queued', 0)}, high-water {bus.get('high_water', 0)}, "
        f"dropped {bus.get('dropped') or 'none'}"
    )
    typer.echo(
        f"filter     blocked {flt.get('blocked') or 'none'}, errors {flt.get('errors') or 'none'}"
    )
    if flt.get("errors"):
        problems.append("privacy filter errors (events were dropped, never stored)")

    typer.echo("\nhooks")
    policy = installer.execution_policy()
    for shell in ("powershell", "bash"):
        st = installer.status(shell, hooks_dir=cfg.data_dir / "hooks")
        extra = f"; execution policy {policy}" if shell == "powershell" and policy else ""
        state = "installed" if st.installed else "not installed"
        typer.echo(f"  {shell:<11} {state} ({st.profile}){extra}")
        for note in st.notes:
            typer.echo(f"              {note}")
        if (
            shell == "powershell"
            and st.installed
            and policy in ("Restricted", "AllSigned", "Undefined")
        ):
            problems.append("PowerShell profiles are blocked by the execution policy")
    pending = (
        len(list((cfg.spool_dir / "terminal").glob("*.jsonl")))
        if (cfg.spool_dir / "terminal").exists()
        else 0
    )
    typer.echo(f"spool      {pending} file(s) waiting")
    for b in browser_install.BROWSERS:
        host = browser_install.status(b)
        reg = "registered" if host.registered else "not registered"
        typer.echo(f"browser    {b:<7} native host {reg}")
    typer.echo(f"autostart  {'enabled' if autostart.is_enabled() else 'disabled'}")
    typer.echo(f"ollama     {_ollama()}")

    if problems:
        typer.echo("\nproblems:\n  - " + "\n  - ".join(problems))
        raise typer.Exit(1)
    typer.echo("\nAll good.")


# --- hooks ---------------------------------------------------------------------------------


@app.command("install-hooks")
def install_hooks(
    shell: ShellOpt = "powershell",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would change.")] = False,
) -> None:
    """Add the terminal hook to your shell profile (a marked, removable block)."""
    cfg = _config()
    for sh in _shells(shell):
        st = installer.install(
            sh,
            hooks_dir=cfg.data_dir / "hooks",
            spool_dir=cfg.spool_dir / "terminal",
            pause_marker=cfg.runtime_dir / pause_mod.MARKER_NAME,
            dry_run=dry_run,
        )
        if not dry_run:
            typer.echo(
                f"{sh}: hook installed in {st.profile}. Open a new terminal to start recording."
            )
        for note in st.notes:
            typer.echo(f"{sh}: {note}")


@app.command("uninstall-hooks")
def uninstall_hooks(shell: ShellOpt = "all") -> None:
    """Remove the terminal hook block from your shell profile(s)."""
    cfg = _config()
    for sh in _shells(shell):
        st = installer.uninstall(sh, hooks_dir=cfg.data_dir / "hooks")
        typer.echo(f"{sh}: removed from {st.profile} (if it was there).")
        typer.echo("Terminals that are already open keep the hook until you close them.")


# --- browser -------------------------------------------------------------------------------

BrowserOpt = Annotated[str, typer.Option("--browser", help="chrome, brave, edge or all.")]


def _browsers(value: str) -> list[browser_install.Browser]:
    if value == "all":
        return list(browser_install.BROWSERS)
    if value not in browser_install.BROWSERS:
        raise typer.BadParameter("use chrome, brave, edge or all")
    return [value]


def extension_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "extensions" / "browser" / "dist"


@browser_app.command("install")
def browser_install_cmd(browser: BrowserOpt = "all") -> None:
    """Register the native host for your browser(s) and turn browser capture on."""
    cfg = _config()
    for b in _browsers(browser):
        manifest = browser_install.register(cfg.data_dir, b, home=_state.home)
        typer.echo(f"{b}: native host registered ({manifest})")
    set_value("observers.browser.enabled", True, _state.home)
    typer.echo("Browser capture is on. Restart the daemon: `personal-os daemon stop` then `start`.")
    typer.echo("")
    typer.echo("Now load the extension: open chrome://extensions (or brave://extensions), turn on")
    typer.echo("Developer mode, click 'Load unpacked' and pick:")
    typer.echo(f"  {extension_dir()}")
    typer.echo(f"Its ID must be {browser_install.EXTENSION_ID}.")


@browser_app.command("uninstall")
def browser_uninstall_cmd(browser: BrowserOpt = "all") -> None:
    """Unregister the native host and turn browser capture off."""
    cfg = _config()
    for b in _browsers(browser):
        browser_install.unregister(cfg.data_dir, b)
        typer.echo(f"{b}: native host removed")
    set_value("observers.browser.enabled", False, _state.home)
    typer.echo("Browser capture is off. Remove the extension from the browser's extensions page.")


@browser_app.command("status")
def browser_status_cmd() -> None:
    """Native host registration per browser, and when the extension last sent something."""
    cfg = _config()
    for b in browser_install.BROWSERS:
        st = browser_install.status(b)
        typer.echo(f"{b:<7} {'registered' if st.registered else 'not registered'}")
    typer.echo(f"extension id   {browser_install.EXTENSION_ID}")
    typer.echo(f"extension dir  {extension_dir()}")
    h = runtime.daemon_info(cfg.runtime_dir).health or {}
    o = (h.get("observers") or {}).get("browser") or {}
    typer.echo(
        f"observer       {o.get('state', 'unknown')}, last event {_ago(o.get('last_event_at'))}"
    )


# --- autostart -----------------------------------------------------------------------------


@autostart_app.command("enable")
def autostart_enable() -> None:
    """Start the daemon at logon (adds an entry to your Startup folder)."""
    path = autostart.enable(_state.home)
    typer.echo(f"Added {path}. The daemon will start when you log in.")


@autostart_app.command("disable")
def autostart_disable() -> None:
    """Stop starting the daemon at logon."""
    typer.echo("Removed." if autostart.disable() else "It wasn't enabled.")


@autostart_app.command("status")
def autostart_status() -> None:
    typer.echo(f"{'enabled' if autostart.is_enabled() else 'disabled'} ({autostart.entry_path()})")


# --- config set ----------------------------------------------------------------------------


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help="Dotted key, e.g. observers.fs.enabled")],
    value: Annotated[
        str, typer.Argument(help='JSON value (true, 600, "text", ["a"]) or plain text')
    ],
) -> None:
    """Change one setting in config.toml (validated; comments kept). Restart the daemon to apply."""
    try:
        parsed: Any = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    try:
        set_value(key, parsed, _state.home)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from None
    except ValueError as exc:
        raise typer.BadParameter(f"invalid value: {exc}") from None
    typer.echo(
        f"{key} = {json.dumps(parsed)}. Restart the daemon to apply: "
        "`personal-os daemon stop` then `personal-os daemon start`."
    )
