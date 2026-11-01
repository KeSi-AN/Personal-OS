"""`forget` must remove every trace, including from the database file's bytes (DESIGN.md §9)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from personal_os.core.clock import SimClock
from personal_os.core.events import Event, PageExtract, PageVisit, TerminalCommand, WindowFocus
from personal_os.core.ids import uuid7
from personal_os.storage import maintenance, repo
from personal_os.storage.db import open_db
from personal_os.storage.writer import EventWriter
from tests.conftest import T0

MARKER = "Zqxjforgetmarker7731"


def db_bytes(path: Path) -> bytes:
    data = b""
    for suffix in ("", "-wal"):
        p = path.with_name(path.name + suffix)
        if p.exists():
            data += p.read_bytes()
    return data


def test_forget_window_removes_rows_fts_and_bytes(home: Path, config):
    path = home / "f.db"
    conn = open_db(path)
    clock = SimClock(T0 + timedelta(hours=2))
    w = EventWriter(conn, config.retention, clock)
    inside = T0 + timedelta(minutes=30)
    w.write(
        [
            Event.point(WindowFocus(app="Code", title="before"), T0, observer_version="t"),
            Event.interval(TerminalCommand(command=f"echo {MARKER}"), inside,
                           inside + timedelta(seconds=2), observer_version="t"),
            Event.interval(PageVisit(url="https://x.dev/p", title=f"{MARKER} page",
                                     extract=PageExtract(main_text=f"text {MARKER} more")),
                           inside, inside + timedelta(minutes=3), observer_version="t"),
            # still-open interval that started before the window: overlaps, so it goes too
            Event.interval(WindowFocus(app="Brave", title=f"{MARKER} open"), inside - timedelta(minutes=5),
                           None, id=uuid7(inside), observer_version="t"),
        ]
    )  # fmt: skip
    assert repo.search(conn, MARKER)
    assert MARKER.lower().encode() in db_bytes(path) or MARKER.encode() in db_bytes(path)

    report = maintenance.forget(
        conn, inside - timedelta(minutes=1), inside + timedelta(minutes=10), clock
    )
    assert report.events == 3 and report.page_extracts == 1
    assert repo.search(conn, MARKER) == []
    kinds = [e.kind for e in repo.iter_events(conn)]
    assert kinds == ["window.focus", "meta.forget"]
    conn.close()

    raw = db_bytes(path)
    assert MARKER.encode() not in raw
    assert MARKER.lower().encode() not in raw  # FTS stores lower-cased tokens


def test_forget_rejects_empty_window(conn, clock):
    import pytest

    with pytest.raises(ValueError):
        maintenance.forget(conn, T0, T0, clock)
