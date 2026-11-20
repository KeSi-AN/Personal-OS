"""Snoozing work mode's nudge: "snooze", "5 more minutes", or "take 5" right after one."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from personal_os.config.schema import Config
from personal_os.core import nudge
from tests.conftest import T0
from tests.test_actions import Env


@pytest.mark.parametrize(
    ("said", "minutes"),
    [("snooze", 5), ("snooze 10", 10), ("snooze it for ten minutes", 10),
     ("give me 5 more minutes", 5), ("just five more", 5), ("not now", 5),
     ("okay fine, snooze please", 5), ("20 more minutes donna", 20)],
)  # fmt: skip
def test_snooze_phrases(said: str, minutes: int):
    assert nudge.parse(said) == minutes


@pytest.mark.parametrize("said", ["snooze that reminder", "take 5", "remind me later",
                                  "play five more songs"])  # fmt: skip
def test_not_a_snooze(said: str):
    assert nudge.parse(said) is None


def test_a_snooze_holds_nudges_then_lets_one_through(tmp_path: Path):
    assert nudge.due(tmp_path, T0)
    nudge.said(tmp_path, T0)
    assert not nudge.due(tmp_path, T0 + timedelta(minutes=10))  # 30 minutes apart
    assert nudge.recent(tmp_path, T0 + timedelta(minutes=2))
    nudge.snooze(tmp_path, 5, T0 + timedelta(minutes=2))
    assert not nudge.due(tmp_path, T0 + timedelta(minutes=6))
    assert nudge.due(tmp_path, T0 + timedelta(minutes=7))  # the snooze ran out: may nudge again
    nudge.said(tmp_path, T0 + timedelta(minutes=7))
    assert not nudge.due(tmp_path, T0 + timedelta(minutes=20))  # and back to 30 minutes apart
    assert not nudge.recent(tmp_path, T0 + timedelta(minutes=30))


def voice_with_clock(home: Path, config: Config) -> Any:
    from personal_os.core.clock import SimClock
    from tests.test_phase7 import voice_for

    v, _h = voice_for(Env(home, config))
    v.clock = SimClock(T0 + timedelta(minutes=1))
    return v


def test_voice_snoozes_right_after_a_nudge(home: Path, config: Config):
    v = voice_with_clock(home, config)
    rt = v.cfg.runtime_dir
    nudge.said(rt, T0)
    assert v.fast("give me 10 more minutes") == (
        "Okay, 10 more minutes. I'll nudge you again then if you're still on it."
    )
    assert not nudge.due(rt, T0 + timedelta(minutes=10))
    assert nudge.due(rt, T0 + timedelta(minutes=11))


def test_take_5_after_a_nudge_rests_and_snoozes(home: Path, config: Config):
    v = voice_with_clock(home, config)
    rt = v.cfg.runtime_dir
    nudge.said(rt, T0)
    reply = v.fast("donna take 5")
    assert reply is not None and "taking 5 minutes" in reply.lower() and v.resting()
    assert not nudge.due(rt, T0 + timedelta(minutes=5)) and nudge.due(rt, T0 + timedelta(minutes=6))


def test_snooze_without_a_nudge_is_not_taken_for_one(home: Path, config: Config):
    v = voice_with_clock(home, config)
    reply = v.fast("not now")
    assert reply is None or "more minute" not in reply


def _nudge_alert(rt: Path) -> None:
    from personal_os.interfaces import alerts

    alerts.add(rt, "nudge-0130", "Work mode", "17 min of chill in work mode.", T0)
    alerts.add(rt, "reminder-1", "Reminder", "stretch", T0)


def test_a_snooze_takes_the_nudge_off_the_island(home: Path, config: Config):
    from personal_os.interfaces import alerts

    v = voice_with_clock(home, config)
    rt = v.cfg.runtime_dir
    nudge.said(rt, T0)
    _nudge_alert(rt)
    v.fast("snooze")
    left = [a["id"] for a in alerts.pending(rt, T0)]
    assert left == ["reminder-1"]  # the nudge is gone; a reminder isn't touched


def test_chill_mode_takes_the_nudge_off_the_island(tmp_path: Path):
    from personal_os.core import modes

    _nudge_alert(tmp_path)
    assert nudge.showing(tmp_path)
    modes.set_mode(tmp_path, "chill", by="voice", now=T0)
    assert not nudge.showing(tmp_path)


def test_back_at_work_takes_the_nudge_off_the_island(home: Path, config: Config, monkeypatch: Any):
    from personal_os.context.now import Window
    from tests.test_modes import _nudge_world

    env, _fake, shown, tick = _nudge_world(
        home, config, monkeypatch, ([Window("Code", "main.py", front=True, exe="Code.exe")], [])
    )
    _nudge_alert(env.config.runtime_dir)
    tick()
    assert shown == [] and not nudge.showing(env.config.runtime_dir)


@pytest.mark.parametrize(
    ("said", "minutes"),
    [("gimme 5", 5), ("give me ten minutes", 10), ("i'll get back in 5", 5),
     ("I'll be back to work in ten minutes", 10), ("back in 5", 5)],
)  # fmt: skip
def test_what_you_said_on_21_nov_snoozes(said: str, minutes: int):
    """ "gimme 5" became a to-do and "i'll get back in 5" a commitment (21 Nov)."""
    assert nudge.parse(said) == minutes


@pytest.mark.parametrize("said", ["i'll call mum in 5", "give me the file", "take 5"])
def test_other_requests_with_numbers_arent_snoozes(said: str):
    assert nudge.parse(said) is None


def test_ill_get_back_in_5_after_a_nudge_is_a_snooze_not_a_commitment(home: Path, config: Config):
    v = voice_with_clock(home, config)
    nudge.said(v.cfg.runtime_dir, T0)
    assert v.fast("i'll get back in 5") == (
        "Okay, 5 more minutes. I'll nudge you again then if you're still on it."
    )


@pytest.mark.parametrize("said", ["summarise today", "recap today", "sum up yesterday",
                                  "give me a summary of today"])  # fmt: skip
def test_a_summary_of_your_day_is_answered_from_your_history(said: str):
    from personal_os.context.route import about_my_past

    assert about_my_past(said)


@pytest.mark.parametrize("said", ["summarize this article", "summarise the meeting notes"])
def test_other_summaries_are_not_history(said: str):
    from personal_os.context.route import about_my_past

    assert not about_my_past(said)
