"""The always-on audio engine, the Vosk wake word (as in JARVIS) and barge-in."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from personal_os.config.schema import Config
from personal_os.voice import router
from personal_os.voice.bargein import CLEAN_TRIGGER_FRAMES, YouGate
from personal_os.voice.engine import RATE, DelayEstimator, _Reference
from personal_os.voice.main import _fragment, _without_wake, history
from personal_os.voice.stt import Transcriber, make_transcriber
from personal_os.voice.wake import VoskWake, wake_pattern
from tests.test_actions import Env
from tests.test_phase7 import voice_for


def bursts(seconds: float, seed: int = 1) -> np.ndarray:
    """Speech-like audio: noise switched on and off every few hundred ms."""
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    x = rng.normal(0, 0.05, n).astype(np.float32)
    gate = (np.sin(np.arange(n) / RATE * 2 * np.pi * 1.7) > 0).astype(np.float32)
    return x * gate


def test_the_speaker_delay_is_measured_and_one_odd_estimate_does_not_move_it():
    ref = bursts(4.0)
    lag = int(0.24 * RATE)
    mic = np.concatenate([np.zeros(lag, np.float32), 0.3 * ref[:-lag]])
    mic += np.random.default_rng(2).normal(0, 0.002, len(mic)).astype(np.float32)
    est = DelayEstimator(initial_s=0.10)
    for _ in range(4):
        est.update(mic, ref)
    assert abs(est.delay_s - 0.24) < 0.02
    est.update(bursts(4.0, seed=9), ref)  # unrelated audio: no trustworthy match
    assert abs(est.delay_s - 0.24) < 0.02
    assert est.update(mic, np.zeros_like(ref)) is None  # nothing playing: no estimate


def test_the_reference_is_silence_where_nothing_played():
    ref = _Reference()
    ref.add(10.0, np.ones(160, np.float32))  # 9.99-10.00
    out = ref.at(10.005, 160)  # 9.995-10.005: half of it overlaps
    assert 70 <= int(out.sum()) <= 90 and out[-1] == 0
    assert not ref.at(20.0, 160).any()  # long after: nothing


class FakeVAD:
    def __init__(self, prob: float) -> None:
        self.prob = prob

    def speech_prob(self, frame: np.ndarray, *, gain: float = 1.0) -> float:
        return self.prob


def test_talking_over_a_reply_is_you_and_leftover_echo_is_not():
    frame = np.full(512, 0.004, np.float32)
    echo = YouGate(FakeVAD(0.95), floor=0.002)  # Silero says speech: it's the reply's echo
    assert not any(echo.feed(frame * 0.05, raw_rms=0.004) for _ in range(30))
    you = YouGate(FakeVAD(0.9), floor=0.002)
    results = [you.feed(frame, raw_rms=0.005) for _ in range(CLEAN_TRIGGER_FRAMES)]
    assert results[-1] and not any(results[:-1])
    quiet = YouGate(FakeVAD(0.9), floor=0.002)  # below the floor: not speech, however it sounds
    assert not any(quiet.feed(frame * 0.1, raw_rms=0.0004) for _ in range(20))


@pytest.mark.parametrize(
    ("heard", "woke"),
    [  # as Vosk writes them
        ("donna", True),
        ("hey donna", True),
        ("hello donna", True),
        ("donna open brave", True),
        ("hi donna what time is it", True),
        ("madonna is a singer", False),
        ("i'm gonna open brave", False),
        ("donald called", False),
    ],
)
def test_the_wake_word_is_a_whole_word(heard: str, woke: bool):
    assert bool(wake_pattern(["donna"]).search(heard)) is woke


class FakeRecognizer:
    """Stands in for Vosk's KaldiRecognizer: returns queued partial/final texts."""

    def __init__(self, texts: list[tuple[str, str]]) -> None:
        self.texts = list(texts)  # ("partial" | "final", text)
        self.resets = 0

    def AcceptWaveform(self, _pcm: bytes) -> bool:
        self.current = self.texts.pop(0) if self.texts else ("partial", "")
        return self.current[0] == "final"

    def Result(self) -> str:
        return json.dumps({"text": self.current[1]})

    def PartialResult(self) -> str:
        return json.dumps({"partial": self.current[1]})

    def Reset(self) -> None:
        self.resets += 1


def wake_with(
    texts: list[tuple[str, str]], *, speaking: bool = False
) -> tuple[VoskWake, list[Any]]:
    events: list[Any] = []
    w = VoskWake(
        Path("."), ["donna"], on_wake=lambda pre: events.append(("wake", len(pre))),
        on_stop=lambda: events.append(("stop", 0)), busy=lambda: False,
        speaking=lambda: speaking,
    )  # fmt: skip
    w._rec = FakeRecognizer(texts)
    return w, events


def test_vosk_wakes_mid_sentence_and_hands_over_the_sentence_so_far():
    w, events = wake_with(
        [("partial", "hey"), ("partial", "hey donna"), ("partial", "hey donna open")]
    )
    for _ in range(3):
        w.feed(np.zeros(512, np.float32))
    assert events == [("wake", 2)]  # once, with the two frames heard so far
    w.last_wake -= 10  # past the 2 s pause
    w._rec = FakeRecognizer([("final", "madonna is a singer")])
    w.feed(np.zeros(512, np.float32))
    assert events == [("wake", 2)]


def test_stop_talking_stops_a_reply_only_while_speaking():
    w, events = wake_with([("final", "stop talking")], speaking=True)
    w.feed(np.zeros(512, np.float32))
    assert events == [("stop", 0)]
    quiet, none = wake_with([("final", "stop talking")], speaking=False)
    quiet.feed(np.zeros(512, np.float32))
    assert none == []


def test_one_stray_word_is_not_a_request():
    assert _fragment("Not.") and _fragment("Big.") and _fragment("")
    assert not _fragment("stop") and not _fragment("Thanks!") and not _fragment("open brave")


def test_courtesies_after_a_request_do_not_spoil_the_name():
    from tests.test_voice_fastpath import INDEX

    intent = router.route("Can you open brave for me?", INDEX)
    assert intent is not None and intent.sure and intent.calls == [("open_app", {"app": "Brave"})]
    assert router.route("lock the screen please", INDEX).calls == [("lock_screen", {})]  # type: ignore[union-attr]


def test_the_request_after_the_wake_word():
    assert _without_wake("Hey Donna, open Brave.") == "open Brave"
    assert _without_wake("Donna.") == ""
    assert _without_wake("hello donna") == ""
    assert _without_wake("Open Brave") == "Open Brave"


def test_the_recogniser_is_whisper_small_as_in_jarvis(home: Path, config: Config):
    assert config.voice.stt_model == "small" and config.voice.stt_fast_model == ""
    assert not config.voice.stt_hints  # no prompt
    t = make_transcriber(home, config.voice.stt_model)
    assert isinstance(t, Transcriber) and not t.available  # nothing downloaded here


def test_saying_only_donna_then_listens_for_the_request(home: Path, config: Config):
    env = Env(home, config)
    v, _h = voice_for(env, "Donna.", "what time is it")
    v.respond("wake")
    entry = history(env.config.runtime_dir)[-1]
    assert entry["trigger"] == "wake" and entry["heard"] == "what time is it"
    assert entry["reply"].startswith("It's ")
