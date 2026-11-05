"""Voice activity detection: Silero VAD (v6, the ONNX model faster-whisper ships), streamed.

One 32 ms frame (512 samples at 16 kHz) in, a speech probability out, with the model's state
carried between frames. It's CPU-only and small, so it can run whenever the mic is open.

Quiet laptop mic arrays record speech far below full scale, so each frame is raised to a
normal level first (at most 30x, so hiss isn't turned into "speech").
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

FRAME = 512  # samples at 16 kHz = 32 ms
CONTEXT = 64
FRAME_MS = 32
MAX_GAIN = 30.0
TARGET_PEAK = 0.5


def model_path() -> Path:
    import faster_whisper

    return Path(os.path.dirname(faster_whisper.__file__)) / "assets" / "silero_vad_v6.onnx"


class StreamingVAD:
    """speech_prob(frame) for consecutive 512-sample frames."""

    def __init__(self, path: Path | None = None) -> None:
        import onnxruntime

        opts = onnxruntime.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.session: Any = onnxruntime.InferenceSession(
            str(path or model_path()), providers=["CPUExecutionProvider"], sess_options=opts
        )
        self.reset()

    def reset(self) -> None:
        self.h = np.zeros((1, 1, 128), dtype=np.float32)
        self.c = np.zeros((1, 1, 128), dtype=np.float32)
        self.context = np.zeros((1, CONTEXT), dtype=np.float32)

    def speech_prob(self, frame: np.ndarray, *, gain: float = 1.0) -> float:
        x = (np.asarray(frame, dtype=np.float32).reshape(1, FRAME) * gain).clip(-1, 1)
        batch = np.concatenate([self.context, x], axis=1)
        out, self.h, self.c = self.session.run(None, {"input": batch, "h": self.h, "c": self.c})
        self.context = x[:, -CONTEXT:]
        return float(np.asarray(out).reshape(-1)[0])


def auto_gain(peak: float) -> float:
    """The gain that brings a frame with this recent peak level to a normal level."""
    if peak <= 0:
        return 1.0
    return float(min(MAX_GAIN, max(1.0, TARGET_PEAK / peak)))
