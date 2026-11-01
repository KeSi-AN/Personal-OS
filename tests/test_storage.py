from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from personal_os.core.clock import SimClock
from personal_os.core.events import (
    Event,
    PageExtract,
    PageVisit,
    PrivacyClass,
    TerminalCommand,
    WindowFocus,
)
from personal_os.core.ids import uuid7
from personal_os.storage import maintenance, repo
from personal_os.storage.db import available_migrations, connect, migrate, open_db, schema_version
from personal_os.storage.writer import EventWriter
from tests.conftest import T0


@pytest.fixture
def writer(conn, config, clock) -> EventWriter:
    return EventWriter(conn, config.retention, clock)


def focus_at(id_, end, *, title="A", last_seen=None, cls=PrivacyClass.NORMAL) -> Event:
    return Event.interval(WindowFocus(app="Code", title=title), T0, end, id=id_,
                          last_seen=last_seen, observer_version="t", privacy_class=cls)  # fmt: skip


def test_migrations_apply_once_and_set_version(home: Path):
    c = connect(home / "m.db")
    assert migrate(c, home / "m.db") == [m.version for m in available_migrations()]
    assert schema_version(c) == len(available_migrations())
    assert migrate(c, home / "m.db") == []
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA secure_delete").fetchone()[0] == 1
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not list(home.glob("*.bak"))  # no stray backup copies of captured data


def test_failed_migration_rolls_back(home: Path, monkeypatch):
    from personal_os.storage import db as dbmod

    real = dbmod.available_migrations()
    broken = dbmod.Migration(
        len(real) + 1, "9999_broken.sql", "CREATE TABLE t (x); SELEC nonsense;"
    )
    c = open_db(home / "b.db")
    monkeypatch.setattr(dbmod, "available_migrations", lambda: [*real, broken])
    with pytest.raises(sqlite3.Error):
        migrate(c, home / "b.db")
    assert schema_version(c) == len(real)
    assert c.execute("SELECT name FROM sqlite_master WHERE name = 't'").fetchone() is None
    assert list(home.glob("b.db.pre-*.bak"))  # backup kept because migration failed


def test_interval_upsert_checkpoints_and_close(conn, writer):
    id_ = uuid7(T0)
    writer.write([focus_at(id_, None, last_seen=T0)])
    writer.write([focus_at(id_, None, last_seen=T0 + timedelta(seconds=60))])
    writer.write([focus_at(id_, T0 + timedelta(seconds=90))])
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    e = repo.get_event(conn, str(id_))
    assert e is not None and e.ts_end == T0 + timedelta(seconds=90)
    # a late, stale checkpoint must not reopen or shorten the interval
    writer.write([focus_at(id_, None, last_seen=T0 + timedelta(seconds=30))])
    e = repo.get_event(conn, str(id_))
    assert e is not None and e.ts_end == T0 + timedelta(seconds=90)


def test_sensitive_row_never_regains_free_text(conn, writer):
    id_ = uuid7(T0)
    writer.write([focus_at(id_, None, title="[redacted]", cls=PrivacyClass.SENSITIVE)])
    writer.write([focus_at(id_, T0 + timedelta(seconds=5), title="leaky title")])
    row = conn.execute("SELECT privacy_class, payload, search_text FROM events").fetchone()
    assert row["privacy_class"] == "sensitive"
    assert "leaky" not in row["payload"] and "leaky" not in row["search_text"]


def test_blocked_event_reaching_writer_is_an_error(writer):
    with pytest.raises(ValueError):
        writer.write([focus_at(uuid7(T0), None, cls=PrivacyClass.BLOCKED)])


def test_page_extract_split_into_own_table_and_cascades(conn, writer):
    e = Event.interval(
        PageVisit(url="https://docs.dev/a", title="Docs", extract=PageExtract(main_text="wal mode")),
        T0, T0 + timedelta(minutes=2), observer_version="t",
    )  # fmt: skip
    writer.write([e])
    assert "wal mode" not in conn.execute("SELECT payload FROM events").fetchone()[0]
    assert conn.execute("SELECT main_text FROM page_extracts").fetchone()[0] == "wal mode"
    back = repo.get_event(conn, str(e.id))
    assert back is not None and back.payload.extract.main_text == "wal mode"  # type: ignore[union-attr]
    conn.execute("DELETE FROM events")
    assert conn.execute("SELECT count(*) FROM page_extracts").fetchone()[0] == 0


def test_search_finds_events_and_extracts(conn, writer):
    writer.write(
        [
            Event.interval(TerminalCommand(command="pytest tests/test_session.py"), T0,
                           T0 + timedelta(seconds=3), observer_version="t"),
            Event.interval(PageVisit(url="https://x.dev", title="Something",
                                     extract=PageExtract(main_text="sqlite write ahead log")),
                           T0, T0 + timedelta(minutes=1), observer_version="t"),
        ]
    )  # fmt: skip
    assert [e.kind for e, _ in repo.search(conn, "test_session")] == ["terminal.command"]
    assert [e.kind for e, _ in repo.search(conn, "write-ahead log")] == ["browser.page_visit"]
    assert repo.search(conn, 'AND OR "(') == []  # user input never becomes FTS syntax


def test_retention_purge(conn, writer, config):
    writer.write([Event.point(WindowFocus(app="Code"), T0, observer_version="t")])
    assert (
        maintenance.purge_expired(conn, T0 + timedelta(days=config.retention.raw_days - 1))[
            "events"
        ]
        == 0
    )
    assert (
        maintenance.purge_expired(conn, T0 + timedelta(days=config.retention.raw_days + 1))[
            "events"
        ]
        == 1
    )


def test_sensitive_events_expire_sooner(conn, writer, config):
    writer.write([focus_at(uuid7(T0), T0, cls=PrivacyClass.SENSITIVE)])
    purged = maintenance.purge_expired(
        conn, T0 + timedelta(days=config.retention.sensitive_days + 1)
    )
    assert purged["events"] == 1


def test_close_open_intervals_uses_last_checkpoint(conn, writer):
    id_ = uuid7(T0)
    writer.write([focus_at(id_, None, last_seen=T0 + timedelta(seconds=120))])
    assert maintenance.close_open_intervals(conn) == 1
    e = repo.get_event(conn, str(id_))
    assert e is not None and e.ts_end == T0 + timedelta(seconds=120) and e.truncated


def test_counters_and_settings(conn, clock: SimClock):
    repo.add_counters(conn, "2026-11-02", "filter.blocked", {"blocked_app": 2})
    repo.add_counters(conn, "2026-11-02", "filter.blocked", {"blocked_app": 3, "zero": 0})
    assert repo.get_counters(conn) == {"filter.blocked": {"blocked_app": 5}}
    repo.set_setting(conn, "k", {"a": 1}, clock)
    assert repo.get_setting(conn, "k") == {"a": 1}
    repo.delete_setting(conn, "k")
    assert repo.get_setting(conn, "k", "dflt") == "dflt"
