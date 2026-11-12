"""Speaking an answer while it's written: the reply stream, the agent's hand-off, voice."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from personal_os.actions import agent
from personal_os.config.schema import Config
from personal_os.llm.fake import FakeProvider
from personal_os.llm.replystream import ReplyStream
from personal_os.voice.main import SentenceSpeaker, spoken, unsaid
from tests.test_actions import Env
from tests.test_agent import Script, answer, use


def read(step: dict[str, Any] | str, *, gate: str | None = "answer", size: int = 1) -> list[str]:
    text = step if isinstance(step, str) else json.dumps(step)
    got: list[str] = []
    rs = ReplyStream(got.append, gate=gate)
    for i in range(0, len(text), size):
        rs.feed(text[i : i + size])
    rs.finish()
    return got


# --- the stream -------------------------------------------------------------------------------


@pytest.mark.parametrize("size", [1, 3, 7, 1000])
def test_sentences_come_out_as_they_finish_whatever_the_chunks(size: int):
    step = answer("Tokyo is the capital of Japan. About fourteen million people live there!")
    assert read(step, size=size) == [
        "Tokyo is the capital of Japan.",
        "About fourteen million people live there!",
    ]


def test_the_first_sentence_is_handed_on_before_the_json_is_done():
    got: list[str] = []
    rs = ReplyStream(got.append)
    rs.feed('{"decide": "answer", "calls": [], "reply": "Tokyo is the capital of Japan. Ab')
    assert got == ["Tokyo is the capital of Japan."]


def test_nothing_is_said_from_a_step_that_uses_tools():
    step = {"decide": "use_tools", "calls": [{"tool": "now", "args": {}}], "reply": "Checking."}
    assert read(step) == []


def test_a_reply_inside_the_calls_is_not_the_answer():
    raw = (
        '{"decide": "answer", "calls": [{"tool": "create_note", "args": {"title": "reply", '
        '"text": "reply: not this one."}}], "reply": "Noted it for you, all done."}'
    )
    assert read(raw) == ["Noted it for you, all done."]


def test_escapes_are_decoded_even_when_split():
    step = answer('She said "hi" to Renée today. Then she left the room.')
    raw = json.dumps(step)  # ensure_ascii: é is written as é
    assert "\\u00e9" in raw
    assert read(raw, size=2) == ['She said "hi" to Renée today.', "Then she left the room."]


def test_a_short_opener_joins_the_next_sentence():
    assert read(answer("Sure. Here's a quick idea for dinner tonight.")) == [
        "Sure. Here's a quick idea for dinner tonight."
    ]


def test_a_list_stops_the_stream_after_its_heading():
    got = read(answer("Three tabs are open:\n- Docs\n- Mail\n- Music"))
    assert got == ["Three tabs are open:"]


def test_an_answer_only_reply_needs_no_decision():
    assert read({"reply": "It's 5 pm in Tokyo right now.", "details": ""}, gate=None) == [
        "It's 5 pm in Tokyo right now."
    ]


# --- the agent hands sentences on -------------------------------------------------------------


def test_the_agent_hands_on_the_answer_sentence_by_sentence(home: Path, config: Config):
    env = Env(home, config)
    said: list[str] = []
    script = Script(answer("A thread runs inside a process. It shares that process's memory."))
    r = agent.respond("what's a thread?", env.ex, FakeProvider(script), on_sentence=said.append)
    assert said == ["A thread runs inside a process.", "It shares that process's memory."]
    assert r.streamed == " ".join(said) and r.reply == r.streamed


def test_a_false_claim_is_never_said_early(home: Path, config: Config):
    env = Env(home, config)
    said: list[str] = []
    script = Script(answer("I've paused the music for you. Anything else I can do?"))
    r = agent.respond("pause it", env.ex, FakeProvider(script), on_sentence=said.append)
    assert said == []  # nothing paused it, so the claim is held back...
    assert "paused" not in r.reply  # ...and corrected in the reply said afterwards
    assert any("stopped speaking early" in line for line in r.trace)


def test_after_tools_the_final_answer_is_streamed(home: Path, config: Config, monkeypatch):
    from tests.test_agent import fake_now

    fake_now(monkeypatch)
    env = Env(home, config)
    said: list[str] = []
    script = Script(use(("now", {})), final="You're in VS Code, on agent.py right now.")
    script.steps.append({"decide": "answer", "calls": [], "reply": ""})  # results, no words
    agent.respond("what am I doing?", env.ex, FakeProvider(script), on_sentence=said.append)
    assert said == ["You're in VS Code, on agent.py right now."]


# --- voice ------------------------------------------------------------------------------------


def test_what_is_left_to_say():
    reply = "First one here. Second one here. Third one here."
    assert unsaid(spoken(reply), "First one here.") == "Second one here. The rest is on screen."
    assert unsaid(spoken(reply), "First one here. Second one here.") == "The rest is on screen."
    assert unsaid("Short answer.", "Short answer.") == ""
    assert unsaid("Corrected answer.", "Something else.") == "Corrected answer."  # said in full
    assert unsaid("Nothing streamed.", "") == "Nothing streamed."


class FakeVoice:
    def __init__(self) -> None:
        self.announced = ""
        self.interrupt = threading.Event()
        self.turn_t0 = time.monotonic()
        self.said: list[str] = []
        self.shown: list[str] = []

    def show(self, state: str, **fields: Any) -> None:
        self.shown.append(fields["reply"])

    def wait_for_popup(self, reply: str) -> None:
        pass

    def say(self, text: str) -> None:
        self.said.append(text)


def test_the_speaker_says_two_sentences_in_order_and_shows_each_first():
    v = FakeVoice()
    s = SentenceSpeaker(v)  # type: ignore[arg-type]
    for line in ("One is first.", "Two is second.", "Three stays on screen."):
        s.add(line)
    s.close()
    assert v.said == ["One is first.", "Two is second."]
    assert v.shown == ["One is first.", "One is first. Two is second."]
    assert s.said == "One is first. Two is second." and s.first_s is not None


def test_the_speaker_says_nothing_once_interrupted_or_after_an_announcement():
    v = FakeVoice()
    v.interrupt.set()
    s = SentenceSpeaker(v)  # type: ignore[arg-type]
    s.add("Not said.")
    s.close()
    assert v.said == [] and s.said == ""
    v2 = FakeVoice()
    v2.announced = "Playing Circles."
    s2 = SentenceSpeaker(v2)  # type: ignore[arg-type]
    s2.add("Playing Circles by Post Malone now.")
    s2.close()
    assert v2.said == []
