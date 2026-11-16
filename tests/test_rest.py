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


# --- several requests in one sentence (17 Nov) --------------------------------------------------


def test_take_5_and_remind_me_once_youre_ready_is_a_rest_that_says_im_back():
    """It became a reminder called "take 5", and you had to say "take 5" again (17 Nov)."""
    said = "take 5 and remind me once you're ready"
    assert rest.parse(said) == 5 and rest.tell_when_back(said)
    assert rest.parse("take 10 and tell me when you're back") == 10
    assert not rest.tell_when_back("take 5")


def ready_voice(home: Path, config: Config) -> Any:
    from tests.test_voice_fastpath import INDEX

    v, h = voice_for(Env(home, config))
    v.index._index = INDEX
    v.last_audio = None
    return v, h


def test_the_logged_sentence_now_rests_and_announces(home: Path, config: Config):
    v, _h = ready_voice(home, config)
    reply = v.fast("take 5 and remind me once you're ready")
    assert reply == rest.reply(5, announce=True) and v.resting()
    assert rest.announces(v.cfg.runtime_dir)


def test_several_rule_requests_in_one_sentence_are_all_done(home: Path, config: Config):
    from personal_os.actions import reminders
    from personal_os.storage.db import open_db

    v, _h = ready_voice(home, config)
    reply = v.fast("remind me in 5 minutes to stretch, then take 5")
    assert reply is not None and "stretch" in reply and "taking 5 minutes" in reply
    assert v.resting()
    conn = open_db(v.cfg.db_path)
    assert [r.text for r in reminders.pending(conn)] == ["stretch"]
    conn.close()


def test_a_reminders_own_and_is_never_split(home: Path, config: Config):
    from personal_os.actions import reminders
    from personal_os.storage.db import open_db

    v, _h = ready_voice(home, config)
    v.fast("remind me in 10 minutes to call mum and dad")
    conn = open_db(v.cfg.db_path)
    assert [r.text for r in reminders.pending(conn)] == ["call mum and dad"]
    conn.close()
    assert not v.resting()


def test_a_part_the_rules_cant_do_sends_the_whole_sentence_on(home: Path, config: Config):
    v, _h = ready_voice(home, config)
    assert v.several("take 10 and write me a poem about rain") is None
    assert not v.resting()  # nothing was done halfway


def test_the_agent_can_rest_her_alongside_other_things(home: Path, config: Config):
    from personal_os.actions.base import Origin

    env = Env(home, config)
    o = env.ex.request("take_rest", {"minutes": 5, "tell_when_back": True}, origin=Origin.PLANNED,
                       request_text="take 5 and remind me once you're ready")  # fmt: skip
    assert o.ok and rest.resting(env.config.runtime_dir)
    assert rest.announces(env.config.runtime_dir)
    assert env.ex.undo(o.action_id).ok and not rest.resting(env.config.runtime_dir)


def test_im_back_is_said_when_the_time_runs_out_not_after_a_press(home: Path, config: Config):
    v, h = ready_voice(home, config)
    rest.start(v.cfg.runtime_dir, 1, now=time.time() - 30, announce=True)
    v.watch_rest()
    assert v.was_resting and v.rest_announce and h.said == []
    rest.start(v.cfg.runtime_dir, 1, now=time.time() - 120, announce=True)  # now it's over
    v.watch_rest()
    assert h.said == [rest.BACK] and not v.was_resting
    rest.start(v.cfg.runtime_dir, 5, announce=True)
    v.watch_rest()
    v.pressed("hotkey")  # woken by you: no "I'm back"
    v.watch_rest()
    assert h.said == [rest.BACK]


def test_play_on_its_own_is_a_whole_request():
    """Typed "play" got no answer: it was taken for a cut-off sentence (17 Nov)."""
    from personal_os.context.route import incomplete

    assert not incomplete("play")
    assert incomplete("can you open the")


def test_im_back_only_when_your_words_asked_for_it(home: Path, config: Config):
    """qwen3:4b set tell_when_back for almost any rest; your words decide (17 Nov)."""
    from personal_os.actions.base import Origin

    env = Env(home, config)
    env.ex.request("take_rest", {"minutes": 30, "tell_when_back": True}, origin=Origin.PLANNED,
                   request_text="I need to focus, take 30 and pause the music")  # fmt: skip
    assert rest.resting(env.config.runtime_dir) and not rest.announces(env.config.runtime_dir)
