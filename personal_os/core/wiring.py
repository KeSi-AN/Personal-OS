"""Builds the capture pipeline from config. Shared by the daemon, CLI, simulator and tests."""

from __future__ import annotations

import sqlite3

from personal_os.config.schema import Config
from personal_os.core.bus import EventBus
from personal_os.core.clock import Clock
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
) -> Pipeline:
    rules = PrivacyRules.from_config(config.privacy, protected_dirs=[config.data_dir])
    gate = PauseGate(conn, clock) if honour_pause else (lambda: False)
    privacy = PrivacyFilter(rules, is_paused=gate)
    writer = EventWriter(conn, config.retention, clock)
    return Pipeline(privacy, writer, clock, bus=bus)
