"""Alerts that must be seen: reminders (and a pointer to a new briefing), shown in the popup.

Windows hides notification banners while a full-screen app runs, during Do Not Disturb, and in
quiet hours: the toast goes silently to the notification centre. So a due reminder is shown in
Personal OS's own always-on-top popup, which stays until you dismiss it, and the Windows toast
is only the fallback (no WebView2, or the popup failed).

Each alert is one small file in runtime/alerts/: the daemon writes it, the popup's Dismiss
deletes it, so the two never overwrite each other. Alerts older than 12 hours are dropped.
Only text you wrote yourself goes in (a reminder's words, a briefing's title), never content
captured from your activity.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from personal_os.config.schema import Config
from personal_os.core import runtime

DIR = "alerts"
KEEP = timedelta(hours=12)
_ID = re.compile(r"^[A-Za-z0-9-]{1,80}$")


def _dir(runtime_dir: Path) -> Path:
    return runtime_dir / DIR


def add(runtime_dir: Path, aid: str, title: str, text: str, at: datetime) -> None:
    if not _ID.match(aid):
        raise ValueError("bad alert id")
    d = _dir(runtime_dir)
    d.mkdir(parents=True, exist_ok=True)
    runtime.write_json_atomic(
        d / f"{aid}.json",
        {"id": aid, "title": title[:80], "text": text[:400], "at": at.isoformat()},
    )


def pending(runtime_dir: Path, now: datetime) -> list[dict[str, Any]]:
    """Alerts not yet dismissed, oldest first. Expired ones are removed."""
    d = _dir(runtime_dir)
    if not d.is_dir():
        return []
    out = []
    for f in d.glob("*.json"):
        data = runtime.read_json(f)
        if data is None:
            continue
        try:
            at = datetime.fromisoformat(str(data.get("at")))
        except ValueError:
            at = now
        if now - at > KEEP:
            f.unlink(missing_ok=True)
            continue
        out.append(
            {
                "id": str(data.get("id", f.stem)),
                "title": str(data.get("title", "")),
                "text": str(data.get("text", "")),
                "at": at.isoformat(),
            }
        )
    return sorted(out, key=lambda a: a["at"])


def dismiss(runtime_dir: Path, aid: str) -> bool:
    if not _ID.match(aid or ""):
        return False  # never a path: only our own ids
    f = _dir(runtime_dir) / f"{aid}.json"
    existed = f.exists()
    f.unlink(missing_ok=True)
    return existed


def show(cfg: Config, aid: str, title: str, text: str, at: datetime) -> bool:
    """Put an alert in the popup and open it. False if the popup can't open (use a toast)."""
    from personal_os.interfaces import popup

    try:
        add(cfg.runtime_dir, aid, title, text, at)
        result = popup.launch(cfg)
    except Exception as exc:
        popup.record_error(cfg.runtime_dir, f"{type(exc).__name__}: {exc}")
        return False
    if result in ("opened", "already open"):
        return True
    dismiss(cfg.runtime_dir, aid)  # it will be shown as a toast instead
    return False
