"""The wake word as Whisper writes it (13 Nov: every wake was dropped as "Don't know")."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from personal_os.config.schema import Config
from personal_os.voice.main import _without_wake, mentions_wake
from tests.test_actions import Env
from tests.test_phase7 import voice_for

# What the voice history recorded after the wake word engine had heard "donna".
SEEN = ["Don't know", "Don't know.", "Done now", "Done up.", "Dunno", "Donner."]


@pytest.mark.parametrize("heard", SEEN)
def test_whispers_spellings_of_donna_count_as_the_wake_word(heard: str):
    assert mentions_wake(heard, ["donna"])
    assert _without_wake(heard) == ""  # just the name: she then listens for the request


@pytest.mark.parametrize(
    ("heard", "wanted"),
    [
        ("Don't know, open Brave.", "open Brave"),
        ("Dunno, play some music", "play some music"),
        ("Hey Donna what time is it", "what time is it"),
        ("Donna, I'm done for today", "I'm done for today"),  # the real name is cut, not "done"
        ("I'm done, huh", "huh"),  # "Hi Donna" run together, as the very first words
    ],
)
def test_the_request_after_it_is_kept(heard: str, wanted: str):
    assert mentions_wake(heard, ["donna"]) and _without_wake(heard) == wanted


@pytest.mark.parametrize("heard", ["of", "I really do not know what to do", "open the door"])
def test_other_speech_still_isnt_the_wake_word(heard: str):
    assert not mentions_wake(heard, ["donna"])


def test_a_wake_turn_tells_whisper_her_name(home: Path, config: Config):
    v, _h = voice_for(Env(home, config))
    got: list[dict[str, Any]] = []

    class Model:
        def transcribe(self, audio: Any, **kw: Any) -> str:
            got.append(kw)
            return "Donna, open Brave."

    v.stt = v.stt_fast = Model()  # type: ignore[assignment]
    assert v.transcribe(object(), wake=True) == "Donna, open Brave."
    assert got[-1] == {"vocabulary": "Hey Donna,"}
    v.transcribe(object())
    assert got[-1] == {}  # other turns are transcribed as before


def test_the_wake_listen_passes_it_on(home: Path, config: Config, monkeypatch: pytest.MonkeyPatch):
    from personal_os.voice import audio

    v, _h = voice_for(Env(home, config))
    del v.listen  # the real listen, not voice_for's stand-in
    v.engine = None
    monkeypatch.setattr(audio, "record_utterance", lambda *_a, **_k: [0.0] * 1600)
    seen: list[bool] = []
    v.transcribe = lambda _a, wake=False, accurate=False: seen.append(wake) or "Donna."  # type: ignore[method-assign,assignment,func-returns-value]
    v.listen([], wake=True)
    v.listen()
    assert seen == [True, False]
