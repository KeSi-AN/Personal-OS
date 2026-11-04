"""The wake word ("hey Donna") with openWakeWord, from a model you train once (docs/VOICE.md).

The microphone is read in 80 ms frames and scored locally; nothing is stored or transcribed
until the wake word is heard. Off unless `voice.wake_word_enabled` and a model file is present.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import numpy as np

from personal_os.voice.stt import SAMPLE_RATE

FRAME = 1280  # 80 ms, what openWakeWord expects


def wake_models(data_dir: Path) -> list[Path]:
    folder = data_dir / "voice" / "wakewords"
    return sorted(folder.glob("*.onnx")) if folder.is_dir() else []


class WakeListener:
    def __init__(self, models: list[Path], threshold: float, on_wake: Callable[[], None]) -> None:
        self.models = models
        self.threshold = threshold
        self.on_wake = on_wake
        self.paused = threading.Event()  # set while push-to-talk or a reply is using the mic
        self.stop = threading.Event()

    def run(self) -> None:
        import sounddevice as sd
        from openwakeword.model import Model
        from openwakeword.utils import download_models

        download_models()  # openWakeWord's two small feature models, once
        model = Model(wakeword_models=[str(m) for m in self.models], inference_framework="onnx")
        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME
        ) as stream:
            while not self.stop.is_set():
                frame, _overflow = stream.read(FRAME)
                if self.paused.is_set():
                    continue
                scores = model.predict(np.asarray(frame[:, 0], dtype=np.int16))
                if max(scores.values(), default=0.0) >= self.threshold:
                    model.reset()
                    self.on_wake()
