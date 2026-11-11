from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from personal_os.config.schema import Config
    from personal_os.core.clock import SimClock
    from personal_os.privacy.filter import PrivacyFilter

# Fixtures import lazily so each layer's tests run without the layers above it.

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=IST)
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PERSONAL_OS_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def no_real_app_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never ask Windows which apps are installed; a test sets its own list."""
    from personal_os.actions import apps

    monkeypatch.setattr(apps, "installed", lambda *_a, **_k: [])


@pytest.fixture(autouse=True)
def mic_not_muted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never ask Windows whether this machine's microphone is muted."""
    from personal_os.voice import main as voice_main

    monkeypatch.setattr(voice_main, "mic_muted", lambda: False)


@pytest.fixture(autouse=True)
def no_real_ducking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never turn down the volume of apps playing on this machine."""
    from personal_os.voice import duck

    monkeypatch.setattr(duck, "_sessions", lambda: [])


@pytest.fixture(autouse=True)
def no_real_popup(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Tests never open the voice popup window; launches are recorded here instead."""
    from personal_os.interfaces import popup

    launched: list[bool] = []
    monkeypatch.setattr(popup, "_spawn", launched.append)
    return launched


@pytest.fixture(autouse=True)
def no_real_app(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Tests never open the desktop app or a browser; what would open is recorded here."""
    from personal_os.interfaces import app, tray

    opened: list[str] = []
    monkeypatch.setattr(app, "_spawn", lambda path: opened.append(f"app {path}"))
    monkeypatch.setattr(tray, "open_url", lambda url: opened.append(f"browser {url}"))
    return opened


@pytest.fixture
def config(home: Path) -> Config:
    from personal_os.config.loader import load_config

    return load_config(home)


@pytest.fixture
def clock() -> SimClock:
    from personal_os.core.clock import SimClock

    return SimClock(T0)


@pytest.fixture
def conn(home: Path) -> Iterator[sqlite3.Connection]:
    from personal_os.storage.db import open_db

    c = open_db(home / "test.db")
    yield c
    c.close()


@pytest.fixture
def privacy(config: Config) -> PrivacyFilter:
    from personal_os.privacy.filter import PrivacyFilter
    from personal_os.privacy.rules import PrivacyRules

    return PrivacyFilter(PrivacyRules.from_config(config.privacy, protected_dirs=[config.data_dir]))


def utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC)
