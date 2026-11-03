"""Builds the capture pipeline from config. Shared by the daemon, CLI, simulator and tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from personal_os.config.schema import Config
from personal_os.core.bus import EventBus
from personal_os.core.clock import Clock
from personal_os.core.events import Event
from personal_os.core.pause import PauseGate
from personal_os.core.pipeline import Pipeline
from personal_os.privacy.filter import PrivacyFilter
from personal_os.privacy.rules import PrivacyRules
from personal_os.storage.writer import EventWriter


def build_pipeline(
    config: Config,
    conn: sqlite3.Connection,
    clock: Clock,
    *,
    bus: EventBus | None = None,
    honour_pause: bool = True,
    pause_gate: Callable[[Event], bool] | None = None,
) -> Pipeline:
    """`pause_gate` overrides the default (the persisted pause setting, read once a second);
    the daemon passes a time-aware gate so intervals closed at pause time still get stored."""
    rules = PrivacyRules.from_config(config.privacy, protected_dirs=[config.data_dir])
    if pause_gate is None:
        setting = PauseGate(conn, clock) if honour_pause else None
        pause_gate = (lambda _e: setting()) if setting else (lambda _e: False)
    privacy = PrivacyFilter(rules, is_paused=pause_gate)
    writer = EventWriter(conn, config.retention, clock)
    return Pipeline(privacy, writer, clock, bus=bus)
