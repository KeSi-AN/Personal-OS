"""Helpers shared by tools: hashing, running a process that the kill switch can stop."""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
from pathlib import Path

from personal_os.actions.base import ActionError

OUTPUT_MAX = 4000


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_process(
    argv: list[str], cwd: Path | None, timeout_s: float, cancel: threading.Event
) -> tuple[int, str]:
    """Run without a shell; stop it on timeout or when `cancel` is set. Returns (code, output)."""
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        raise ActionError(f"couldn't start {argv[0]}: {exc}") from None
    chunks: list[bytes] = []
    reader = threading.Thread(target=lambda: chunks.extend(iter(proc.stdout.read1, b"")))  # type: ignore[union-attr]
    reader.daemon = True
    reader.start()
    deadline = time.monotonic() + timeout_s
    while proc.poll() is None:
        if cancel.is_set() or time.monotonic() > deadline:
            proc.kill()
            proc.wait(5)
            reader.join(2)
            if cancel.is_set():
                from personal_os.actions.executor import Cancelled

                raise Cancelled
            raise ActionError(f"stopped after {timeout_s:.0f} s (timeout)")
        time.sleep(0.05)
    reader.join(5)
    out = b"".join(chunks).decode("utf-8", errors="replace")
    if len(out) > OUTPUT_MAX:
        out = out[: OUTPUT_MAX // 2] + "\n…\n" + out[-OUTPUT_MAX // 2 :]
    return int(proc.returncode), out
