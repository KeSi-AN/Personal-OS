"""Fixes from the 20 Nov audit: own windows aren't your activity, greetings are instant,
"in 5" is 5 minutes, typed questions take typed answers, the app list never blocks a turn."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from personal_os.actions import reminder_parse
from personal_os.config.schema import Config
from personal_os.context.route import chat_reply, route
from personal_os.observers.window import is_own_window
from tests.test_actions import Env
from tests.test_phase7 import voice_for


def test_personal_os_windows_arent_your_activity():
    me = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(5)  # personal_os.island"]
    )
    try:
        assert is_own_window(me.pid)  # a process started with a personal_os module
        assert not is_own_window(os.getppid()) or "personal_os" in " ".join(
            __import__("psutil").Process(os.getppid()).cmdline()
        )
        assert not is_own_window(None) and not is_own_window(0)
    finally:
        me.kill()


@pytest.mark.parametrize("said", ["hi donna", "wadup", "what's up", "yo donna", "how are you",
                                  "hey", "how's it going"])  # fmt: skip
def test_greetings_are_answered_at_once(said: str):
    """They went to the agent: 60-78 s, and once a reply with "[list of overdue items]"."""
    assert route(said) == "chat" and chat_reply(said).endswith("What can I do for you?")


def test_questions_still_go_on():
    assert route("what did I do yesterday") == "ask"
    assert route("hi, open brave") != "chat"


@pytest.mark.parametrize(
    ("said", "text", "minutes"),
    [
        ("remind me in 5 to check claude", "check claude", 5),
        ("remind me in 10 to stretch", "stretch", 10),
    ],
)
def test_a_bare_number_after_in_is_minutes(said: str, text: str, minutes: int):
    r = reminder_parse.parse(said)
    assert r is not None and (r.text, r.minutes, r.missing) == (text, minutes, None)


def test_a_number_in_the_message_stays_in_it():
    r = reminder_parse.parse("remind me to read chapter 5")
    assert r is not None and r.text == "read chapter 5" and r.missing == "when"


def test_a_typed_turns_question_takes_the_next_typed_text(home: Path, config: Config):
    """Typed "yes" became a new request and the question counted as declined (19 Nov)."""
    v, _h = voice_for(Env(home, config))
    v.turn_trigger = "typed"
    v.turn = {"id": "t"}
    v.say = lambda _t: None  # type: ignore[method-assign]
    got: list[str] = []
    t = threading.Thread(target=lambda: got.append(v.ask_aloud("Hold you to it?")))
    t.start()
    deadline = time.monotonic() + 3
    while not v.awaiting_typed.is_set() and time.monotonic() < deadline:
        time.sleep(0.02)
    v.from_popup({"text": "yes"})
    t.join(3)
    assert got == ["yes"] and v.triggers.empty()  # an answer, not a new request


def test_a_typed_question_gives_up_quietly(home: Path, config: Config):
    v, _h = voice_for(Env(home, config))
    v.turn_trigger, v.turn = "typed", {"id": "t"}
    v.say = lambda _t: None  # type: ignore[method-assign]
    assert v.typed_answer_to("Sure?", timeout_s=0.3) == ""
    assert not v.awaiting_typed.is_set()


def test_open_app_never_waits_on_the_windows_scan(home: Path, config: Config, monkeypatch):
    """ "close brave" waited 82 s on a stalled Start-menu scan (17 Nov)."""
    from personal_os.actions import apps
    from personal_os.actions.tools import desktop

    def stalled(*_a: object, **_k: object) -> list[apps.InstalledApp]:
        raise AssertionError("a turn must never run the scan")

    monkeypatch.setattr(apps, "installed", stalled)
    monkeypatch.setattr(apps, "installed_now", lambda *_a: [apps.InstalledApp("Brave", "Brave")])
    found = desktop.installed_app("brave", Env(home, config).config)
    assert found is not None and found.name == "Brave"


def test_a_stalled_scan_keeps_the_last_list(tmp_path: Path, monkeypatch):
    from personal_os.actions import apps

    apps.runtime.write_json_atomic(
        tmp_path / apps.CACHE, {"at": 0, "apps": [{"name": "Brave", "app_id": "b"}]}
    )
    monkeypatch.setattr(apps, "read_installed", lambda: [])  # stalled: nothing back
    monkeypatch.setattr(apps, "_memo", None)
    assert [a.name for a in apps._installed(tmp_path)] == ["Brave"]  # not an empty list
