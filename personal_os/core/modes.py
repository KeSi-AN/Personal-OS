"""Modes (phase 10): work, gaming and chill, each with real effects.

    work    Donna nudges you back if chill takes over (15+ of the last 20 minutes); reminders,
            briefings and background summaries as usual.
    gaming  The GPU is the game's: the local model is unloaded and background jobs pause. No wake
            word (game audio would set it off), no popups over the game, nothing spoken unless
            you ask; reminders go quietly to the notification centre. Push-to-talk still works.
            Entered by itself when a game is in front, left 3 minutes after it closes.
    chill   Hands-free: the wake word on, no productivity nudges.

The mode lives in runtime/mode.json (mode, since, by, previous), so voice, the daemon and the
dashboard share it; changes are appended to runtime/mode_log.jsonl for the daily brief.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from personal_os.core import runtime

Mode = Literal["work", "gaming", "chill"]
MODES: tuple[Mode, ...] = ("work", "gaming", "chill")
ALIASES = {"leisure": "chill", "relax": "chill", "relaxing": "chill", "game": "gaming",
           "games": "gaming", "focus": "work", "working": "work", "study": "work"}  # fmt: skip
FILE = "mode.json"
LOG = "mode_log.jsonl"


@dataclass(frozen=True)
class Effects:
    label: str
    wake_word: bool | None  # None: your setting
    popup: bool
    speak_announcements: bool  # reminders and briefings said aloud
    followup: bool
    background_jobs: bool  # summaries and other model work
    unload_model: bool
    nudge_chill_min: int  # 0: never
    describe: str


EFFECTS: dict[Mode, Effects] = {
    "work": Effects(
        "Work", None, True, True, True, True, False, 15,
        "Focus: a nudge if chill takes over (15 of the last 20 minutes), everything else as usual.",
    ),
    "gaming": Effects(
        "Gaming", False, False, False, False, False, True, 0,
        "Quiet and light: the local model unloaded and background jobs paused so the GPU is the "
        "game's; no wake word, popups or spoken announcements. Push-to-talk still works.",
    ),
    "chill": Effects(
        "Chill", True, True, True, True, True, False, 0,
        "Hands-free: the wake word on, no productivity nudges.",
    ),
}  # fmt: skip


def normalize(name: str) -> Mode | None:
    n = name.strip().lower().removesuffix(" mode")
    n = ALIASES.get(n, n)
    return n if n in MODES else None


def state(runtime_dir: Path) -> dict[str, Any]:
    data = runtime.read_json(runtime_dir / FILE) or {}
    if data.get("mode") not in MODES:
        return {"mode": "work", "by": "default", "since": "", "previous": ""}
    return data


def current(runtime_dir: Path) -> Mode:
    return state(runtime_dir)["mode"]  # type: ignore[no-any-return]


def effects(runtime_dir: Path) -> Effects:
    return EFFECTS[current(runtime_dir)]


def set_mode(runtime_dir: Path, mode: Mode, *, by: str, now: datetime | None = None) -> bool:
    """Switch; returns False if it was already that mode. `by`: voice, dashboard, chat, auto."""
    old = state(runtime_dir)
    if old["mode"] == mode:
        if by != "auto" and old.get("by") == "auto":  # you confirmed it: auto won't undo it now
            runtime.write_json_atomic(runtime_dir / FILE, {**old, "by": by})
        return False
    at = (now or datetime.now(UTC)).isoformat()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime.write_json_atomic(
        runtime_dir / FILE, {"mode": mode, "by": by, "since": at, "previous": old["mode"]}
    )
    with (runtime_dir / LOG).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": at, "mode": mode, "by": by}) + "\n")
    return True


def history(runtime_dir: Path, since: datetime) -> list[dict[str, Any]]:
    path = runtime_dir / LOG
    if not path.exists():
        return []
    out = []
    for line in path.read_text("utf-8").splitlines()[-500:]:
        try:
            rec = json.loads(line)
            if datetime.fromisoformat(rec["at"]) >= since:
                out.append(rec)
        except (ValueError, KeyError):
            continue
    return out


# --- automatic gaming --------------------------------------------------------------------------


def auto_gaming_step(
    runtime_dir: Path, game_in_front: bool, now: datetime, *, quiet_s: float = 180
) -> str:
    """Called every ~30 s with whether a game is the foreground app. Enters gaming when one is,
    and goes back to the previous mode once no game has been in front for `quiet_s` (only if
    gaming was entered automatically: a mode you chose is never undone). Returns what changed:
    "entered", "left" or ""."""
    s = state(runtime_dir)
    marker = runtime_dir / "mode_game_seen.json"
    if game_in_front:
        runtime.write_json_atomic(marker, {"at": now.isoformat()})
        if s["mode"] != "gaming":
            set_mode(runtime_dir, "gaming", by="auto", now=now)
            return "entered"
        return ""
    if s["mode"] == "gaming" and s.get("by") == "auto":
        seen = (runtime.read_json(marker) or {}).get("at")
        last = datetime.fromisoformat(seen) if seen else now
        if (now - last).total_seconds() >= quiet_s:
            back = normalize(str(s.get("previous") or "work")) or "work"
            set_mode(runtime_dir, back, by="auto", now=now)
            return "left"
    return ""
