"""Talking to the browser extension (ADR-076): write a command, wait for its result.

The native host (running inside the browser while the extension is connected) claims command
files from runtime/browser/cmd/ and writes results to runtime/browser/res/. Each connected
browser leaves a marker in runtime/browser/hosts/ for as long as its host runs. Files, no port:
a web page can't reach any of this.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import psutil

from personal_os.core import runtime

TIMEOUT_S = 15.0


class BrowserUnavailable(RuntimeError):
    pass


class BrowserError(RuntimeError):
    pass


def _dir(runtime_dir: Path, name: str) -> Path:
    return runtime_dir / "browser" / name


def connected(runtime_dir: Path) -> list[str]:
    """Browsers whose extension is connected right now (stale markers are removed)."""
    out: list[str] = []
    d = _dir(runtime_dir, "hosts")
    if not d.is_dir():
        return out
    for f in d.glob("*.json"):
        data = runtime.read_json(f) or {}
        pid = int(data.get("pid") or 0)
        if pid and psutil.pid_exists(pid):
            out.append(str(data.get("browser", "")))
        else:
            f.unlink(missing_ok=True)
    return sorted(set(out))


def pick(runtime_dir: Path, preferred: str) -> str:
    browsers = connected(runtime_dir)
    if not browsers:
        raise BrowserUnavailable(
            "the browser isn't connected: open Brave (or Chrome) with the Personal OS extension"
        )
    return preferred if preferred in browsers else browsers[0]


def send(
    runtime_dir: Path,
    op: str,
    args: dict[str, Any] | None = None,
    *,
    browser: str = "brave",
    timeout_s: float = TIMEOUT_S,
) -> Any:
    """Run one command in the browser; its data, or BrowserError with the extension's reason."""
    target = pick(runtime_dir, browser)
    cid = uuid.uuid4().hex
    cmd_dir, res_dir = _dir(runtime_dir, "cmd"), _dir(runtime_dir, "res")
    cmd_dir.mkdir(parents=True, exist_ok=True)
    runtime.write_json_atomic(
        cmd_dir / f"{cid}.json",
        {"id": cid, "op": op, "args": args or {}, "browser": target, "at": time.time()},
    )
    res = res_dir / f"{cid}.json"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if res.exists():
            try:
                data = json.loads(res.read_text("utf-8"))
            except (OSError, ValueError):
                time.sleep(0.05)
                continue
            res.unlink(missing_ok=True)
            if not data.get("ok"):
                raise BrowserError(str(data.get("error") or "the browser couldn't do that"))
            return data.get("data")
        time.sleep(0.1)
    (cmd_dir / f"{cid}.json").unlink(missing_ok=True)  # never run it late
    raise BrowserError("the browser didn't answer in time")
