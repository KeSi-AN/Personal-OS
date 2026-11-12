"""Beats: a few words ("One sec.", "Let me check.") said the moment a slow lookup starts, so
a question is never met with silence (docs/AGENT.md, phase 11; Roamies ships the same idea
as recordings made once in each voice).

Each phrase is made once in the voice replies use (Piper, or the Windows voice) and kept as a
WAV under `<data>/voice/beats/<voice>/`, then held in memory, so a beat plays at once: no
speech is made while you wait. Changing the voice or its speed makes a new set. Only these
fixed phrases are ever recorded; nothing you said or anything captured goes into a beat.
"""

from __future__ import annotations

import contextlib
import json
import random
import re
import threading
import wave
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from personal_os.core.logging import get_logger

_log = get_logger("voice.beats")

# Short and general: any of them must fit before recall, a search, a page or the calendar.
# Each is under a second in Piper's hfc_female voice (0.53-0.95 s): a longer one ("Sure, one
# moment." was 1.47 s) would hold up an answer that came back quickly.
PHRASES = (
    "One sec.",
    "On it.",
    "Let me check.",
    "Let me look.",
    "Let me see.",
    "Checking.",
    "Looking now.",
    "Hang on.",
    "Just a moment.",
)
INDEX = "index.json"


class Renderer(Protocol):
    def voice_key(self) -> str: ...

    def render(self, text: str) -> tuple[np.ndarray, int]: ...


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "beat"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:80]


def write_wav(path: Path, audio: np.ndarray, rate: int) -> None:
    pcm = (np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0) * 32767).astype(np.int16)
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    tmp.replace(path)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError("not 16-bit mono")
        rate, frames = w.getframerate(), w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0, rate


class Beats:
    def __init__(self, renderer: Renderer, folder: Path, *, seed: int | None = None) -> None:
        self.renderer = renderer
        self.folder = folder
        self.loaded: dict[str, tuple[np.ndarray, int]] = {}  # phrase -> audio, once ready
        self.last = ""
        self._rng = random.Random(seed)
        self._lock = threading.Lock()

    def prepare(self) -> int:
        """Make the phrases this voice doesn't have yet, then load them all. Returns how
        many are ready. Safe to call again (after the voice changes, say)."""
        with self._lock:
            key = self.renderer.voice_key()
            where = self.folder / _safe(key)
            where.mkdir(parents=True, exist_ok=True)
            index: dict[str, Any] = {}
            with contextlib.suppress(OSError, ValueError):
                index = json.loads((where / INDEX).read_text("utf-8"))
            ready: dict[str, tuple[np.ndarray, int]] = {}
            made = 0
            for phrase in PHRASES:
                path = where / f"{_slug(phrase)}.wav"
                try:
                    if not path.exists() or index.get(phrase) != path.name:
                        audio, rate = self.renderer.render(phrase)
                        write_wav(path, audio, rate)
                        index[phrase] = path.name
                        made += 1
                    ready[phrase] = read_wav(path)
                except Exception as exc:  # one phrase failing never costs the others
                    _log.warning(
                        "beat not made", extra={"fields": {"exc_type": type(exc).__name__}}
                    )
            with contextlib.suppress(OSError):
                (where / INDEX).write_text(json.dumps(index, indent=1), encoding="utf-8")
            self.loaded = ready  # swapped in whole: a reader sees the old set or the new one
            _log.info("beats ready", extra={"fields": {"count": len(ready), "made": made}})
            return len(ready)

    def pick(self) -> tuple[str, np.ndarray, int] | None:
        """A beat at random, never the one played last; None until they're ready."""
        loaded = self.loaded
        choices = [p for p in loaded if p != self.last] or list(loaded)
        if not choices:
            return None
        phrase = self._rng.choice(choices)
        self.last = phrase
        audio, rate = loaded[phrase]
        return phrase, audio, rate
