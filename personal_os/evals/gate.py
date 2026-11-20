"""The bench as a check before pushing: has the code that decides what Donna understands and
does changed since the last bench run, and did that run pass? (docs/BENCH.md)

    uv run python -m personal_os.evals.gate      # exit 0: up to date and passing

The bench needs Ollama and a few minutes, so the gate doesn't run it: each bench run records a
fingerprint of the files below, and the gate compares it with the files as they are now. The
git pre-push hook (scripts/install_git_hooks.py) runs it.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# What decides how a request is understood and answered.
WATCHED = (
    "personal_os/actions/agent.py",
    "personal_os/actions/planner.py",
    "personal_os/actions/music.py",
    "personal_os/actions/reminder_parse.py",
    "personal_os/actions/tools/*.py",
    "personal_os/context/route.py",
    "personal_os/context/ask.py",
    "personal_os/context/question.py",
    "personal_os/context/evidence.py",
    "personal_os/context/commitments.py",
    "personal_os/voice/main.py",
    "personal_os/voice/router.py",
    "personal_os/voice/rest.py",
    "personal_os/voice/dialogue.py",
    "personal_os/projects/resolver.py",
    "personal_os/projects/sessions.py",
    "personal_os/llm/*.py",
    "personal_os/evals/cases.toml",
)
META = "latest-meta.json"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def fingerprint(root: Path | None = None) -> str:
    """A hash of the watched files' contents (line endings ignored)."""
    root = root or repo_root()
    h = hashlib.sha256()
    for pattern in WATCHED:
        for f in sorted(root.glob(pattern)):
            h.update(f.relative_to(root).as_posix().encode())
            h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


def record(out_dir: Path, *, passed: int, total: int, flags: list[str]) -> None:
    """Called by the bench after a full run (not one limited with --only)."""
    meta = {
        "fingerprint": fingerprint(),
        "passed": passed,
        "total": total,
        "flags": flags,
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    (out_dir / META).write_text(json.dumps(meta, indent=1), "utf-8")


def check(data_dir: Path, root: Path | None = None) -> tuple[bool, str]:
    try:
        meta: dict[str, Any] = json.loads((data_dir / "bench" / META).read_text("utf-8"))
    except (OSError, ValueError):
        return False, "No full bench run recorded yet."
    if meta.get("fingerprint") != fingerprint(root):
        return False, (
            f"Code that decides what Donna does changed since the last full bench run "
            f"({meta.get('at', '?')})."
        )
    if meta.get("passed") != meta.get("total"):
        return False, f"The last bench run passed {meta.get('passed')} of {meta.get('total')}."
    return True, f"Bench up to date: {meta['passed']}/{meta['total']} at {meta.get('at')}."


def main() -> int:
    from personal_os.config.loader import default_home, load_config

    real = load_config(default_home(), create=False)
    ok, message = check(real.data_dir)
    print(message)
    if not ok:
        print("Run: uv run python -m personal_os.evals.bench", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
