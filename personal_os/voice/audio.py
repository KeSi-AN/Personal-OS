"""Recording one utterance from the microphone: wait for speech, stop at a pause.

Energy-based endpointing (no extra dependency): the noise floor is measured first, speech is
anything well above it, and a short silence after speech ends the utterance. Audio stays in
memory and is never written to disk.
"""

from __future__ import annotations

import queue
from collections.abc import Callable

import numpy as np

from personal_os.voice.stt import SAMPLE_RATE

BLOCK = 1600  # 100 ms
CALIBRATE_BLOCKS = 3
START_TIMEOUT_S = 6.0
SILENCE_END_S = 0.9
MAX_S = 15.0


def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(block), dtype=np.float64)))


def endpoint(blocks: list[np.ndarray], noise: float) -> tuple[int, int] | None:
    """(first, last) block index of the utterance in `blocks`, or None if nobody spoke."""
    threshold = max(noise * 3.0, 0.01)
    voiced = [i for i, b in enumerate(blocks) if rms(b) > threshold]
    if not voiced:
        return None
    return max(0, voiced[0] - 3), min(len(blocks) - 1, voiced[-1] + 3)


def record_utterance(
    should_stop: Callable[[], bool] = lambda: False, *, device: int | None = None
) -> np.ndarray | None:
    """Record until a pause after speech; None if no speech started within START_TIMEOUT_S."""
    import sounddevice as sd

    q: queue.Queue[np.ndarray] = queue.Queue()

    def callback(indata: np.ndarray, _frames: int, _time: object, _status: object) -> None:
        q.put(indata[:, 0].copy())

    blocks: list[np.ndarray] = []
    noise = 0.0
    started = False
    quiet = 0
    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=BLOCK,
        callback=callback,
        device=device,
    ):
        while not should_stop():
            try:
                block = q.get(timeout=1.0)
            except queue.Empty:
                continue
            blocks.append(block)
            n = len(blocks)
            if n <= CALIBRATE_BLOCKS:
                noise = max(noise, rms(block))
                continue
            loud = rms(block) > max(noise * 3.0, 0.01)
            if loud:
                started, quiet = True, 0
            elif started:
                quiet += 1
            if started and quiet * BLOCK / SAMPLE_RATE >= SILENCE_END_S:
                break
            if not started and n * BLOCK / SAMPLE_RATE >= START_TIMEOUT_S:
                return None
            if n * BLOCK / SAMPLE_RATE >= MAX_S:
                break
    span = endpoint(blocks[CALIBRATE_BLOCKS:], noise)
    if span is None:
        return None
    a, b = span
    return np.concatenate(blocks[CALIBRATE_BLOCKS + a : CALIBRATE_BLOCKS + b + 1])
