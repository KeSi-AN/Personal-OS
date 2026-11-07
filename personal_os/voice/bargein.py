"""Barge-in (spec §4, §9): speaking over a reply stops it and starts listening.

The mic stays open while a reply plays. Laptop speakers leak into the mic, and Silero rightly
calls that leaked reply "speech", so the VAD alone can't tell you from the assistant. Without
an echo canceller (Windows doesn't expose one to PortAudio), an adaptive echo gate does it:

1. For the first 400 ms of a reply the gate learns how loud the echo is relative to what's
   playing (you're assumed quiet while it starts talking).
2. After that a frame counts as you only if the VAD says speech AND the mic is clearly louder
   than the echo it expects for the current playback level.
3. About a quarter second of that (8 frames) stops playback; the frames heard so far are
   handed to the next recording, so your first words aren't lost.

With headphones there's no echo and any speech interrupts. Ctrl+Alt+Shift+V always interrupts.

Two guards keep it from talking to itself (seen on a laptop with a YouTube video playing:
the video's speech "interrupted" each reply, was answered, and so on):

- Other audio: while any other app is making sound (a video, music, a call), voice barge-in
  is off. Windows' per-app audio meters say who is playing.
- No chains: the voice loop doesn't arm barge-in for a reply to a turn that itself started
  by barge-in.

`voice.barge_in = "hotkey"` leaves only the key; "voice" turns this on.

With the audio engine's echo canceller (voice/engine.py) there's a better way, `CleanBargeIn`:
the speakers are taken out of the mic (the assistant and any other app), so what's left is you.
A frame is you when the VAD hears speech in the clean audio and the clean audio is a large
share of the raw mic (leftover echo is a few percent of it; your voice is most of it). Other
apps' sound is cancelled too, so the other-audio guard isn't needed. It only listens while a
reply is actually sounding, not in the first ~0.3 s of each sentence (the canceller is still
catching up there), and the no-chains guard applies here too (voice/main.py `say`): seen on
laptop speakers, "I'm here to help" leaked through, was heard as "I'm here", and answered.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections import deque
from collections.abc import Callable

import numpy as np

from personal_os.voice.speech import Player
from personal_os.voice.stt import SAMPLE_RATE
from personal_os.voice.vad import FRAME, StreamingVAD, auto_gain

WARMUP_S = 0.4
ECHO_MARGIN = 2.5
SPEECH_PROB = 0.7
TRIGGER_FRAMES = 8  # 256 ms
KEEP_FRAMES = 20  # ~0.6 s handed to the next recording


OTHER_AUDIO_PEAK = 0.02  # another app louder than this means "something else is playing"
OTHER_AUDIO_EVERY_S = 0.25


def other_audio_level(own_pid: int | None = None) -> float:
    """The loudest sound any other app is making right now (0 if none, or if unknown)."""
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

        comtypes.CoInitialize()
        own = own_pid if own_pid is not None else os.getpid()
        loudest = 0.0
        for s in AudioUtilities.GetAllSessions():
            if s.Process is None or s.Process.pid == own or s.State != 1:
                continue
            meter = s._ctl.QueryInterface(IAudioMeterInformation)
            loudest = max(loudest, float(meter.GetPeakValue()))
        return loudest
    except Exception:
        return 0.0


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x), dtype=np.float64))) if x.size else 0.0


class EchoGate:
    """Frame-by-frame decision, separated from the audio device for testing."""

    def __init__(self, vad: StreamingVAD, floor: float, warmup_frames: int) -> None:
        self.vad = vad
        self.floor = floor
        self.warmup = warmup_frames
        self.coupling = 0.0  # mic level / playback level, learned during warm-up
        self.seen = 0
        self.run = 0
        self.peaks: deque[float] = deque(maxlen=31)
        self.recent: deque[np.ndarray] = deque(maxlen=KEEP_FRAMES)

    def feed(self, frame: np.ndarray, out_level: float) -> bool:
        """True when it's you talking over the reply."""
        self.seen += 1
        self.recent.append(frame)
        mic = _rms(frame)
        self.peaks.append(float(np.max(np.abs(frame))) if frame.size else 0.0)
        prob = self.vad.speech_prob(frame, gain=auto_gain(max(self.peaks)))
        if self.seen <= self.warmup:
            if out_level > 0.01:
                self.coupling = max(self.coupling, mic / out_level)
            return False
        expected = self.coupling * out_level
        you = prob >= SPEECH_PROB and mic > max(self.floor, ECHO_MARGIN * expected)
        self.run = self.run + 1 if you else 0
        return self.run >= TRIGGER_FRAMES


class BargeIn:
    """Watches the mic while `player` speaks; calls `on_speech(frames)` once if you talk."""

    def __init__(
        self,
        player: Player,
        on_speech: Callable[[list[np.ndarray]], None],
        *,
        floor: float,
        other_audio: Callable[[], float] = other_audio_level,
    ) -> None:
        self.player = player
        self.on_speech = on_speech
        self.floor = floor
        self.other_audio = other_audio
        self.done = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> BargeIn:
        self.thread = threading.Thread(target=self._run, name="pos-barge", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.done.set()
        if self.thread is not None:
            self.thread.join(1.0)

    def _run(self) -> None:
        try:
            import sounddevice as sd

            gate = EchoGate(StreamingVAD(), self.floor, int(WARMUP_S * SAMPLE_RATE / FRAME))
        except Exception:
            return
        q: queue.Queue[np.ndarray] = queue.Queue()

        def callback(indata: np.ndarray, _f: int, _t: object, _s: object) -> None:
            q.put(indata[:, 0].copy())

        started = time.monotonic()
        checked, busy = 0.0, False
        try:
            with sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=FRAME,
                callback=callback,
            ):  # fmt: skip
                while not self.done.is_set():
                    try:
                        frame = q.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    now = time.monotonic()
                    if now - checked >= OTHER_AUDIO_EVERY_S:
                        checked, busy = now, self.other_audio() > OTHER_AUDIO_PEAK
                    if busy:
                        gate.run = 0  # a video or call is playing: its voices aren't you
                        continue
                    if not self.player.playing.is_set() and now - started > 1.0:
                        continue  # between sentences: nothing to talk over
                    if gate.feed(frame, self.player.level):
                        self.on_speech(list(gate.recent))
                        return
        except Exception:
            return


YOU_SHARE = 0.4  # clean / raw level above this: most of what the mic hears is not the speakers
CLEAN_SPEECH_PROB = 0.6
CLEAN_TRIGGER_FRAMES = 10  # ~320 ms: a word, not a click or a burst of leftover echo
SETTLE_FRAMES = 9  # ~290 ms at the start of each sentence, while the canceller catches up


class YouGate:
    """Frame by frame on echo-cancelled audio: is that you talking? (Testable without audio.)"""

    def __init__(self, vad: StreamingVAD, floor: float) -> None:
        self.vad = vad
        self.floor = floor
        self.run = 0
        self.peaks: deque[float] = deque(maxlen=31)
        self.recent: deque[np.ndarray] = deque(maxlen=KEEP_FRAMES)

    def feed(self, clean: np.ndarray, raw_rms: float) -> bool:
        self.recent.append(clean)
        self.peaks.append(float(np.max(np.abs(clean))) if clean.size else 0.0)
        prob = self.vad.speech_prob(clean, gain=auto_gain(max(self.peaks)))
        level = _rms(clean)
        you = (
            prob >= CLEAN_SPEECH_PROB
            and level >= self.floor / 2
            and level >= YOU_SHARE * max(raw_rms, 1e-9)
        )
        self.run = self.run + 1 if you else 0
        return self.run >= CLEAN_TRIGGER_FRAMES


class CleanBargeIn:
    """Barge-in on the audio engine's echo-cancelled frames: talk over a reply to stop it."""

    def __init__(
        self,
        engine: object,
        on_speech: Callable[[list[np.ndarray]], None],
        *,
        floor: float,
        playing: Callable[[], bool] | None = None,
    ) -> None:
        """`playing`: is a reply sounding right now? Only then is there anything to talk
        over; between sentences and after the reply, nothing is taken as an interruption."""
        self.engine = engine
        self.on_speech = on_speech
        self.floor = floor
        self.playing = playing or (lambda: True)
        self.done = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self) -> CleanBargeIn:
        self.thread = threading.Thread(target=self._run, name="pos-barge", daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.done.set()
        if self.thread is not None:
            self.thread.join(1.0)

    def _run(self) -> None:
        try:
            gate = YouGate(StreamingVAD(), self.floor)
        except Exception:
            return
        was_playing, settle = False, 0
        with self.engine.subscribe() as q:  # type: ignore[attr-defined]
            while not self.done.is_set():
                try:
                    frame = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if not self.playing():
                    gate.run, was_playing = 0, False
                    continue
                if not was_playing:  # a sentence starts: let the canceller settle first
                    was_playing, settle = True, SETTLE_FRAMES
                hit = gate.feed(frame.audio, frame.raw_rms)
                if settle:
                    settle -= 1
                    gate.run = 0
                    continue
                if hit:
                    self.on_speech(list(gate.recent))
                    return
