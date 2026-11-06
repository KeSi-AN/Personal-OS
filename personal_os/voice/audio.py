"""Recording one utterance from the microphone: wait for speech, stop at a pause.

Speech is detected with Silero VAD (voice/vad.py) on 32 ms frames, after raising quiet mics to
a normal level. The turn ends after `silence_ms` without speech (600 ms by default; the voice
loop listens a little longer when what was said so far is clearly unfinished, "open ..."). The
energy-based detector below stays as a fallback if the VAD model can't load. Audio stays in
memory and is never written to disk.
"""

from __future__ import annotations

import queue
from collections import deque
from collections.abc import Callable, Iterable

import numpy as np

from personal_os.voice.stt import SAMPLE_RATE
from personal_os.voice.vad import FRAME, FRAME_MS, StreamingVAD, auto_gain

BLOCK = 1600  # 100 ms, for the energy fallback
CALIBRATE_BLOCKS = 3
START_TIMEOUT_S = 6.0
SILENCE_END_S = 1.4  # energy fallback: a pause between words mustn't end the recording
SILENCE_MS = 600  # VAD: quiet this long after speech ends the turn
MAX_S = 15.0
PREROLL_MS = 300  # kept from just before speech starts, so the first word isn't clipped
SPEECH_PROB = 0.5
MIN_SPEECH_MS = 96  # three frames: a click or a cough isn't a turn
# Quietest level that counts as speech, whatever the noise. Laptop mic arrays can record
# normal speech around 0.003-0.01 RMS, so a higher floor makes them "hear nothing".
MIC_FLOOR = 0.002


def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(block), dtype=np.float64)))


def threshold(noise: float, floor: float = MIC_FLOOR) -> float:
    return max(noise * 3.0, floor)


def endpoint(
    blocks: list[np.ndarray], noise: float, floor: float = MIC_FLOOR
) -> tuple[int, int] | None:
    """(first, last) block index of the utterance in `blocks`, or None if nobody spoke."""
    level = threshold(noise, floor)
    voiced = [i for i, b in enumerate(blocks) if rms(b) > level]
    if not voiced:
        return None
    return max(0, voiced[0] - 3), min(len(blocks) - 1, voiced[-1] + 3)


class Endpointer:
    """Frame by frame: has speech started, and has the turn ended? (No audio device here, so
    it's testable with synthetic frames.)"""

    def __init__(
        self,
        vad: StreamingVAD,
        *,
        floor: float = MIC_FLOOR,
        silence_ms: int = SILENCE_MS,
        start_timeout_s: float = START_TIMEOUT_S,
        max_s: float = MAX_S,
        started: bool = False,
    ) -> None:
        self.vad = vad
        self.floor = floor
        self.silence_frames = max(1, silence_ms // FRAME_MS)
        self.start_frames = int(start_timeout_s * 1000 / FRAME_MS)
        self.max_frames = int(max_s * 1000 / FRAME_MS)
        self.min_speech = max(1, MIN_SPEECH_MS // FRAME_MS)
        self.preroll: deque[np.ndarray] = deque(maxlen=max(1, PREROLL_MS // FRAME_MS))
        self.frames: list[np.ndarray] = []
        self.peaks: deque[float] = deque(maxlen=31)  # about a second
        self.started = started
        self.voiced = self.min_speech if started else 0
        self.quiet = 0
        self.seen = 0
        self.probs: list[float] = []

    def is_speech(self, frame: np.ndarray) -> bool:
        self.peaks.append(float(np.max(np.abs(frame))) if frame.size else 0.0)
        prob = self.vad.speech_prob(frame, gain=auto_gain(max(self.peaks)))
        self.probs.append(prob)
        return prob >= SPEECH_PROB and rms(frame) >= self.floor / 2

    def feed(self, frame: np.ndarray) -> str:
        """'waiting' (no speech yet), 'speaking', 'done' (turn over), or 'timeout'."""
        self.seen += 1
        speech = self.is_speech(frame)
        if not self.started:
            self.preroll.append(frame)
            self.voiced = self.voiced + 1 if speech else 0
            if self.voiced >= self.min_speech:
                self.started = True
                self.frames.extend(self.preroll)
                return "speaking"
            return "timeout" if self.seen >= self.start_frames else "waiting"
        self.frames.append(frame)
        self.quiet = 0 if speech else self.quiet + 1
        if self.quiet >= self.silence_frames or len(self.frames) >= self.max_frames:
            return "done"
        return "speaking"

    def audio(self) -> np.ndarray | None:
        if not self.started or not self.frames:
            return None
        keep = len(self.frames) - max(0, self.quiet - 3)  # trailing silence trimmed
        return np.concatenate(self.frames[:keep])


_vad: StreamingVAD | None = None


def shared_vad() -> StreamingVAD | None:
    """One VAD model per process (loading takes a moment); None if it can't load."""
    global _vad
    if _vad is None:
        try:
            _vad = StreamingVAD()
        except Exception:
            return None
    return _vad


def record_utterance(
    should_stop: Callable[[], bool] = lambda: False,
    *,
    device: int | None = None,
    floor: float = MIC_FLOOR,
    levels: list[float] | None = None,
    silence_ms: int = SILENCE_MS,
    start_timeout_s: float = START_TIMEOUT_S,
    preroll: Iterable[np.ndarray] = (),
) -> np.ndarray | None:
    """Record until a pause after speech; None if no speech started in time.

    `preroll`: frames already heard (speech that interrupted a reply), so recording starts
    mid-sentence without losing the start. `levels`, if given, collects each frame's RMS.
    """
    vad = shared_vad()
    if vad is None:
        return _record_energy(should_stop, device=device, floor=floor, levels=levels)
    import sounddevice as sd

    vad.reset()
    first = list(preroll)
    ep = Endpointer(
        vad,
        floor=floor,
        silence_ms=silence_ms,
        start_timeout_s=start_timeout_s,
        started=bool(first),
    )
    ep.frames.extend(first)
    q: queue.Queue[np.ndarray] = queue.Queue()

    def callback(indata: np.ndarray, _frames: int, _time: object, _status: object) -> None:
        q.put(indata[:, 0].copy())

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        blocksize=FRAME,
        callback=callback,
        device=device,
    ):
        while not should_stop():
            try:
                frame = q.get(timeout=1.0)
            except queue.Empty:
                continue
            if levels is not None:
                levels.append(rms(frame))
            state = ep.feed(frame)
            if state in ("done", "timeout"):
                break
    return ep.audio()


def record_from_engine(
    engine: object,
    should_stop: Callable[[], bool] = lambda: False,
    *,
    floor: float = MIC_FLOOR,
    silence_ms: int = SILENCE_MS,
    start_timeout_s: float = START_TIMEOUT_S,
    preroll: Iterable[np.ndarray] = (),
    levels: list[float] | None = None,
) -> np.ndarray | None:
    """record_utterance on the audio engine's echo-cancelled frames (voice/engine.py): the
    speakers (a reply still fading, a video) are out of what's recorded."""
    vad = shared_vad()
    if vad is None:
        return None
    vad.reset()
    first = list(preroll)
    ep = Endpointer(
        vad, floor=floor, silence_ms=silence_ms, start_timeout_s=start_timeout_s,
        started=bool(first),
    )  # fmt: skip
    ep.frames.extend(first)
    with engine.subscribe() as q:  # type: ignore[attr-defined]
        while not should_stop():
            try:
                frame = q.get(timeout=1.0)
            except queue.Empty:
                continue
            if levels is not None:
                levels.append(rms(frame.audio))
            if ep.feed(frame.audio) in ("done", "timeout"):
                break
    return ep.audio()


def _record_energy(
    should_stop: Callable[[], bool],
    *,
    device: int | None,
    floor: float,
    levels: list[float] | None,
) -> np.ndarray | None:
    """The fallback: energy above the measured noise floor counts as speech."""
    import sounddevice as sd

    q: queue.Queue[np.ndarray] = queue.Queue()

    def callback(indata: np.ndarray, _frames: int, _time: object, _status: object) -> None:
        q.put(indata[:, 0].copy())

    blocks: list[np.ndarray] = []
    calibration: list[float] = []
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
            if levels is not None:
                levels.append(rms(block))
            n = len(blocks)
            if n <= CALIBRATE_BLOCKS:
                # The median, so starting to talk right after the beep doesn't raise the noise.
                calibration.append(rms(block))
                noise = float(np.median(calibration))
                continue
            loud = rms(block) > threshold(noise, floor)
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
    span = endpoint(blocks[CALIBRATE_BLOCKS:], noise, floor)
    if span is None:
        return None
    a, b = span
    return np.concatenate(blocks[CALIBRATE_BLOCKS + a : CALIBRATE_BLOCKS + b + 1])
