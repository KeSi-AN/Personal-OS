"""Root causes behind a broken reminder conversation (2026-11-06):

    "Remind me in a minute."                      -> stored the message "Remind me in a minute"
    "Actually, edit that reminder to say ..."     -> sent to search: "I don't have data on that"
    "Do I have any upcoming reminders?"           -> routed as an action; a new reminder was set

1. Routing by first word: "do" was an action verb, fillers hid the verb, "edit" wasn't one.
2. Reminders were filled in by the model, which guessed a missing message.
3. There was no way to change or cancel a reminder.
4. The planner copied an action out of the conversation and ran it as "asked for directly".
5. A pause ended the turn before the message was said.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from personal_os.actions import reminder_parse, reminders
from personal_os.actions.base import Origin
from personal_os.actions.planner import borrowed, run_request
from personal_os.config.schema import Config
from personal_os.context.route import normalize, route
from personal_os.llm.fake import FakeProvider
from personal_os.voice.main import history
from tests.test_actions import Confirm, Env, plan
from tests.test_phase7 import voice_for
from tests.test_voice_fastpath import INDEX


@pytest.fixture
def quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    import winsound

    monkeypatch.setattr(winsound, "Beep", lambda *_a: None)


def no_model(*_a: Any) -> Any:
    raise AssertionError("the model was asked")


def pending(env: Env) -> list[str]:
    from personal_os.storage.db import open_db

    conn = open_db(env.config.db_path)
    try:
        return [p.text for p in reminders.pending_full(conn)]
    finally:
        conn.close()


# --- 1. routing -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Do I have any upcoming reminders?", "ask"),  # was "do": a question became an action
        ("Actually, edit that reminder to say, hey, go for a stretch.", "do"),  # was "ask"
        ("Remind me in a minute.", "do"),
        ("Does my reminder say stretch?", "ask"),
        ("Is there anything open in personal-os?", "ask"),
        ("Can you open brave?", "do"),
        ("Can I see what I did yesterday?", "ask"),
        ("okay so open brave", "do"),
        ("Hey Donna, um, cancel that reminder", "do"),
        ("change the volume to 40", "do"),
        ("what did I work on yesterday", "ask"),
        ("stop the music", "do"),
        ("stop", "stop"),
        ("okay", "chat"),
        ("thanks", "chat"),
    ],
)
def test_route(text: str, kind: str):
    assert route(text) == kind


def test_normalize_keeps_a_bare_filler():
    assert normalize("Actually, edit that") == "edit that"
    assert normalize("okay") == "okay"


# --- 2. reminders read by rules -------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "op", "args", "missing"),
    [
        ("Remind me in a minute.", "set", {"text": "", "minutes": 1}, "what"),
        ("remind me in 10 minutes to stretch", "set", {"text": "stretch", "minutes": 10}, None),
        ("remind me to stretch in 10 minutes", "set", {"text": "stretch", "minutes": 10}, None),
        ("remind me to call mum at 5 p.m.", "set", {"text": "call mum", "at": "5 p.m"}, None),
        ("set a reminder for two minutes saying hi", "set", {"text": "hi", "minutes": 2}, None),
        ("remind me in half an hour to drink water please", "set",
         {"text": "drink water", "minutes": 30}, None),
        ("remind me in 2 hours to leave", "set", {"text": "leave", "minutes": 120}, None),
        ("remind me to stretch", "set", {"text": "stretch"}, "when"),
        ("Actually, edit that reminder to say, hey, go for a stretch.", "update",
         {"text": "hey, go for a stretch"}, None),
        ("move the stretch reminder to 5 pm", "update", {"at": "5 pm", "which": "stretch"}, None),
        ("change the reminder to 17.30", "update", {"at": "17.30"}, None),
        ("postpone the reminder by 10 minutes", "update", {"delay_minutes": 10}, None),
        ("cancel that reminder", "cancel", {}, None),
        ("delete the water reminder", "cancel", {"which": "water"}, None),
        ("cancel all my reminders", "cancel", {"all": True}, None),
    ],
)  # fmt: skip
def test_parse(text: str, op: str, args: dict[str, Any], missing: str | None):
    r = reminder_parse.parse(text)
    assert r is not None and (r.op, r.args(), r.missing) == (op, args, missing)


@pytest.mark.parametrize(
    "text",
    [
        "what reminders do I have",
        "change the volume to 40",
        "make it louder",
        "move it to 5 pm",  # "it" means a reminder only right after one was talked about
        "open the reminders app",
    ],
)
def test_not_a_reminder_command(text: str):
    assert reminder_parse.parse(text) is None


def test_pronouns_after_a_reminder():
    r = reminder_parse.parse("make it 5 pm instead", allow_pronoun=True)
    assert r is not None and (r.op, r.args()) == ("update", {"at": "5 pm"})
    r = reminder_parse.parse("push it back 10 minutes", allow_pronoun=True)
    assert r is not None and r.args() == {"delay_minutes": 10}
    assert reminder_parse.parse("make it louder", allow_pronoun=True) is None


@pytest.mark.parametrize(
    ("answer", "text"),
    [("to stretch", "stretch"), ("Stretch.", "stretch"), ("drink some water", "drink some water"),
     ("", ""), ("never mind", ""), ("no", "")],
)  # fmt: skip
def test_message_from(answer: str, text: str):
    assert reminder_parse.message_from(answer).lower() == text


@pytest.mark.parametrize(
    ("answer", "expected"),
    [("in ten minutes", (10, None)), ("at 5 p.m.", (None, "5 p.m")), ("5 pm", (None, "5 pm")),
     ("ten minutes", (10, None)), ("whenever", (None, None))],
)  # fmt: skip
def test_time_from(answer: str, expected: tuple[int | None, str | None]):
    assert reminder_parse.time_from(answer) == expected


# --- 3. changing and cancelling -------------------------------------------------------------


def test_set_says_back_what_it_set_and_refuses_a_non_message(home: Path, config: Config):
    env = Env(home, config)
    ok = env.ex.request("set_reminder", {"text": "stretch", "minutes": 1}, origin=Origin.USER)
    assert ok.ok and '"stretch"' in ok.message and "today" in ok.message
    bad = env.ex.request(
        "set_reminder", {"text": "Remind me in a minute", "minutes": 1}, origin=Origin.USER
    )
    assert not bad.ok and "what should I remind you about" in bad.message


def test_update_the_latest_by_words_and_undo(home: Path, config: Config):
    env = Env(home, config)
    env.ex.request("set_reminder", {"text": "drink water", "minutes": 30}, origin=Origin.USER)
    env.ex.request("set_reminder", {"text": "stretch", "minutes": 1}, origin=Origin.USER)
    out = env.ex.request("update_reminder", {"text": "go for a stretch"}, origin=Origin.USER)
    assert out.ok and '"go for a stretch"' in out.message
    assert sorted(p.text for p in reminders.pending_full(env.conn)) == [
        "drink water",
        "go for a stretch",
    ]
    out = env.ex.request("update_reminder", {"which": "water", "at": "5pm"}, origin=Origin.USER)
    assert out.ok and "17:00" in out.message
    assert env.ex.undo(out.action_id).ok
    water = reminders.find(env.conn, "water")
    assert water is not None and water.text == "drink water" and "17:00" not in str(water.due)


def test_update_and_cancel_with_nothing_there(home: Path, config: Config):
    env = Env(home, config)
    out = env.ex.request("update_reminder", {"text": "x y"}, origin=Origin.USER)
    assert not out.ok and "no reminder coming up" in out.message
    out = env.ex.request("cancel_reminder", {}, origin=Origin.USER)
    assert not out.ok and "no reminder coming up" in out.message


def test_cancel_latest_named_and_all(home: Path, config: Config):
    env = Env(home, config)
    for text in ("stretch", "water", "call mum"):
        env.ex.request("set_reminder", {"text": text, "minutes": 5}, origin=Origin.USER)
    assert env.ex.request("cancel_reminder", {}, origin=Origin.USER).message == (
        'Cancelled the reminder "call mum".'
    )
    assert env.ex.request("cancel_reminder", {"which": "water"}, origin=Origin.USER).ok
    assert [p.text for p in reminders.pending_full(env.conn)] == ["stretch"]
    env.ex.request("set_reminder", {"text": "leave", "minutes": 9}, origin=Origin.USER)
    assert env.ex.request("cancel_reminder", {"all": True}, origin=Origin.USER).message == (
        "Cancelled 2 reminders."
    )


# --- 4. the planner and the conversation ----------------------------------------------------


def test_planner_handles_reminders_by_rules(home: Path, config: Config):
    env = Env(home, config)
    r = run_request("Remind me in a minute.", env.ex, FakeProvider(no_model))
    assert r.reply == "What should I remind you about?" and r.outcomes == []
    r = run_request("remind me in 2 minutes to stretch", env.ex, FakeProvider(no_model))
    assert r.outcomes and r.outcomes[0].ok
    r = run_request("edit that reminder to say go for a stretch", env.ex, FakeProvider(no_model))
    assert r.outcomes[0].ok and [p.text for p in reminders.pending_full(env.conn)] == [
        "go for a stretch"
    ]


def test_borrowed_args():
    context = "You: edit that reminder to say hey go for a stretch"
    assert borrowed({"text": "hey, go for a stretch", "minutes": 1}, "Do I have any?", context)
    assert not borrowed({"text": "stretch"}, "remind me to stretch", context)
    assert not borrowed({"text": "drink water"}, "remind me to drink water", context)


def test_context_cant_turn_into_an_action_without_asking(home: Path, config: Config):
    """The model copied an earlier sentence into a new reminder; now that needs a yes."""
    confirm = Confirm(False)
    env = Env(home, config, confirm)
    model = FakeProvider(
        lambda *_a: plan(("set_reminder", {"text": "hey, go for a stretch", "minutes": 1}))
    )
    r = run_request(
        "set something up for later",
        env.ex,
        model,
        context="You: edit that reminder to say hey go for a stretch",
    )
    assert confirm.prompts, "it ran without asking"
    assert not any(o.ok for o in r.outcomes)
    assert reminders.pending_full(env.conn) == []


# --- 5. the whole conversation, by voice ----------------------------------------------------


def test_the_conversation_that_went_wrong(home: Path, config: Config, quiet: None):
    env = Env(home, config)
    v, h = voice_for(
        env,
        "Remind me in a minute.",  # the message was cut off by a pause
        "to stretch",  # the answer to "What should I remind you about?"
        "Actually, edit that reminder to say, hey, go for a stretch.",
        "Do I have any upcoming reminders?",
        "thanks",
    )
    v.index._index = INDEX
    v.last_audio = None
    v.converse("hotkey")
    assert "What should I remind you about?" in h.said
    assert pending(env) == ["hey, go for a stretch"]  # one reminder, edited, nothing extra
    log = history(env.config.runtime_dir)
    assert [x["heard"] for x in log][:3] == [
        "Remind me in a minute.",
        "Actually, edit that reminder to say, hey, go for a stretch.",
        "Do I have any upcoming reminders?",
    ]
    assert '"stretch"' in log[0]["reply"]
    assert log[1]["reply"].startswith("Changed.")
    assert log[2]["reply"].startswith('You have 1 reminder coming up: "hey, go for a stretch"')


def test_a_reminder_with_no_answer_is_not_set(home: Path, config: Config, quiet: None):
    env = Env(home, config)
    v, _h = voice_for(env, "")
    v.index._index = INDEX
    assert v.fast("remind me in a minute") == "Okay, no reminder."
    assert pending(env) == []


def test_a_pause_before_the_message_keeps_listening():
    assert reminder_parse.needs_more("Remind me in a minute.")
    assert not reminder_parse.needs_more("remind me in a minute to stretch")
    assert not reminder_parse.needs_more("open brave")


@pytest.mark.parametrize(
    ("at", "when"),
    [("tomorrow 9am", ("tomorrow", "09:00")), ("tomorrow morning 9", ("tomorrow", "09:00")),
     ("9:30 tomorrow", ("tomorrow", "09:30")), ("this evening 7", (None, "19:00"))],
)  # fmt: skip
def test_longer_spoken_times(at: str, when: tuple[str | None, str]):
    from datetime import UTC, datetime

    now = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)
    due = reminders.due_time(now, None, at).astimezone()
    day, hhmm = when
    assert f"{due:%H:%M}" == hhmm
    if day == "tomorrow":
        assert due.date() > now.astimezone().date()
