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
    assert v.fast("give me 10 more minutes") == "Okay, 10 more minutes. I'll check in after that."
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
