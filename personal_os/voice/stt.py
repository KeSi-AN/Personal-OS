"""Speech in: faster-whisper on the CPU (int8), loaded on first use, freed when idle."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE = 16_000
IDLE_UNLOAD_S = 600


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

    def transcribe(self, audio: np.ndarray, *, vocabulary: str = "") -> str:
        """Mono float32 audio at 16 kHz -> text. `vocabulary` biases towards known names."""
        with self._lock:
            if self._model is None:
                self._model = self._load()
            self._last_used = time.monotonic()
            segments, _info = self._model.transcribe(
                audio.astype(np.float32),
                language="en",
                beam_size=1,
                vad_filter=True,
                initial_prompt=vocabulary or None,
                condition_on_previous_text=False,
            )
            return " ".join(s.text.strip() for s in segments).strip()

    def unload_if_idle(self) -> bool:
        with self._lock:
            if self._model is not None and time.monotonic() - self._last_used > IDLE_UNLOAD_S:
                self._model = None
                return True
        return False
