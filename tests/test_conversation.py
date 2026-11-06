"""Conversations with Donna: answering her questions in your own words, amendments,
follow-ups without the hotkey, and short-term context. No audio, no real model."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from personal_os.config.schema import Config
from personal_os.llm.fake import FakeProvider
from personal_os.voice import dialogue, router
from personal_os.voice.main import history
from tests.test_actions import Env
from tests.test_phase7 import voice_for
from tests.test_voice_fastpath import INDEX


@pytest.fixture
def quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    import winsound

    monkeypatch.setattr(winsound, "Beep", lambda *_a: None)


def asked(said: str = "open brat") -> router.Intent:
    intent = router.route(said, INDEX)
    assert intent is not None and not intent.sure, intent
    return intent


def opens(reading: dialogue.Reading) -> set[str]:
    assert reading.intent is not None
    return router.targets(reading.intent)


# --- understanding an answer ----------------------------------------------------------------


@pytest.mark.parametrize("answer", ["yes", "yeah, that one", "the first one"])
def test_choosing_an_offered_option(answer: str):
    r = dialogue.understand(answer, asked(), INDEX)
    assert r.kind == "choose" and opens(r) == {"Brave"}


@pytest.mark.parametrize(
    "answer",
    [
        "No, I wanted the calculator",
        "no I meant calculator",
        "Calculator.",
        "not that, calculator instead",
        "actually it's calculator",
    ],
)
def test_correcting_to_something_not_offered(answer: str):
    """The bug: "did you mean Brave?" -> "I wanted the calculator" used to give up."""
    r = dialogue.understand(answer, asked(), INDEX)
    assert r.kind == "correct" and opens(r) == {"Calculator"}


def test_correcting_with_a_whole_command():
    r = dialogue.understand("no, open chrome", asked(), INDEX)
    assert r.kind == "correct" and opens(r) == {"Google Chrome"}


def test_a_correction_that_is_itself_unsure_asks_again():
    r = dialogue.understand("no, I wanted brat", asked(), INDEX)
    assert r.kind == "correct" and r.intent is not None and not r.intent.sure


@pytest.mark.parametrize("answer", ["no", "Nope.", "never mind", "cancel", "neither", ""])
def test_cancelling(answer: str):
    assert dialogue.understand(answer, asked(), INDEX).kind == "cancel"


def test_a_name_that_isnt_here_goes_to_the_normal_path_not_the_bin():
    r = dialogue.understand("no, I wanted zoom", asked(), INDEX)
    assert r.kind == "new" and r.text == "open zoom"


@pytest.mark.parametrize(
    ("answer", "wanted"),
    [
        ("forget that, what's the weather", "what's the weather"),
        ("no, remind me at 5", "remind me at 5"),
        ("scratch that. Lock the screen please", "Lock the screen"),
    ],
)
def test_no_then_a_different_request(answer: str, wanted: str):
    r = dialogue.understand(answer, asked(), INDEX)
    if r.kind == "correct":  # "lock the screen" is itself a fast-path command
        assert r.intent is not None and r.intent.sure
    else:
        assert (r.kind, r.text) == ("new", wanted)


def test_asking_for_something_else_entirely():
    r = dialogue.understand("what's on my calendar today", asked(), INDEX)
    assert r.kind == "new" and r.text == "what's on my calendar today"


def test_mumbling_is_unclear():
    assert dialogue.understand("um", asked(), INDEX).kind == "unclear"


def test_the_model_places_what_the_rules_cannot():
    seen: list[str] = []

    def responder(system: str, user: str, schema: Any) -> dict[str, Any]:
        seen.append(user)
        return {"kind": "choose", "choice": 1}

    classify = dialogue.model_classifier(FakeProvider(responder), INDEX)
    r = dialogue.understand("the one with the lion", asked(), INDEX, classify=classify)
    assert r.kind == "choose" and r.by_model and opens(r) == {"Brave"}
    assert "Did you mean Brave" in seen[0] and "the one with the lion" in seen[0]

    def correct(*_a: Any) -> dict[str, Any]:
        return {"kind": "correct", "name": "calculator"}

    r = dialogue.understand(
        "the maths one",
        asked(),
        INDEX,
        classify=dialogue.model_classifier(FakeProvider(correct), INDEX),
    )
    assert r.kind == "correct" and opens(r) == {"Calculator"}


def test_a_broken_model_falls_back_to_the_rules():
    classify = dialogue.model_classifier(FakeProvider(None), INDEX)  # raises when called
    r = dialogue.understand("the one with the lion", asked(), INDEX, classify=classify)
    assert r.kind == "new"  # three or more words: handled as a request, not dropped


# --- yes, no, and amendments ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("yes", ("yes", "")),
        ("yeah go ahead", ("yes", "")),
        ("no", ("no", "")),
        ("", ("no", "")),
        ("yes, no wait", ("no", "")),  # ambiguous is a no (ADR-066)
        ("no, make it 5 pm instead", ("amend", "5 pm")),
        ("no, remind me at 5 pm instead", ("amend", "remind me at 5 pm")),
        ("yes but in VS Code", ("amend", "in VS Code")),
        ("no thanks", ("no", "")),
        ("no, cancel", ("no", "")),
    ],
)
def test_yes_no(answer: str, expected: tuple[str, str]):
    assert dialogue.yes_no(answer) == expected


@pytest.mark.parametrize(
    ("text", "ends"),
    [
        ("thanks", True),
        ("Thank you Donna.", True),
        ("okay thanks", True),
        ("that's all", True),
        ("no thanks", True),
        ("thanks, now open brave", False),
        ("open brave", False),
        ("what about tomorrow?", False),
    ],
)
def test_closing(text: str, ends: bool):
    assert dialogue.closing(text) is ends


# --- short-term context ---------------------------------------------------------------------


def test_conversation_keeps_both_sides_and_expires():
    c = dialogue.Conversation(ttl_s=300)
    c.add(
        "remind me in two minutes to stretch", "I'll remind you at 16:34 to stretch.", "do", now=0
    )
    c.add("what did I work on yesterday", "You worked on the parser.", "ask", now=10)
    text = c.for_planner(now=20)
    assert "You: remind me in two minutes to stretch" in text
    assert "Donna: I'll remind you at 16:34" in text
    assert "You: what did I work on yesterday" in text
    # Donna's own answers are kept too ("those tabs" needs them); anything copied from them
    # into an action needs a yes (planner.borrowed).
    assert "Donna: You worked on the parser." in text
    assert c.active(now=309) and not c.active(now=311)
    assert c.for_planner(now=400) == ""


def test_the_planner_sees_the_conversation(home: Path, config: Config):
    from personal_os.actions.planner import run_request
    from tests.test_actions import plan

    env = Env(home, config)
    prompts: list[str] = []

    def responder(system: str, user: str, schema: Any) -> Any:
        prompts.append(user)
        return plan()

    run_request(
        "make it 5 pm instead",
        env.ex,
        FakeProvider(responder),
        context="You: remind me in two minutes to stretch",
    )
    assert prompts and "remind me in two minutes to stretch" in prompts[0]
    assert prompts[0].rstrip().endswith("Request: make it 5 pm instead")


# --- the voice loop -------------------------------------------------------------------------


def runs(v: Any) -> list[Any]:
    ran: list[Any] = []

    def fake(intent: Any, text: str) -> str:
        ran.append(router.targets(intent))
        return str(intent.reply)

    v.run_intent = fake
    v.index._index = INDEX
    v.last_audio = None
    return ran


def test_did_you_mean_then_i_wanted(home: Path, config: Config):
    v, _h = voice_for(Env(home, config), "No, I wanted the calculator")
    ran = runs(v)
    assert v.fast("open brat") == "Opening Calculator."
    assert ran == [{"Calculator"}]
    assert v.asked["understood"] == "correct"


def test_unsure_twice_then_it_stops_asking(home: Path, config: Config):
    v, h = voice_for(Env(home, config), "um", "er")
    runs(v)
    assert v.fast("open brat") == "I still couldn't tell which one, so I left it."
    assert h.said[1].startswith("Sorry, which one?")


def test_something_else_becomes_the_next_request(home: Path, config: Config):
    v, _h = voice_for(Env(home, config), "what time is it in the evening")
    runs(v)
    assert v.fast("open brat") == ""
    assert v.next_request == "what time is it in the evening"


def test_a_no_with_a_change_is_an_amendment(home: Path, config: Config):
    v, h = voice_for(Env(home, config), "no, make it 5 pm instead")
    assert v.confirm("set_reminder (tier 2)\n  stretch at 16:34\nProceed?") is False
    assert v.next_request == "5 pm"
    assert h.said[-1] == "Okay, changing it."


def test_follow_ups_without_the_hotkey(home: Path, config: Config, quiet: None):
    env = Env(home, config)
    v, _h = voice_for(env, "open brave", "open the calculator", "thanks")
    ran = runs(v)
    v.converse("hotkey")
    assert ran == [{"Brave"}, {"Calculator"}]
    log = history(env.config.runtime_dir)
    assert [x["trigger"] for x in log] == ["hotkey", "followup", "followup"]
    assert log[-1]["route"] == "end of conversation"
    assert not v.convo.active()  # "thanks" ends it


def test_silence_closes_the_window_quietly(home: Path, config: Config, quiet: None):
    env = Env(home, config)
    v, _h = voice_for(env, "open brave")  # then nothing
    runs(v)
    v.converse("hotkey")
    assert [x["trigger"] for x in history(env.config.runtime_dir)] == ["hotkey"]


def test_follow_ups_off(home: Path, config: Config, quiet: None):
    off = config.model_copy(update={"voice": config.voice.model_copy(update={"followup_s": 0})})
    env = Env(home, off)
    v, h = voice_for(env, "open brave", "open the calculator")
    ran = runs(v)
    v.converse("hotkey")
    assert ran == [{"Brave"}]
    assert h.phrases == ["open the calculator"]  # never listened for it


def test_a_redirected_answer_runs_as_the_next_turn(home: Path, config: Config, quiet: None):
    env = Env(home, config)
    v, _h = voice_for(env, "open brat", "no, I wanted zoom")
    runs(v)
    handled: list[str] = []
    v.handle = lambda text: handled.append(text) or "I couldn't find zoom."  # type: ignore[method-assign]
    v.converse("hotkey")
    assert handled == ["open zoom"]
    log = history(env.config.runtime_dir)
    assert [x["trigger"] for x in log] == ["hotkey", "followup"]
    assert log[1]["heard"] == "open zoom"


def test_the_conversation_is_rebuilt_from_history_and_ends_on_thanks(tmp_path):
    from datetime import UTC, datetime, timedelta

    from personal_os.voice.main import record_history

    now = datetime.now(UTC)
    at = lambda m: (now - timedelta(minutes=m)).isoformat()  # noqa: E731
    record_history(tmp_path, {"at": at(50), "heard": "old talk", "reply": "x", "route": "agent"})
    record_history(tmp_path, {"at": at(40), "heard": "thanks", "route": dialogue.ENDED})
    record_history(
        tmp_path,
        {
            "at": at(3),
            "heard": "which tabs are open?",
            "reply": "Docs, Music and Mail.",
            "route": "agent",
        },
    )
    record_history(tmp_path, {"at": at(2), "heard": "", "route": "nothing heard", "quiet": True})
    record_history(
        tmp_path, {"at": at(1), "heard": "group those", "reply": "", "route": "agent", "error": "x"}
    )
    c = dialogue.Conversation.from_history(tmp_path)
    text = c.for_planner()
    assert text.splitlines() == ["You: which tabs are open?", "Donna: Docs, Music and Mail."]
