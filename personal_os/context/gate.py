"""When background LLM jobs may run (ADR-014): on AC power, user idle, nothing full-screen."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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
    ) -> None:
        self.cfg = cfg
        self.mode_allows = mode_allows
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
        if self.idle_seconds is None:
            return GateState(False, "idle time unknown on this platform")
        if self.idle_seconds() < self.cfg.min_user_idle_s:
            return GateState(False, "you're active")
        if self.cfg.skip_when_fullscreen and self.fullscreen is not None and self.fullscreen():
            return GateState(False, "full-screen app in front")
        return GateState(True, "ok")
