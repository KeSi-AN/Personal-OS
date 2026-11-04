"""Phase 6 services: memory, reminders, brightness, rollups, follow-ups, forget, pairing."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from personal_os.actions import reminders
from personal_os.actions.base import Origin
from personal_os.config.schema import Config
from personal_os.context import ask as ask_mod
from personal_os.context import memory, rollups
from personal_os.context.summarizer import summarize_session
from personal_os.core.clock import SimClock
from personal_os.llm.fake import FakeProvider
from personal_os.projects.sessions import load_sessions, rebuild_sessions
from personal_os.storage import maintenance
from personal_os.web import auth
from tests.conftest import T0
from tests.test_actions import Env
from tests.test_context import auth_session, refs_of, summary_reply
from tests.test_projects import World


@pytest.fixture
def w(home: Path, config: Config) -> World:
    return World(home, config)


# --- memory ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "fact"),
    [
        ("remember that the staging db is on port 5433", "the staging db is on port 5433"),
        ("Please remember: standup moved to 10:30.", "standup moved to 10:30"),
        ("remember auth-service uses httpx", "auth-service uses httpx"),
        ("what do you remember about auth?", None),
        ("where did I leave off?", None),
    ],
)
def test_parse_remember(text: str, fact: str | None):
    assert memory.parse_remember(text) == fact


def test_memory_is_editable_and_reaches_the_answer_prompt(w: World):
    f = memory.add(w.conn, w.clock, "  the staging db   is on port 5433 ", "cli")
    assert f.text == "the staging db is on port 5433"
    assert memory.update(w.conn, w.clock, f.id, "staging db: port 5434")
    assert memory.find(w.conn, f.id[-8:]).text == "staging db: port 5434"
    auth_session(w)
    rebuild_sessions(w.conn, w.clock, w.at(-60), w.at(600))
    fake = FakeProvider(lambda s, u, schema: {"answered": False, "confidence": "low"})
    ask_mod.ask(w.conn, fake, "where did I leave off on auth-service?", w.at(120))
    assert "Fact: You told me: staging db: port 5434" in fake.calls[0][1]
    assert memory.delete(w.conn, f.id) and memory.all_facts(w.conn) == []


# --- follow-ups -----------------------------------------------------------------------------------


def test_follow_up_keeps_project_and_range(w: World):
    auth_session(w)
    rebuild_sessions(w.conn, w.clock, w.at(-60), w.at(600))
    first = ask_mod.ask(
        w.conn, None, "what did I do yesterday on auth-service?", w.at(60 * 24 + 60)
    )
    again = ask_mod.ask(
        w.conn, None, "and which commands failed?", w.at(60 * 24 + 60), previous=first.question
    )
    assert again.question.project and again.question.project.id == "auth-service"
    assert again.question.range_label == first.question.range_label
    fresh = ask_mod.ask(w.conn, None, "and which commands failed?", w.at(60 * 24 + 60))
    assert fresh.question.project is None


# --- reminders and brightness -----------------------------------------------------------------------


def test_due_time_parsing():
    now = T0  # 09:00 IST
    assert reminders.due_time(now, 30, None) == now + timedelta(minutes=30)
    assert reminders.due_time(now, None, "17:30").astimezone(T0.tzinfo).strftime("%H:%M") == "17:30"
    five = reminders.due_time(now, None, "5pm").astimezone(T0.tzinfo)
    assert five.hour == 17 and five.date() == now.date()
    early = reminders.due_time(now, None, "8am").astimezone(T0.tzinfo)
    assert early.date() > now.date()  # already past today: tomorrow
    with pytest.raises(ValueError):
        reminders.due_time(now, None, "teatime")


def test_reminder_tool_fires_once_and_can_be_undone(home: Path, config: Config):
    env = Env(home, config)
    out = env.run("set_reminder", text="stretch", minutes=20)
    assert out.ok and "remind you" in out.preview
    assert reminders.take_due(env.conn, SimClock(T0 + timedelta(minutes=10))) == []
    due = reminders.take_due(env.conn, SimClock(T0 + timedelta(minutes=21)))
    assert [r.text for r in due] == ["stretch"]
    assert reminders.take_due(env.conn, SimClock(T0 + timedelta(minutes=30))) == []  # only once
    second = env.run("set_reminder", text="call back", at="17:00")
    assert env.ex.undo(second.action_id).ok and reminders.pending(env.conn) == []
    assert env.run("set_reminder", text="x").status == "blocked"  # no time given


def test_brightness_tool_and_undo(home: Path, config: Config):
    env = Env(home, config)
    out = env.run("set_brightness", percent=30)
    assert out.ok and env.desktop.brightness == 30
    env.ex.undo(out.action_id)
    assert env.desktop.brightness == 50
    assert env.run("set_brightness", percent=150).status == "invalid"


# --- rollups ----------------------------------------------------------------------------------------


def _summarise(w: World) -> None:
    rebuild_sessions(w.conn, w.clock, w.at(-60), w.at(600))
    fake = FakeProvider(lambda s, u, schema: summary_reply(refs_of(u, "edited")))
    for s in load_sessions(w.conn, w.at(-60), w.at(600)):
        summarize_session(w.conn, fake, w.clock, s)


def test_project_state_and_day_rollups_are_grounded(w: World):
    auth_session(w)
    _summarise(w)
    clock = SimClock(w.at(180))

    def reply(system: str, user: str, schema: Any) -> dict[str, Any]:
        summary = refs_of(user, "session")
        commit = refs_of(user, "commit")
        if "current state" in user:
            return {"goal": "OAuth token refresh for auth-service.",
                    "done": [{"text": "Committed token refresh.", "refs": commit},
                             {"text": "Invented.", "refs": [999]}],
                    "in_progress": [], "blockers": [],
                    "next_steps": [{"text": "Handle timeouts.", "refs": summary}],
                    "key_files": ["session.py"], "key_links": []}  # fmt: skip
        return {"headline": "A focused auth day.",
                "by_project": [{"text": "auth-service: token refresh.", "refs": summary}],
                "open_threads": []}  # fmt: skip

    fake = FakeProvider(reply)
    todo = rollups.pending(w.conn, clock)
    assert ("project", "auth-service") in todo and any(k == "day" for k, _ in todo)
    assert rollups.write_project_state(w.conn, fake, clock, "auth-service")
    state = rollups.load(w.conn, "project:auth-service")
    assert state is not None and [i["text"] for i in state.body["done"]] == [
        "Committed token refresh."
    ]
    assert state.body["done"][0]["event_ids"]
    assert rollups.write_day(w.conn, fake, clock, w.at(0))
    assert rollups.load(w.conn, f"day:{w.at(0).astimezone():%Y-%m-%d}") is not None
    assert ("project", "auth-service") not in rollups.pending(w.conn, clock)  # up to date
    maintenance.forget(w.conn, w.at(0), w.at(60), w.clock)
    assert rollups.load(w.conn, "project:auth-service") is None


def test_forget_also_removes_facts_and_reminders_made_in_the_window(home: Path, config: Config):
    env = Env(home, config)
    memory.add(env.conn, env.clock, "secret plan", "cli")
    env.run("set_reminder", text="secret meeting", minutes=60)
    maintenance.forget(env.conn, T0 - timedelta(minutes=1), T0 + timedelta(minutes=1), env.clock)
    assert memory.all_facts(env.conn) == [] and reminders.pending(env.conn) == []


# --- pairing and pending approvals -------------------------------------------------------------------


def test_pairing_codes_expire_and_burn_after_guesses():
    clock = SimClock(T0)
    p = auth.Pairing(clock)
    code = p.start()
    assert re.fullmatch(r"\d{6}", code)
    for _ in range(auth.PAIRING_TRIES):
        assert not p.redeem("999999" if code != "999999" else "000000")
    assert not p.redeem(code)  # burnt by the guesses
    code = p.start()
    clock.advance(6 * 60)
    assert not p.redeem(code)  # expired
    code = p.start()
    assert p.redeem(code) and not p.redeem(code)  # one use


def test_deferred_confirmation_is_pending_and_audited(home: Path, config: Config):
    from personal_os.actions.audit import history
    from personal_os.actions.executor import Deferred

    def defer(prompt: str) -> bool:
        raise Deferred

    env = Env(home, config)
    env.ex.confirmer = defer
    out = env.ex.request(
        "create_file", {"path": str(env.work / "x.txt"), "content": "x"}, origin=Origin.USER
    )
    assert out.status == "pending" and not (env.work / "x.txt").exists()
    assert (
        history(env.conn, out.action_id)[-1].detail == "waiting for your approval in the dashboard"
    )
