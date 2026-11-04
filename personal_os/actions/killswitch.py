"""The kill switch: a file in the runtime folder, so every process sees it at once.

Set by `personal-os actions kill` or the daemon's Ctrl+Alt+Shift+K. While it exists the
assistant is observe-only: nothing new starts, and running actions are cancelled.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

KILL_FILE = "actions.killed"
UNLOCKS_FILE = "actions.unlocks.json"


def kill_path(runtime_dir: Path) -> Path:
    return runtime_dir / KILL_FILE


def engage(runtime_dir: Path, now: datetime, reason: str) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    kill_path(runtime_dir).write_text(
        json.dumps({"at": now.isoformat(), "reason": reason}), encoding="utf-8"
    )


def release(runtime_dir: Path) -> bool:
    path = kill_path(runtime_dir)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed


def engaged(runtime_dir: Path) -> bool:
    return kill_path(runtime_dir).exists()


# --- time-limited unlocks of tier 3 tools --------------------------------------------------------


def unlocks(runtime_dir: Path, now: datetime) -> dict[str, datetime]:
    try:
        raw = json.loads((runtime_dir / UNLOCKS_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for tool, until in raw.items():
        try:
            t = datetime.fromisoformat(until)
        except (TypeError, ValueError):
            continue
        if t > now:
            out[tool] = t
    return out


def unlock(runtime_dir: Path, tool: str, until: datetime, now: datetime) -> None:
    current = {k: v.isoformat() for k, v in unlocks(runtime_dir, now).items()}
    current[tool] = until.isoformat()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / UNLOCKS_FILE).write_text(json.dumps(current), encoding="utf-8")


def relock_all(runtime_dir: Path) -> None:
    (runtime_dir / UNLOCKS_FILE).unlink(missing_ok=True)
