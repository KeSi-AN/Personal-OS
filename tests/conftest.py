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
