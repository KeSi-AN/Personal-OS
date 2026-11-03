"""Terminal observer: ingests the spool files written by the shell hooks (DESIGN.md §4.5, ADR-004).

Each shell appends one JSON line per command to spool/terminal/<shell>-<pid>.jsonl. To read
without racing a shell that is appending, a file is first renamed (atomic; the shell's next
append simply creates a new file), then read in full and deleted.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from personal_os.core.events import Event, TerminalCommand
from personal_os.core.logging import get_logger
from personal_os.core.observer import BaseObserver, ObserverContext
from personal_os.observers.gitutil import find_repo_root, read_head_branch
from personal_os.observers.spool import Skip, SpoolStats, drain

VERSION = "1.0.0"
MAX_COMMAND_CHARS = 4096
_SHELLS = {"powershell", "bash", "zsh", "fish"}
_log = get_logger("observers.terminal")


_MSYS_RE = re.compile(r"^/([a-zA-Z])(/.*)?$")


def msys_to_windows(path: str) -> str:
    """Git Bash reports /c/Users/x; everything else in the system uses C:/Users/x."""
    m = _MSYS_RE.match(path)
    if m and sys.platform == "win32":
        return f"{m.group(1).upper()}:{m.group(2) or '/'}"
    return path


# Commands an editor injects into its integrated terminal at startup (not typed by you).
_BOOTSTRAP_RE = re.compile(
    r"shellIntegration\.(ps1|bash|zsh|fish)|VSCODE_SHELL_INTEGRATION|cursor-shell-integration",
    re.IGNORECASE,
)


def is_tool_bootstrap(command: str) -> bool:
    return bool(_BOOTSTRAP_RE.search(command))


def _epoch(value: Any) -> float:
    return float(str(value).replace(",", "."))


def parse_record(rec: dict[str, Any]) -> Event | None:
    """One hook record → a terminal.command interval. None for private or unusable records."""
    command = rec.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    if command.startswith(" "):
        return None  # private-command convention; the hooks already skip these
    if is_tool_bootstrap(command):
        return None
    try:
        if "start_epoch" in rec:  # bash: $EPOCHREALTIME (decimal comma in some locales)
            start = datetime.fromtimestamp(_epoch(rec["start_epoch"]), UTC)
            end = datetime.fromtimestamp(_epoch(rec.get("end_epoch", rec["start_epoch"])), UTC)
        else:
            start = datetime.fromisoformat(str(rec["start"]))
            end = datetime.fromisoformat(str(rec.get("end") or rec["start"]))
    except (KeyError, ValueError, TypeError, OverflowError):
        return None
    if start.tzinfo is None or end.tzinfo is None:
        return None
    if end < start:
        end = start
    local_start, local_end = start.astimezone(), end.astimezone()
    cwd = msys_to_windows(rec["cwd"]) if isinstance(rec.get("cwd"), str) else None
    repo = find_repo_root(cwd) if cwd and os.path.isdir(cwd) else None
    shell = str(rec.get("shell", "other"))
    exit_code = rec.get("exit_code")
    success = rec.get("success")
    payload = TerminalCommand(
        command=command.rstrip("\r\n")[:MAX_COMMAND_CHARS],
        cwd=cwd,
        shell=shell if shell in _SHELLS else "other",
        shell_session=str(rec.get("session", ""))[:64],
        host=str(rec["host"])[:64] if rec.get("host") else None,
        exit_code=exit_code if isinstance(exit_code, int) else None,
        success=success if isinstance(success, bool) else None,
        duration_ms=int((end - start) / timedelta(milliseconds=1)),
        history_id=rec.get("history_id") if isinstance(rec.get("history_id"), int) else None,
        repo_root=repo,
        branch=read_head_branch(repo) if repo else None,
    )
    return Event.interval(
        payload, local_start, local_end, observer_version=VERSION, project_hint=repo
    )


def _parse_spooled(rec: dict[str, Any]) -> Event | None:
    if isinstance(rec.get("command"), str) and rec["command"].startswith(" "):
        raise Skip  # private command (the hooks never write these; defence in depth)
    return parse_record(rec)


def ingest_spool_dir(spool: Path, stats: SpoolStats) -> list[Event]:
    """Claim, read and delete every finished spool file. Safe to call while shells append."""
    return drain(spool, _parse_spooled, stats)


class TerminalObserver(BaseObserver):
    name = "terminal"
    version = VERSION

    def __init__(self, spool_dir: Path, *, poll_s: float = 1.0) -> None:
        super().__init__()
        self.spool_dir = spool_dir
        self.poll_s = poll_s
        self.stats = SpoolStats()

    async def run(self, ctx: ObserverContext) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        while True:
            if not ctx.paused.is_set():
                for event in await asyncio.to_thread(ingest_spool_dir, self.spool_dir, self.stats):
                    self.emit(event)
            await ctx.clock.sleep(self.poll_s)
