"""Phase 0 acceptance: simulator events flow bus → privacy filter → SQLite, and nothing that
should be blocked or redacted ever reaches the database file."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest

from personal_os.core.bus import EventBus
from personal_os.core.clock import SimClock, SystemClock
from personal_os.core.events import PageVisit, WindowFocus
from personal_os.core.wiring import build_pipeline
from personal_os.sim.generator import generate
from personal_os.sim.replay import dump_jsonl, feed_through_bus, load_jsonl
from personal_os.sim.scenario import load_scenario, parse_duration
from personal_os.storage import maintenance, repo
from personal_os.storage.db import open_db
from tests.conftest import FIXTURES

WORKDAY = FIXTURES / "scenarios" / "workday.toml"
PLANTED = [b"zqxjtoken", b"hunter2", b"482913", b"private-thing", b"reels", b"Something private"]


def _bytes(path: Path) -> bytes:
    return b"".join(
        p.read_bytes() for p in (path, path.with_name(path.name + "-wal")) if p.exists()
    ).lower()


@pytest.fixture
def stored(home: Path, config):
    path = home / "sim.db"
    conn = open_db(path)
    pipeline = build_pipeline(config, conn, SystemClock(), honour_pause=False)
    import asyncio

    asyncio.run(feed_through_bus(generate(load_scenario(WORKDAY)), pipeline, EventBus(maxsize=16)))
    return conn, path, pipeline


def test_scenario_generates_checkpointed_intervals():
    events = generate(load_scenario(WORKDAY))
    focus_ids = Counter(e.id for e in events if e.source == "window")
    assert max(focus_ids.values()) > 10  # long focus intervals were checkpointed every 60 s
    assert events == sorted(events, key=lambda e: e.last_seen or e.ts_end or e.ts_start)


def test_end_to_end_storage(stored):
    conn, _path, pipeline = stored
    events = list(repo.iter_events(conn))
    kinds = Counter(e.kind for e in events)
    assert kinds["terminal.command"] == 5
    assert kinds["git.commit"] == 1
    assert kinds["browser.page_visit"] == 3  # SO + docs + mail (sensitive); 5 others blocked
    # every focus interval stored once and closed, despite many checkpoints
    focus = [e for e in events if e.source == "window"]
    assert all(e.ts_end is not None for e in focus)
    assert len({e.id for e in focus}) == len(focus)
    # WhatsApp desktop app never stored; WhatsApp tab in Brave stored as time only
    assert not any(
        isinstance(e.payload, WindowFocus) and e.payload.exe == "WhatsApp.exe" for e in events
    )
    assert any(
        isinstance(e.payload, WindowFocus) and e.payload.title == "[redacted]" for e in focus
    )
    mail = [
        e for e in events if isinstance(e.payload, PageVisit) and "mail.google.com" in e.payload.url
    ]
    assert mail[0].privacy_class == "sensitive" and mail[0].payload.extract is None  # type: ignore[union-attr]
    # .env edit recorded as sensitive, with no details
    env = [e for e in events if e.source == "fs" and e.payload.path.endswith(".env")]  # type: ignore[union-attr]
    assert env[0].privacy_class == "sensitive"
    # project hint from repo root
    assert {e.project_hint for e in events if e.source == "git"} == {
        "C:/Users/addya/Documents/auth-service"
    }
    c = pipeline.privacy.counters
    assert c.blocked == {"blocked_domain": 2, "blocked_app": c.blocked["blocked_app"],
                         "password_field": 1, "incognito": 1, "short_dwell": 1}  # fmt: skip
    assert c.errors == {}
    assert repo.get_counters(conn)["filter.blocked"]["incognito"] == 1


def test_nothing_planted_reaches_the_database_file(stored):
    conn, path, _ = stored
    conn.close()
    raw = _bytes(path)
    for marker in PLANTED:
        assert marker.lower() not in raw, marker


def test_research_is_searchable(stored):
    conn, _, _ = stored
    kinds = {e.kind for e, _ in repo.search(conn, "refresh token")}
    assert {"browser.page_visit", "browser.search", "git.commit"} <= kinds
    hits = repo.search(conn, "auth_flow")  # only in the page extract text
    assert hits and isinstance(hits[0][0].payload, PageVisit)


def test_forget_slice_of_simulated_day(stored):
    conn, _path, _ = stored
    t = load_scenario(WORKDAY).start
    clock = SimClock(t + timedelta(hours=3))
    before = sum(1 for _ in repo.iter_events(conn))
    report = maintenance.forget(conn, t + timedelta(minutes=21), t + timedelta(minutes=29), clock)
    assert report.events > 0 and report.page_extracts == 2
    assert repo.search(conn, "auth_flow") == []
    assert sum(1 for _ in repo.iter_events(conn)) == before - report.events + 1  # + marker


def test_jsonl_roundtrip(tmp_path: Path):
    events = generate(load_scenario(WORKDAY))
    out = tmp_path / "w.jsonl"
    assert dump_jsonl(events, out) == len(events)
    assert list(load_jsonl(out)) == events


def test_bus_backpressure_drops_nothing_when_producer_waits(stored):
    _, _, pipeline = stored
    assert pipeline.bus is not None and sum(pipeline.bus.stats.dropped.values()) == 0


@pytest.mark.parametrize(
    ("text", "seconds"), [("90s", 90), ("25m", 1500), ("1h30m", 5400), (12, 12)]
)
def test_parse_duration(text, seconds):
    assert parse_duration(text) == seconds


def test_parse_duration_rejects_garbage():
    with pytest.raises(ValueError):
        parse_duration("soon")
