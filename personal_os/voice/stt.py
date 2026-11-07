"""Speech in: Whisper on the CPU (faster-whisper, int8), loaded on first use, freed when idle.

As in JARVIS (github.com/Namish-Dev/JARVIS): the multilingual Whisper "small" model, told the
language is English, with no prompt. The multilingual model was trained on far more accents
than the English-only ones (small.en, base.en), which heard "breathe" or "a break" for "Brave".
faster-whisper runs the same model weights as JARVIS's openai-whisper, without PyTorch and
several times faster.

A prompt of the names that exist here (`voice.stt_hints`) is off by default, as in JARVIS;
a biased second decode is still used for a near miss on a name ("Did you mean ...?").
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE = 16_000
IDLE_UNLOAD_S = 600


MAX_GAIN = 30.0


def normalize(audio: np.ndarray) -> np.ndarray:
    """Quiet recordings (laptop mic arrays) raised to a normal level, at most 30x, so the
    speech filter and the model don't take quiet speech for silence. Loud audio is unchanged."""
    out = audio.astype(np.float32)
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if 0 < peak < 0.5:
        out = out * min(0.9 / peak, MAX_GAIN)
    return out


# Whisper invents text for noise ("Neeeeeee", "Thank you for watching"): segments it was
# unsure held speech at all, or that are one sound repeated, are dropped.
NO_SPEECH = 0.6
UNSURE_LOGPROB = -1.0
REPEATED = 2.4  # gzip compression ratio: real speech is well under it


def heard(segments: Any) -> str:
    kept = []
    for s in segments:
        no_speech = float(getattr(s, "no_speech_prob", 0.0) or 0.0)
        logprob = float(getattr(s, "avg_logprob", 0.0) or 0.0)
        ratio = float(getattr(s, "compression_ratio", 0.0) or 0.0)
        if (no_speech > NO_SPEECH and logprob < UNSURE_LOGPROB) or ratio > REPEATED:
            continue
        kept.append(s.text.strip())
    text = " ".join(kept).strip()
    return re.sub(r"(\w)\1{3,}", r"\1\1", text)  # "Neeeeee" -> "Nee"


def model_dir(data_dir: Path, name: str) -> Path:
    return data_dir / "models" / f"whisper-{name}"


class Transcriber:
    def __init__(self, data_dir: Path, name: str = "small.en") -> None:
        self.path = model_dir(data_dir, name)
        self.name = name
        self._model: Any = None
        self._lock = threading.Lock()
        self._last_used = 0.0

    @property
    def available(self) -> bool:
        return (self.path / "model.bin").exists()

    def _load(self) -> Any:
        from faster_whisper import WhisperModel

        if not self.available:
            raise FileNotFoundError(
                f"speech model not found in {self.path} (personal-os voice setup downloads it)"
            )
        return WhisperModel(str(self.path), device="cpu", compute_type="int8", cpu_threads=6)

    def transcribe(
        self,
        audio: np.ndarray,
        *,
        vocabulary: str = "",
        hotwords: str = "",
        beam_size: int = 5,
    ) -> str:
        """Mono float32 audio at 16 kHz -> text. `vocabulary` (a prompt) and `hotwords`, when
        given, bias recognition towards names that exist here."""
        with self._lock:
            if self._model is None:
                self._model = self._load()
            self._last_used = time.monotonic()
            segments, _info = self._model.transcribe(
                normalize(audio),
                language="en",
                beam_size=beam_size,
                vad_filter=False,  # the turn was already cut by Silero VAD
                initial_prompt=vocabulary or None,
                hotwords=hotwords or None,
                condition_on_previous_text=False,
                without_timestamps=True,
            )
            return heard(segments)

    def recheck(self, audio: np.ndarray, phrases: list[str]) -> str:
        """The same audio decoded again, biased hard towards a few candidate phrases
        ("open Brave", "open Brat..."): for a near miss, before asking the user."""
        prompt = " ".join(f"{p}." for p in phrases)
        return self.transcribe(audio, vocabulary=prompt, hotwords=", ".join(phrases), beam_size=5)

    def warm(self) -> None:
        """Load the model now, so the first command isn't slow (no-op without the model)."""
        if not self.available:
            return
        with self._lock:
            if self._model is None:
                self._model = self._load()
            self._last_used = time.monotonic()

    def unload_if_idle(self) -> bool:
        with self._lock:
            if self._model is not None and time.monotonic() - self._last_used > IDLE_UNLOAD_S:
                self._model = None
                return True
        return False


def make_transcriber(data_dir: Path, name: str) -> Transcriber:
    """The recogniser for a configured model name (a Whisper size: "small", "small.en"...)."""
    return Transcriber(data_dir, name)
