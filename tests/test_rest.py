"""Donna takes a break (voice/rest.py, ADR-085), and the island closes 5 s after you use it."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from personal_os.config.schema import Config
from personal_os.interfaces import island
from personal_os.interfaces import island_logic as L
from personal_os.voice import rest
from tests.test_actions import Env
from tests.test_island import inputs
from tests.test_phase7 import voice_for

# --- what counts as a rest --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "minutes"),
    [
        ("take 10", 10),
        ("Donna take 5", 5),
        ("take five", 5),
        ("take ten minutes", 10),
        ("take a 10 minute break", 10),
        ("take a break", rest.DEFAULT_MIN),
        ("take a breather", rest.DEFAULT_MIN),
        ("rest up for 20 mins", 20),
        ("go rest", rest.DEFAULT_MIN),
        ("snooze 15", 15),
        ("sleep for an hour", 60),
        ("be quiet for half an hour", 30),
        ("stop listening for 10 minutes", 10),
        ("Donna, take 2 hours", 120),
        ("take 10 please", 10),
        ("take 9 hours", rest.MAX_MIN),  # capped
    ],
)
def test_rests(said: str, minutes: int):
    assert rest.parse(said) == minutes


@pytest.mark.parametrize(
    "said",
    [
        "take a screenshot", "take me to youtube", "take notes", "what's the rest of my day",
        "be quiet", "stop listening", "sleep",  # alone, these are "stop now"
        "remind me to take a break in 10 minutes",
    ],
)  # fmt: skip
def test_not_rests(said: str):
    assert rest.parse(said) is None


def test_the_rest_file(tmp_path: Path):
    now = 1_000_000.0
    end = rest.start(tmp_path, 10, now=now)
    assert end == now + 600 and rest.resting(tmp_path, now=now + 1)
    assert rest.left_min(tmp_path, now=now + 61) == 9  # rounded up
    assert not rest.resting(tmp_path, now=now + 601)  # over: cleared
    assert not (tmp_path / rest.REST_FILE).exists()
    rest.start(tmp_path, 5, now=time.time())
    assert rest.end_rest(tmp_path) and not rest.end_rest(tmp_path)


def test_replies():
    assert rest.reply(10) == "Okay, taking 10 minutes. Press Talk or the hotkey when you need me."
    assert rest.reply(60).startswith("Okay, taking an hour.")
    assert rest.reply(120).startswith("Okay, taking 2 hours.")


# --- voice while resting ----------------------------------------------------------------------


def test_take_10_puts_listening_on_hold_until_a_press(home: Path, config: Config):
    v, _h = voice_for(Env(home, config))
    assert v.fast("Donna, take 10") == rest.reply(10)
    assert v.resting()
    v.pressed("wake")  # the wake word, heard as it was stopping: ignored
    assert v.triggers.empty() and v.resting()
    v.pressed("hotkey")  # the hotkey, or the island's Talk button: awake, and she listens
    assert not v.resting() and v.triggers.get_nowait() == "hotkey" and v.wake_dirty


def test_the_island_talk_button_wakes_her(home: Path, config: Config):
    v, _h = voice_for(Env(home, config))
    v.take_rest(20)
    v.from_popup({"talk": True})
    assert not v.resting() and v.triggers.get_nowait() == "hotkey"


def test_no_follow_up_listening_while_resting(home: Path, config: Config, monkeypatch):
    v, _h = voice_for(Env(home, config))
    turns: list[str] = []

    def respond(trigger: str, text: str | None = None) -> bool:
        turns.append(trigger)
        if trigger == "hotkey":
            v.take_rest(10)  # this turn was "take 10"
        return True  # her reply was spoken in full: normally she'd listen for more

    monkeypatch.setattr(v, "respond", respond)
    v._converse("hotkey")
    assert turns == ["hotkey"]  # no "followup" turn after it


def test_the_wake_word_stays_off_while_resting(home: Path, config: Config, monkeypatch):
    from personal_os.voice import wake

    started: list[Any] = []
    monkeypatch.setattr(wake, "available", lambda *_a: True)
    monkeypatch.setattr(wake, "VoskWake", lambda *a, **k: started.append(1) or _Wake())
    cfg = config.model_copy(
        update={"voice": config.voice.model_copy(update={"wake_word_enabled": True})}
    )
    v, _h = voice_for(Env(home, cfg))
    v.take_rest(10)
    v.sync_wake()
    assert started == [] and v.wake is None
    v.pressed("hotkey")
    v.sync_wake()
    assert started == [1] and v.wake is not None


class _Wake:
    last = None

    def start(self) -> None: ...

    def close(self) -> None: ...


# --- the island: 5 s after you use it ---------------------------------------------------------


def test_done_with_it():
    assert L.done_with(inputs(used_s=L.USED_CLOSE_S))
    assert not L.done_with(inputs(used_s=2.0))  # not yet
    assert not L.done_with(inputs(used_s=None))  # never used: the normal timings
    assert not L.done_with(inputs(used_s=9.0, hovered_s=1.0))  # the pointer is on it
    assert not L.done_with(inputs(used_s=9.0, pinned=True))
    assert not L.done_with(inputs(used_s=9.0, voice_busy=True))  # she's listening or talking


def test_after_you_use_it_it_closes_5_s_after_you_leave(config: Config):
    """It stayed open much longer after a click or typing in a box (17 Nov)."""
    isl = island.Island(config)
    now = time.monotonic()
    isl.page, isl.opened_by, isl.turn_up = "notes", "turn", True  # a turn's reply, being read
    isl.target = L.Target("open", "notes", "")
    isl.typing = True  # a box still has the keyboard
    isl.view = {"state": "done"}
    isl.used_at, isl.left_at = now - 8, now - 3  # used, then left 3 s ago
    isl._decide(now)
    assert isl.target.mode == "open"  # 3 s since leaving: still open
    isl._decide(now + 2.5)
    assert isl.target.mode != "open" and not isl.turn_up and isl.used_at is None


def test_the_pointer_on_it_keeps_it_open(config: Config):
    isl = island.Island(config)
    now = time.monotonic()
    isl.opened_by, isl.target = "hover", L.Target("open", "today", "")
    isl.view = {"state": "done"}
    isl.used_at, isl.hover_since = now - 30, now - 30
    isl._decide(now)
    assert isl.target.mode == "open"
