"""Phase 1 of the 20 Nov audit: capture survives a failed write (B1), the claim check covers
requests (B2), one model connection per voice (B3), forget reaches the copies outside the
database and the window observer's memory (B4, B5), the search index is rewritten only when
text changes, range queries are bounded, embeddings stay on the CPU, the resolver rests when
there's nothing new, and "pause media when you're back" is answered, not dropped."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import deque
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from personal_os.config.schema import Config
from personal_os.context.route import is_question, route
from personal_os.core.bus import EventBus
from personal_os.core.clock import SimClock, SystemClock, to_ms
from personal_os.core.events import Event, WindowFocus
from personal_os.core.wiring import build_pipeline
from personal_os.storage import maintenance, repo
from personal_os.storage.db import open_db
from tests.conftest import T0


def focus(start_min: float, end_min: float, title: str = "Notes - Notepad") -> Event:
    return Event.interval(
        WindowFocus(app="Notepad", exe="notepad.exe", title=title),
        T0 + timedelta(minutes=start_min), T0 + timedelta(minutes=end_min),
        observer_version="t",
    )  # fmt: skip


# --- B1: a failed write is tried again, and capture goes on ---------------------------------


class Flaky:
    """A writer whose first `fails` writes fail as a locked database does."""

    def __init__(self, inner: Any, fails: int, exc: Exception) -> None:
        self.inner, self.fails, self.exc, self.calls = inner, fails, exc, 0
        self.conn = inner.conn

    def write(self, events: Any, retract: Any = ()) -> int:
        self.calls += 1
        if self.calls <= self.fails:
            raise self.exc
        return int(self.inner.write(events, retract=retract))


def run_pipeline(pipe: Any, events: list[Event]) -> None:
    async def go() -> None:
        bus, stop = EventBus(), asyncio.Event()
        task = asyncio.create_task(pipe.run(bus, stop, timeout_s=0.02, linger_s=0.0))
        for e in events:
            bus.publish(e)
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 10)

    asyncio.run(go())


def test_a_locked_database_is_waited_out_not_fatal(home: Path, config: Config,
                                                   monkeypatch: pytest.MonkeyPatch):  # fmt: skip
    from personal_os.core import pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "RETRY_FIRST_S", 0.01)
    conn = open_db(home / "b1.db")
    pipe = build_pipeline(config, conn, SystemClock(), honour_pause=False)
    pipe.writer = Flaky(pipe.writer, 2, sqlite3.OperationalError("database is locked"))
    run_pipeline(pipe, [focus(0, 1), focus(1, 2, "Other - Notepad")])
    assert conn.execute("SELECT count(*) FROM events WHERE source = 'window'").fetchone()[0] == 2
    assert pipe.stats.retries == 2 and pipe.stats.failed == 0
    assert "database is locked" in pipe.stats.last_error


def test_a_batch_that_can_never_be_written_is_set_aside(home: Path, config: Config):
    conn = open_db(home / "b1b.db")
    pipe = build_pipeline(config, conn, SystemClock(), honour_pause=False)
    pipe.writer = Flaky(pipe.writer, 1, ValueError("blocked event reached the writer"))
    run_pipeline(pipe, [focus(0, 1)])
    run_pipeline(pipe, [focus(1, 2, "Later - Notepad")])  # capture still works after it
    assert pipe.stats.failed == 1
    assert conn.execute("SELECT count(*) FROM events WHERE source = 'window'").fetchone()[0] == 1


# --- B2: a request routed to "ask" by default still has its claims checked -----------------


@pytest.mark.parametrize("said", ["music please", "louder", "I want to hear drake"])
def test_unplaced_requests_are_not_questions(said: str):
    assert route(said) == "ask" and not is_question(said)


@pytest.mark.parametrize("said", ["what's playing?", "do I have reminders", "is brave open"])
def test_questions_are_questions(said: str):
    assert is_question(said)


def test_a_claim_with_no_tool_behind_it_is_corrected(home: Path, config: Config):
    from personal_os.actions import agent
    from personal_os.llm.fake import FakeProvider
    from tests.test_actions import Env

    env = Env(home, config)

    def model(system: str, user: str, schema: Any) -> Any:
        return {"decide": "answer", "calls": [], "reply": "Playing your music now."}

    out = agent.respond("music please", env.ex, FakeProvider(model), max_steps=1)
    assert "Playing" not in out.reply and "haven't done that" in out.reply


# --- B3: one model connection per voice process ------------------------------------------


def test_voice_keeps_one_provider(config: Config):
    from personal_os.voice.main import Voice

    v = Voice(config)
    first = v.provider()
    assert first is not None and v.provider() is first
    assert first.cancel is v.interrupt


# --- B4, B5: forget reaches everything made from the window -------------------------------


def test_forget_trims_the_copies_outside_the_database(home: Path, config: Config):
    from personal_os.context import activity

    conn = open_db(home / "b4.db")
    clock = SimClock(T0 + timedelta(hours=2))
    pipe = build_pipeline(config, conn, clock, honour_pause=False)
    pipe.process([focus(10, 20, "Secret plans - YouTube")])
    rt, data = config.runtime_dir, config.data_dir
    rt.mkdir(parents=True, exist_ok=True)
    inside, outside = T0 + timedelta(minutes=15), T0 - timedelta(hours=1)
    (rt / "voice_history.jsonl").write_text(
        json.dumps({"at": outside.isoformat(), "heard": "keep me"}) + "\n"
        + json.dumps({"at": inside.isoformat(), "heard": "secret plans"}) + "\n",
        "utf-8",
    )  # fmt: skip
    (rt / "voice_last.json").write_text(json.dumps({"at": inside.isoformat()}), "utf-8")
    repo.set_setting(conn, "briefings.last", {"at": inside.isoformat(), "lines": ["x"]}, clock)
    activity.save_cache(data, {activity.media_key("Secret plans - YouTube"): {"topic": "plans"},
                               "other": {"topic": "kept"}})  # fmt: skip

    maintenance.forget(conn, T0, T0 + timedelta(minutes=30), clock, data_dir=data,
                       runtime_dir=rt)  # fmt: skip

    hist = (rt / "voice_history.jsonl").read_text("utf-8")
    assert "keep me" in hist and "secret plans" not in hist
    assert not (rt / "voice_last.json").exists()
    assert repo.get_setting(conn, "briefings.last") is None
    assert activity.load_cache(data) == {"other": {"topic": "kept"}}


def test_forget_clears_the_window_observers_memory():
    from personal_os.observers.window import WindowObserver

    t = lambda m: T0 + timedelta(minutes=m)  # noqa: E731
    history = deque([("a.exe", "early", t(0), t(5)), ("b.exe", "inside", t(12), t(18)),
                     ("c.exe", "late", t(40), t(45))])  # fmt: skip
    obs = SimpleNamespace(tracker=SimpleNamespace(reset=lambda: None, history=history))
    WindowObserver.forget(obs, t(10), t(30))  # type: ignore[arg-type]
    assert [x[1] for x in history] == ["early", "late"]


# --- storage: the search index, bounded ranges ---------------------------------------------


def test_the_search_index_is_rewritten_only_when_text_changes(home: Path, config: Config):
    conn = open_db(home / "fts.db")
    pipe = build_pipeline(config, conn, SystemClock(), honour_pause=False)
    e = focus(0, 1)
    pipe.process([e])
    before = conn.total_changes
    pipe.process([e.model_copy(update={"ts_end": T0 + timedelta(minutes=2)})])  # checkpoint
    same_text = conn.total_changes - before
    before = conn.total_changes
    changed = e.model_copy(update={"payload": WindowFocus(app="Notepad", exe="notepad.exe",
                                                          title="Renamed - Notepad")})  # fmt: skip
    pipe.process([changed])
    assert conn.total_changes - before > same_text  # the index rows: delete and insert
    hits = conn.execute("SELECT count(*) FROM events_fts WHERE events_fts MATCH 'renamed'")
    assert hits.fetchone()[0] == 1


def test_a_long_event_is_still_found_by_a_bounded_query(home: Path, config: Config):
    from personal_os.context.evidence import fetch_rows

    conn = open_db(home / "range.db")
    pipe = build_pipeline(config, conn, SystemClock(), honour_pause=False)
    pipe.process([focus(-60 * 30, 5, "All day - Notepad"), focus(-60 * 24 * 5, -60 * 24 * 5 + 1)])
    rows = fetch_rows(conn, to_ms(T0), to_ms(T0 + timedelta(minutes=1)))
    assert [r["ts_start"] for r in rows] == [to_ms(T0 - timedelta(hours=30))]


# --- the model's helpers --------------------------------------------------------------------


def test_embeddings_never_use_the_gpu(config: Config, monkeypatch: pytest.MonkeyPatch):
    from personal_os.llm.ollama import OllamaProvider

    p = OllamaProvider(config.llm)
    sent: list[dict[str, Any]] = []

    def fake(path: str, body: dict[str, Any], on_text: Any = None) -> dict[str, Any]:
        sent.append(body)
        return {"embeddings": [[0.1] * 8 for _ in body["input"]]}

    monkeypatch.setattr(p, "_with_gpu_fallback", fake)
    p.embed(["a page"])
    p.embed(["a question"], query=True)
    assert [b["options"]["num_gpu"] for b in sent] == [0, 0]


def test_the_resolver_rests_when_nothing_is_new(home: Path, config: Config):
    from personal_os.projects.resolver import Resolver

    conn = open_db(home / "res.db")
    r = Resolver(conn, SystemClock())

    def boom() -> None:
        raise AssertionError("nothing to do: no rebuild")

    r.refresh = boom  # type: ignore[method-assign]
    assert r.resolve_pending() == 0


# --- "pause media when you're back" ----------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "now", "later"),
    [
        ("take 1 min off, and pause media when you're back and say hi",
         ["take 1 min off and tell me when you're back"], ["pause media"]),
        ("take 5 and when you're back pause the music", ["take 5"], ["pause the music"]),
        ("take 5 and say hi when you're back", ["take 5 and tell me when you're back"], []),
        ("pause music and play numb", ["pause music", "play numb"], []),
    ],
)  # fmt: skip
def test_what_waits_for_the_end_of_a_rest(said: str, now: list[str], later: list[str]):
    from personal_os.voice.main import split_requests
    from personal_os.voice.rest import parse, plan_parts

    assert plan_parts(split_requests(said)) == (now, later)
    assert parse("take 1 min off") == 1


def test_voice_says_what_it_cant_do_later(home: Path, config: Config):
    from tests.test_actions import Env
    from tests.test_phase7 import voice_for

    v, _h = voice_for(Env(home, config))
    reply = v.fast("take 1 min off, and pause media when you're back and say hi")
    assert reply is not None and "taking a minute" in reply.lower()
    assert "I can't do things later yet, so I didn't pause media" in reply
    assert v.resting()
    from personal_os.voice import rest

    assert rest.announces(config.runtime_dir)  # she says she's back (the "say hi")
