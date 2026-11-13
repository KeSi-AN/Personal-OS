"""Commitments: "I'll finish the deck tonight", kept with your yes, shown to Donna, said when due."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from personal_os.actions import agent
from personal_os.config.schema import Config
from personal_os.context import commitments
from personal_os.context.commitments import detect, due_from
from personal_os.core.clock import SimClock
from personal_os.llm.fake import FakeProvider
from personal_os.projects import items
from tests.test_actions import Confirm, Env
from tests.test_agent import Script, answer

IST = timezone(timedelta(hours=5, minutes=30))
FRI = datetime(2026, 11, 13, 14, 30, tzinfo=IST)  # a Friday afternoon


@pytest.mark.parametrize(
    ("said", "text", "when"),
    [
        ("I'll finish the deck tonight", "finish the deck", "tonight"),
        ("I need to call mum tomorrow morning", "call mum", "tomorrow morning"),
        ("I have to submit the assignment by friday", "submit the assignment", "by friday"),
        ("okay I'll have the report done by 5", "have the report done", "by 5"),
        ("I'm going to clean my room this evening", "clean my room", "this evening"),
        ("I'll review the PR in 2 hours", "review the PR", "in 2 hours"),
        ("I must renew my passport by end of the day", "renew my passport", "by end of the day"),
        ("I need to buy 2", "buy 2", ""),  # a bare number isn't a time
        ("I should finish the essay", "finish the essay", ""),
    ],
)
def test_commitments_are_spotted_with_their_time(said: str, text: str, when: str):
    assert detect(said) == commitments.Proposal(text, when)


@pytest.mark.parametrize(
    "said",
    [
        "I finished the deck",  # done, not promised
        "do I have to pay rent?",  # a question
        "I'll finish it tonight?",
        "I need to open chrome",  # something to do on the computer now
        "I need to see my reminders",
        "I'll be back",
        "I'll let you know",
        "I want to play some music",
        "remind me to stretch in 10 minutes",  # a reminder has its own path
    ],
)
def test_other_sentences_are_not_commitments(said: str):
    assert detect(said) is None


@pytest.mark.parametrize(
    ("when", "due"),
    [
        ("tonight", datetime(2026, 11, 13, 21, 0)),
        ("this evening", datetime(2026, 11, 13, 19, 0)),
        ("tomorrow morning", datetime(2026, 11, 14, 11, 0)),
        ("tomorrow at 10", datetime(2026, 11, 14, 10, 0)),
        ("by friday", datetime(2026, 11, 13, 18, 0)),  # said on a Friday: today
        ("next friday", datetime(2026, 11, 20, 18, 0)),
        ("by monday", datetime(2026, 11, 16, 18, 0)),
        ("at 5pm", datetime(2026, 11, 13, 17, 0)),
        ("by 5", datetime(2026, 11, 13, 17, 0)),  # by 5 means the afternoon
        ("in 2 hours", datetime(2026, 11, 13, 16, 30)),
        ("by end of the day", datetime(2026, 11, 13, 18, 0)),
    ],
)
def test_due_times(when: str, due: datetime, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    got = due_from(when, FRI)
    assert got is not None
    assert got.astimezone(IST).replace(tzinfo=None) == due


def test_a_time_already_past_today_becomes_an_hour_from_now():
    late = FRI.replace(hour=22, minute=30)
    assert due_from("tonight", late) == late.astimezone() + timedelta(hours=1)


def test_no_phrase_no_due_time_and_an_unreadable_one_is_an_error():
    assert due_from("", FRI) is None
    with pytest.raises(ValueError):
        due_from("whenever", FRI)


# --- kept only with your yes --------------------------------------------------------------------


def never_called(*_a: object) -> dict[str, object]:
    raise AssertionError("a commitment is offered without the model")


def test_a_commitment_is_kept_after_a_yes(home: Path, config: Config):
    confirm = Confirm(True)
    env = Env(home, config, confirm)
    r = agent.respond("I'll finish the deck tonight", env.ex, FakeProvider(never_called))
    assert len(confirm.prompts) == 1 and "finish the deck" in confirm.prompts[0]
    assert r.reply.startswith('Got it. I\'ll hold you to "finish the deck"')
    [todo] = items.todos(env.conn)
    assert todo.title == "finish the deck" and todo.due is not None and not todo.done


def test_without_a_yes_nothing_is_kept(home: Path, config: Config):
    env = Env(home, config, Confirm(False))
    r = agent.respond("I need to call mum tomorrow morning", env.ex, FakeProvider(never_called))
    assert r.reply == "Okay, I won't keep track of it." and items.todos(env.conn) == []


def test_asking_for_it_directly_needs_no_question(home: Path, config: Config):
    confirm = Confirm()
    env = Env(home, config, confirm)
    out = env.ex.request(
        "commit_to", {"text": "send the invoice", "when": "at 5pm"}, origin=agent.Origin.PLANNED,
        request_text="hold me to sending the invoice at 5pm",
    )  # fmt: skip
    assert out.ok and confirm.prompts == []


def test_undo_removes_it(home: Path, config: Config):
    env = Env(home, config, Confirm(True))
    r = agent.respond("I'll finish the deck tonight", env.ex, FakeProvider(never_called))
    assert env.ex.undo(r.outcomes[0].action_id).ok
    assert items.todos(env.conn) == []


# --- asked about, answered by rules ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "asked"),
    [
        ("what did I say I'd do?", True),
        ("is anything overdue?", True),
        ("whats due", True),
        ("what do I have to do today", True),
        ("what's on my list", True),
        ("list my todos", True),
        ("what did I say earlier", False),  # the conversation
        ("what did I do yesterday", False),  # recall
        ("what's due on my calendar", False),  # the calendar's own tool
        ("what did you say", False),
    ],
)
def test_questions_about_commitments(said: str, asked: bool):
    assert commitments.asked_about(said) is asked


def test_they_are_answered_from_the_list_overdue_first(home: Path, config: Config):
    env = Env(home, config)
    now = env.clock.now()
    items.add(env.conn, env.clock, None, "todo", title="send the invoice to Ravi",
              due=now - timedelta(hours=3))  # fmt: skip
    items.add(env.conn, env.clock, None, "todo", title="finish the deck",
              due=now + timedelta(hours=4))  # fmt: skip
    items.add(env.conn, env.clock, None, "todo", title="water the plants")
    r = agent.respond("is anything overdue?", env.ex, FakeProvider(never_called))
    assert r.reply.startswith("One thing is overdue: send the invoice to Ravi, due")
    assert "Next: finish the deck" in r.reply and "1 more on screen" in r.reply
    assert "OVERDUE" in r.details and "water the plants" in r.details


def test_nothing_open_is_said_plainly(home: Path, config: Config):
    env = Env(home, config)
    r = agent.respond("what did I say I'd do?", env.ex, FakeProvider(never_called))
    assert r.reply.startswith("Nothing's open.")


# --- shown to Donna, said when due ---------------------------------------------------------------


def test_open_commitments_reach_the_agent_overdue_first(home: Path, config: Config):
    env = Env(home, config)
    now = env.clock.now()
    items.add(env.conn, env.clock, None, "todo", title="water the plants")
    items.add(env.conn, env.clock, None, "todo", title="send the invoice",
              due=now - timedelta(hours=2))  # fmt: skip
    items.add(env.conn, env.clock, None, "todo", title="research link", source="auto")
    block = commitments.prompt_block(env.conn, now)
    lines = block.splitlines()[1:]
    assert lines[0].startswith("- send the invoice: due") and "OVERDUE" in lines[0]
    assert "- water the plants" in lines and "research link" not in block  # not yours
    script = Script(answer("Start with the invoice: it's overdue."))
    provider = FakeProvider(script)
    agent.respond("should I start with the invoice or the plants", env.ex, provider)
    system, user = provider.calls[0]
    assert "send the invoice" in user and "send the invoice" not in system  # cache intact


def test_nothing_open_adds_nothing(home: Path, config: Config):
    env = Env(home, config)
    assert commitments.prompt_block(env.conn, env.clock.now()) == ""


def test_a_due_commitment_is_said_once(home: Path, config: Config):
    env = Env(home, config)
    clock = SimClock(env.clock.now())
    items.add(
        env.conn, clock, None, "todo", title="call mum", due=clock.now() + timedelta(minutes=5)
    )
    assert commitments.take_due(env.conn, clock) == []  # not yet
    clock.advance(6 * 60)
    [due] = commitments.take_due(env.conn, clock)
    assert due.text == "call mum"
    assert commitments.take_due(env.conn, clock) == []  # once
    done = items.add(env.conn, clock, None, "todo", title="done one", due=clock.now())
    items.set_done(env.conn, clock, done)
    assert commitments.take_due(env.conn, clock) == []  # ticked off: nothing to say
