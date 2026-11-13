"""The popup on standby: time to read, held open while used, expandable, and a box for the
next prompt that goes through the voice process like anything you say."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from personal_os.config.schema import Config
from personal_os.interfaces import popup
from personal_os.voice import main as voice_main
from personal_os.voice.popup_view import READ_MAX_S, AutoClose, View, reading_s
from tests.test_actions import Env
from tests.test_phase7 import voice_for
from tests.test_popup import voice_process  # noqa: F401  (fixture)


def done(reply: str = "", details: str = "", tid: str = "t1") -> View:
    return View(True, "done", tid, reply=reply, details=details)


LONG = " ".join(["word"] * 110)  # about the length of "what did I work on yesterday?"


# --- time to read ----------------------------------------------------------------------------


def test_a_long_answer_stays_up_long_enough_to_read():
    assert reading_s(done("Opening Brave.")) == 0  # short: the usual few seconds
    assert 30 < reading_s(done(LONG)) < 40
    assert reading_s(done(LONG * 20)) == READ_MAX_S  # it still closes on its own, eventually
    a = AutoClose(5)
    a.step(done(LONG), 0)
    assert a.step(done(LONG), 20)[0] is False  # was closed at 5 s before
    assert a.step(done(LONG), 40)[0] is True


def test_held_while_the_mouse_is_over_it_or_you_type():
    a = AutoClose(5)
    a.step(done("Short."), 0)
    a.hold(True)
    assert a.step(done("Short."), 60)[0] is False
    a.hold(False)  # let go: the full delay again, from now
    assert a.step(done("Short."), 61)[0] is False
    assert a.step(done("Short."), 65.9)[0] is False
    assert a.step(done("Short."), 66.1)[0] is True


def test_stopping_still_closes_it_while_held():
    a = AutoClose(5)
    a.hold(True)
    assert a.step(View(True, "done", "t1", dismissed=True), 0)[0] is True


# --- the window's API ------------------------------------------------------------------------


def test_expand_shows_the_big_window_and_keeps_it_open(config: Config):
    api = popup.Api(config, demo=True)
    assert api.view()["expanded"] is False  # the demo starts with a short turn
    assert api.expand(True) is True
    data = api.view()
    assert data["expanded"] is True and data["pinned"] is True  # waits for you to read
    assert api.expand(False) is False and api.view()["expanded"] is False
    assert api.hold(True) is True and api.hold(False) is False


def test_typing_in_the_demo_sends_nothing(config: Config):
    api = popup.Api(config, demo=True)
    assert "demo" in api.ask("open brave")
    assert not (config.runtime_dir / voice_main.ASK_FILE).exists()


def test_typing_needs_voice_running(config: Config):
    api = popup.Api(config, demo=False)
    assert "isn't running" in api.ask("what's open?")
    assert not (config.runtime_dir / voice_main.ASK_FILE).exists()


def write_status(config: Config) -> None:
    import os

    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    (config.runtime_dir / voice_main.STATUS).write_text(
        json.dumps({"state": "ready", "pid": os.getpid()}), encoding="utf-8"
    )


@pytest.mark.usefixtures("voice_process")
def test_a_typed_prompt_is_passed_to_voice(config: Config):
    write_status(config)
    api = popup.Api(config, demo=False)
    assert api.ask("  what did I   work on yesterday?  ") == ""
    assert voice_main.take_ask(config.runtime_dir) == {"text": "what did I work on yesterday?"}
    assert voice_main.take_ask(config.runtime_dir) is None  # taken once
    assert api.ask("   ") == ""  # nothing typed: nothing sent
    assert voice_main.take_ask(config.runtime_dir) is None
    assert api.talk() == ""
    assert voice_main.take_ask(config.runtime_dir) == {"talk": True}


def test_an_old_request_is_dropped_not_acted_on_late(tmp_path: Path):
    voice_main.request_ask(tmp_path, text="open brave")
    data = json.loads((tmp_path / voice_main.ASK_FILE).read_text("utf-8"))
    data["at"] = time.time() - 60
    (tmp_path / voice_main.ASK_FILE).write_text(json.dumps(data), encoding="utf-8")
    assert voice_main.take_ask(tmp_path) is None
    assert not (tmp_path / voice_main.ASK_FILE).exists()


def test_a_long_prompt_is_cut_to_the_limit(config: Config, monkeypatch: pytest.MonkeyPatch):
    got: list[str] = []
    monkeypatch.setattr(voice_main, "status", lambda _rt: {"running": True})
    monkeypatch.setattr(
        voice_main, "request_ask", lambda _rt, text="", talk=False: got.append(text)
    )
    popup.Api(config, demo=False).ask("x" * 5000)
    assert len(got[0]) == popup.ASK_MAX


# --- the voice process takes it ----------------------------------------------------------------


def test_a_typed_prompt_becomes_a_turn_and_the_newest_wins(home: Path, config: Config):
    v, _h = voice_for(Env(home, config))
    v.from_popup({"text": "open brave"})
    v.from_popup({"text": "open spotify"})  # typed again before the first was taken
    assert v.triggers.get_nowait() == "typed" and v.triggers.empty()
    assert v.typed.get_nowait() == "open spotify" and v.typed.empty()


def test_typing_interrupts_what_she_is_doing(home: Path, config: Config):
    v, _h = voice_for(Env(home, config))
    v.state = "speaking"
    v.from_popup({"text": "stop that and open brave"})
    assert v.interrupt.is_set()


def test_talk_is_the_push_to_talk_key(home: Path, config: Config):
    v, _h = voice_for(Env(home, config))
    v.from_popup({"talk": True})
    assert v.triggers.get_nowait() == "hotkey"


def test_a_typed_turn_is_answered_without_opening_the_mic_after(home: Path, config: Config):
    v, _h = voice_for(Env(home, config))
    calls: list[tuple[str, Any]] = []

    def respond(trigger: str, text: str | None = None) -> bool:
        calls.append((trigger, text))
        return True  # spoken in full: a spoken turn would now listen for a follow-up

    v.respond = respond  # type: ignore[method-assign]
    v.typed.put("what's open?")
    v._converse("typed")
    assert calls == [("typed", "what's open?")]


def test_typed_prompts_get_the_quick_commands(home: Path, config: Config):
    v, _h = voice_for(Env(home, config))
    seen: list[str] = []
    v.fast = lambda text: seen.append(text) or "Opening Brave."  # type: ignore[method-assign,assignment,func-returns-value]
    v.launch_popup = lambda: None  # type: ignore[method-assign]
    v.respond("typed", text="open brave")
    assert seen == ["open brave"]
    v.respond("followup", text="no, at 5 pm instead")  # an amendment: straight to the agent
    assert seen == ["open brave"]


# --- the page ----------------------------------------------------------------------------------


def test_the_page_has_the_box_and_keeps_text_out_of_html():
    html = popup.page()
    assert 'id="askform"' in html and 'id="prompt"' in html and 'id="talk"' in html
    assert 'id="expand"' in html and "preventDefault" in html
    assert "action=" not in html  # the form sends nothing anywhere; the API carries the text
    assert "renderEarlier" in html and ".textContent = ex.reply" in html
    assert "innerHTML = ex" not in html
