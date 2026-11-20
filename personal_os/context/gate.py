"""When background LLM jobs may run (ADR-014): on AC power, user idle, nothing full-screen."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import psutil

from personal_os.config.schema import LLMConfig


@dataclass(frozen=True)
class GateState:
    ok: bool
    reason: str  # why not, or "ok"


def on_ac_power() -> bool:
    battery = psutil.sensors_battery()
    return battery is None or bool(battery.power_plugged)  # no battery = a desktop


class JobGate:
    def __init__(
        self,
        cfg: LLMConfig,
        idle_seconds: Callable[[], float] | None,
        fullscreen: Callable[[], bool] | None,
        paused: Callable[[], bool],
        ac_power: Callable[[], bool] = on_ac_power,
        mode_allows: Callable[[], bool] = lambda: True,
        voice_busy: Callable[[], bool] = lambda: False,
    ) -> None:
        self.cfg = cfg
        self.mode_allows = mode_allows
        self.voice_busy = voice_busy
        self.idle_seconds = idle_seconds
        self.fullscreen = fullscreen
        self.paused = paused
        self.ac_power = ac_power

    def check(self) -> GateState:
        if not self.cfg.enabled:
            return GateState(False, "llm disabled in config")
        if self.paused():
            return GateState(False, "capture paused")
        if not self.mode_allows():
            return GateState(False, "gaming mode: the GPU is the game's")
        if self.cfg.only_on_ac_power and not self.ac_power():
            return GateState(False, "on battery")
        if self.voice_busy():
            # A spoken conversation leaves the keyboard idle, so "idle" alone let a 16 s
            # summary run between two of Donna's steps, and the next one waited for it.
            return GateState(False, "Donna is in a conversation")
        if self.idle_seconds is None:
            return GateState(False, "idle time unknown on this platform")
        if self.idle_seconds() < self.cfg.min_user_idle_s:
            return GateState(False, "you're active")
        if self.cfg.skip_when_fullscreen and self.fullscreen is not None and self.fullscreen():
            return GateState(False, "full-screen app in front")
        return GateState(True, "ok")


VOICE_QUIET_S = 90.0  # after a turn ends, a follow-up is likely: no background model work


def voice_busy(runtime_dir: Path, now: datetime | None = None) -> bool:
    """Is Donna in a conversation: listening, thinking or speaking, or her last turn ended
    less than VOICE_QUIET_S ago (voice.json)? False when voice isn't running."""
    from personal_os.voice.main import ACTIVE, status

    st = status(runtime_dir)
    if not st.get("running"):
        return False
    if st.get("state") in ACTIVE:
        return True
    done = (st.get("turn") or {}).get("done_at")
    if not done:
        return False
    try:
        ended = datetime.fromisoformat(str(done))
    except ValueError:
        return False
    now = now or datetime.now().astimezone()
    return (now - ended).total_seconds() < VOICE_QUIET_S
