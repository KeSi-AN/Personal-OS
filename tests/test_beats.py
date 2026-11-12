"""Beats: made once per voice, kept, played before slow lookups and never over speech."""

from __future__ import annotations

import itertools
import threading
from pathlib import Path
from typing import Any

import numpy as np

from personal_os.actions import agent
from personal_os.config.schema import Config
from personal_os.llm.fake import FakeProvider
from personal_os.voice import beats as beats_mod
from personal_os.voice.beats import PHRASES, Beats, read_wav, write_wav
from personal_os.voice.main import SLOW_TOOLS
from tests.test_actions import Env
from tests.test_agent import Script, use
from tests.test_phase7 import voice_for


class FakeRenderer:
    def __init__(self, key: str = "piper-test-0.95", fail: str = "") -> None:
        self.key = key
        self.fail = fail
        self.made: list[str] = []

    def voice_key(self) -> str:
        return self.key

    def render(self, text: str) -> tuple[np.ndarray, int]:
        if text == self.fail:
            raise RuntimeError("no voice")
        self.made.append(text)
        return np.linspace(-0.5, 0.5, 2205, dtype=np.float32), 22_050


def test_wav_round_trip(tmp_path: Path):
    audio = np.array([0.0, 0.5, -0.5, 0.25], dtype=np.float32)
    write_wav(tmp_path / "a.wav", audio, 16_000)
    back, rate = read_wav(tmp_path / "a.wav")
    assert rate == 16_000 and np.allclose(back, audio, atol=1e-4)


def test_beats_are_made_once_per_voice_and_then_only_loaded(tmp_path: Path):
    r = FakeRenderer()
    b = Beats(r, tmp_path)
    assert b.pick() is None  # nothing until they're ready: silence, as before
    assert b.prepare() == len(PHRASES) and len(r.made) == len(PHRASES)
    again = Beats(r, tmp_path)
    assert again.prepare() == len(PHRASES) and len(r.made) == len(PHRASES)  # loaded, not made
    other = FakeRenderer(key="piper-test-1.2")  # another speed sounds different: a new set
    assert Beats(other, tmp_path).prepare() == len(PHRASES) and len(other.made) == len(PHRASES)
    assert len([p for p in tmp_path.iterdir() if p.is_dir()]) == 2


def test_one_phrase_failing_doesnt_cost_the_others(tmp_path: Path):
    b = Beats(FakeRenderer(fail="On it."), tmp_path)
    assert b.prepare() == len(PHRASES) - 1 and "On it." not in b.loaded


def test_the_same_beat_is_never_played_twice_in_a_row(tmp_path: Path):
    b = Beats(FakeRenderer(), tmp_path, seed=1)
    b.prepare()
    picks = [b.pick() for _ in range(40)]
    phrases = [p[0] for p in picks if p is not None]
    assert all(a != b for a, b in itertools.pairwise(phrases))
    assert len(set(phrases)) > 3


def test_only_fixed_phrases_are_ever_recorded():
    assert all(len(p) <= 20 and p.endswith(".") for p in PHRASES)
    assert not hasattr(beats_mod, "record")  # no way to make a beat from other text


# --- the agent tells voice which tool is about to run ------------------------------------------


def test_the_agent_names_each_tool_before_it_runs(home: Path, config: Config, monkeypatch):
    from tests.test_agent import fake_now

    fake_now(monkeypatch)
    env = Env(home, config)
    seen: list[str] = []
    script = Script(use(("now", {})), final="VS Code is in front of you.")
    agent.respond("what's open?", env.ex, FakeProvider(script), before_tool=seen.append)
    assert seen == ["now"]


# --- voice ------------------------------------------------------------------------------------


class FakePlayer:
    def __init__(self) -> None:
        self.playing = threading.Event()
        self.played: list[int] = []
        self.release = threading.Event()

    def play(self, audio: Any, rate: int, interrupt: Any = None) -> bool:
        self.playing.set()
        self.release.wait(2)
        self.played.append(len(audio))
        self.playing.clear()
        return True


def ready_voice(env: Env, tmp_path: Path) -> Any:
    v, _h = voice_for(env)
    v.beats = Beats(FakeRenderer(), tmp_path / "beats", seed=3)
    v.beats.prepare()
    v.speaker.player = FakePlayer()
    return v


def test_one_beat_per_turn_on_its_own_thread(home: Path, config: Config, tmp_path: Path):
    v = ready_voice(Env(home, config), tmp_path)
    v.beat()
    assert v.beat_said in PHRASES and v.beat_thread is not None
    first = v.beat_said
    v.beat()  # a second slow lookup in the same turn: no second beat
    v.speaker.player.release.set()
    v.finish_beat()
    assert v.beat_said == first and len(v.speaker.player.played) == 1


def test_speech_waits_for_the_beat_to_finish(home: Path, config: Config, tmp_path: Path):
    from personal_os.voice.main import Voice

    v = ready_voice(Env(home, config), tmp_path)
    order: list[str] = []
    v.speaker.say = lambda text, interrupt=None: order.append(f"say:{text}") or True
    v.beat()
    threading.Timer(
        0.2, lambda: (order.append("beat done"), v.speaker.player.release.set())
    ).start()
    Voice.say(v, "Here's what you did yesterday.")  # the real say, not the test's stand-in
    assert order == ["beat done", "say:Here's what you did yesterday."]


def test_no_beat_once_something_was_said_or_when_off(home: Path, config: Config, tmp_path: Path):
    env = Env(home, config)
    v = ready_voice(env, tmp_path)
    v.streamed = "Tokyo is the capital of Japan."
    v.beat()
    assert v.beat_said == ""
    v = ready_voice(env, tmp_path)
    v.announced = "Playing Circles."
    v.beat()
    assert v.beat_said == ""
    v = ready_voice(env, tmp_path)
    v.interrupt.set()
    v.beat()
    assert v.beat_said == ""
    off = env.config.model_copy(
        update={"voice": env.config.voice.model_copy(update={"beats": False})}
    )
    v = ready_voice(env, tmp_path)
    v.cfg = off
    v.beat()
    assert v.beat_said == ""


def test_only_slow_lookups_get_a_beat():
    assert {"recall", "browser_read", "calendar_today"} <= SLOW_TOOLS
    assert not {"set_volume", "play_media", "open_app", "now", "set_reminder"} & SLOW_TOOLS
