"""The always-on audio engine: one microphone, echo-cancelled, shared by everything that listens.

    mic (16 kHz) ──┐
                   ├─> WebRTC echo canceller (livekit's AudioProcessingModule) ─> clean frames
    loopback ──────┘        reference: what the speakers are playing, from any app

The reference is Windows' loopback of the default output device, so the assistant's own voice
*and* a video or call playing on the PC are taken out of the mic: what remains is you. That
lets the wake word listen all the time and lets you talk over a reply (barge-in) without the
assistant hearing itself (seen once: a YouTube video kept "interrupting" replies).

Speakers add delay (an HDMI TV: about a quarter second), which the canceller can't bridge on
its own, so the engine measures it by cross-correlating the mic with the reference while
something plays, and feeds the reference that much later.

Consumers `subscribe()` to 32 ms frames (512 samples, Silero VAD's frame) of clean audio, each
with the raw mic level, so "is this you or leftover echo?" can be answered per frame. Without
a loopback device or the canceller the engine still runs, on the raw mic, and says so.
Audio is never written to disk.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np

from personal_os.core.logging import get_logger

_log = get_logger("voice.engine")

RATE = 16_000
BLOCK = 160  # 10 ms: the echo canceller's frame
FRAME = 512  # 32 ms: what consumers get (Silero VAD's frame)
HISTORY_S = 4.0  # kept for delay estimation
MAX_DELAY_S = 0.8
DEFAULT_DELAY_S = 0.24  # measured on an HDMI TV; re-measured live
ESTIMATE_EVERY_S = 1.0
MATCH = 0.5  # envelope correlation needed to trust a delay estimate
SUBSCRIBER_FRAMES = 400  # ~13 s buffered per subscriber before old frames are dropped


@dataclass
class Frame:
    """32 ms of clean audio, with the raw (before echo cancelling) level for comparison."""

    audio: np.ndarray  # float32, 512 samples, 16 kHz
    raw_rms: float
    ref_rms: float  # how loud the speakers were (0 if nothing playing or no reference)
    t: float  # monotonic time of the frame's end


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if x.size else 0.0


class DelayEstimator:
    """Speaker-to-mic delay from envelope cross-correlation, smoothed over a few estimates."""

    def __init__(self, initial_s: float = DEFAULT_DELAY_S) -> None:
        self.delay_s = initial_s
        # Seeded with the starting value, so one odd estimate (the first second of a reply,
        # mostly silence) can't move the delay; several agreeing ones do.
        self.recent: deque[float] = deque([initial_s] * 3, maxlen=7)

    def update(self, mic: np.ndarray, ref: np.ndarray) -> float | None:
        """mic and ref: the same stretch of time, ref undelayed. Returns a new estimate or
        None if the reference was too quiet or the match too weak to trust."""
        if len(mic) != len(ref):
            return None
        # Enough of the window must be playback, or the match is mostly silence.
        hop10 = len(ref) // BLOCK
        loud = np.sqrt(np.mean(np.square(ref[: hop10 * BLOCK].reshape(hop10, BLOCK)), axis=1))
        if hop10 == 0 or float(np.mean(loud > 0.005)) < 0.4:
            return None
        hop = 80  # 5 ms envelope resolution
        n = len(mic) // hop
        env_m = np.abs(mic[: n * hop]).reshape(n, hop).mean(axis=1)
        env_r = np.abs(ref[: n * hop]).reshape(n, hop).mean(axis=1)
        env_m, env_r = env_m - env_m.mean(), env_r - env_r.mean()
        max_lag = int(MAX_DELAY_S * RATE / hop)
        best, best_lag = 0.0, 0
        norm = float(np.linalg.norm(env_m) * np.linalg.norm(env_r)) or 1.0
        for lag in range(0, min(max_lag, n - 10)):
            c = float(np.dot(env_m[lag:], env_r[: n - lag])) / norm
            if c > best:
                best, best_lag = c, lag
        if best < MATCH:
            return None
        self.recent.append(best_lag * hop / RATE)
        self.delay_s = float(np.median(self.recent))
        return self.delay_s


class _Reference:
    """Loopback audio, resampled to 16 kHz, by arrival time; silence where nothing arrived
    (Windows sends no loopback data at all while nothing plays)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.chunks: deque[tuple[float, np.ndarray]] = deque()
        self.keep_s = HISTORY_S + MAX_DELAY_S + 1.0

    def add(self, t: float, samples: np.ndarray) -> None:
        with self.lock:
            self.chunks.append((t, samples))
            while self.chunks and t - self.chunks[0][0] > self.keep_s:
                self.chunks.popleft()

    def at(self, t_end: float, n: int) -> np.ndarray:
        """The n samples that ended at time t_end (zeros where there was nothing)."""
        out = np.zeros(n, dtype=np.float32)
        t_start = t_end - n / RATE
        with self.lock:
            for ct, samples in self.chunks:
                c_start = ct - len(samples) / RATE
                if ct <= t_start or c_start >= t_end:
                    continue
                a = round((c_start - t_start) * RATE)
                src_lo, dst_lo = max(0, -a), max(0, a)
                m = min(len(samples) - src_lo, n - dst_lo)
                if m > 0:
                    out[dst_lo : dst_lo + m] = samples[src_lo : src_lo + m]
        return out


class AudioEngine:
    def __init__(
        self, *, echo_cancel: bool = True, initial_delay_s: float = DEFAULT_DELAY_S
    ) -> None:
        self.want_aec = echo_cancel
        self.delay = DelayEstimator(initial_delay_s)
        self.ref = _Reference()
        self.subscribers: list[queue.Queue[Frame]] = []
        self.sub_lock = threading.Lock()
        self.running = threading.Event()
        self.aec_active = False
        self.loopback_name = ""
        self.error = ""
        self._mic_q: queue.Queue[tuple[float, np.ndarray]] = queue.Queue(maxsize=500)
        self._streams: list[Any] = []
        self._pa: Any = None
        self._apm: Any = None
        self._thread: threading.Thread | None = None
        self._hist_mic: deque[np.ndarray] = deque(maxlen=int(HISTORY_S * RATE / BLOCK))
        self._hist_ref: deque[np.ndarray] = deque(maxlen=int(HISTORY_S * RATE / BLOCK))

    # --- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        import sounddevice as sd

        def mic_cb(indata: np.ndarray, _f: int, _t: object, _s: object) -> None:
            with contextlib.suppress(queue.Full):
                self._mic_q.put_nowait((time.monotonic(), indata[:, 0].copy()))

        mic = sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=BLOCK,
                             callback=mic_cb)  # fmt: skip
        mic.start()
        self._streams.append(mic)
        if self.want_aec:
            try:
                self._open_loopback()
                from livekit import rtc

                self._apm = rtc.AudioProcessingModule(
                    echo_cancellation=True, noise_suppression=True, high_pass_filter=True,
                    auto_gain_control=False,
                )  # fmt: skip
                self.aec_active = True
            except Exception as exc:  # no loopback device, or no canceller: raw mic
                self.error = f"echo cancelling off: {type(exc).__name__}: {exc}"[:200]
                _log.warning("echo cancelling unavailable", extra={"fields": {"why": self.error}})
        self.running.set()
        self._thread = threading.Thread(target=self._process, name="pos-audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running.clear()
        for s in self._streams:
            with contextlib.suppress(Exception):
                if hasattr(s, "stop_stream"):
                    s.stop_stream()
                    s.close()
                else:
                    s.stop()
                    s.close()
        self._streams.clear()
        if self._pa is not None:
            with contextlib.suppress(Exception):
                self._pa.terminate()
        if self._thread is not None:
            self._thread.join(2)

    def _open_loopback(self) -> None:
        import pyaudiowpatch as pyaudio
        from scipy.signal import resample_poly

        self._pa = pa = pyaudio.PyAudio()
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        loop = next(
            (d for d in pa.get_loopback_device_info_generator() if out["name"] in d["name"]), None
        )
        if loop is None:
            raise RuntimeError(f"no loopback device for {out['name']}")
        rate, ch = int(loop["defaultSampleRate"]), int(loop["maxInputChannels"])
        self.loopback_name = str(loop["name"])

        def cb(data: bytes, frames: int, _info: object, _status: int) -> tuple[None, int]:
            x = np.frombuffer(data, dtype=np.float32).reshape(-1, ch).mean(axis=1)
            y = resample_poly(x, RATE, rate).astype(np.float32) if rate != RATE else x
            self.ref.add(time.monotonic(), y)
            return (None, pyaudio.paContinue)

        stream = pa.open(format=pyaudio.paFloat32, channels=ch, rate=rate, input=True,
                         input_device_index=loop["index"], frames_per_buffer=rate // 100,
                         stream_callback=cb)  # fmt: skip
        stream.start_stream()
        self._streams.append(stream)

    # --- consumers ----------------------------------------------------------------------------

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[queue.Queue[Frame]]:
        q: queue.Queue[Frame] = queue.Queue(maxsize=SUBSCRIBER_FRAMES)
        with self.sub_lock:
            self.subscribers.append(q)
        try:
            yield q
        finally:
            with self.sub_lock:
                self.subscribers.remove(q)

    def _publish(self, frame: Frame) -> None:
        with self.sub_lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(frame)
            except queue.Full:  # a slow consumer loses its oldest audio, never blocks the mic
                with contextlib.suppress(queue.Empty):
                    q.get_nowait()
                with contextlib.suppress(queue.Full):
                    q.put_nowait(frame)

    # --- processing ---------------------------------------------------------------------------

    def _process(self) -> None:
        pending: list[np.ndarray] = []
        raw_pending: list[np.ndarray] = []
        ref_levels: list[float] = []
        last_estimate = time.monotonic()
        while self.running.is_set():
            try:
                t, block = self._mic_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if len(block) != BLOCK:
                continue
            ref_now = self.ref.at(t, BLOCK)  # undelayed, for estimating the delay
            ref_block = self.ref.at(t - self.delay.delay_s, BLOCK) if self.aec_active else ref_now
            clean = self._cancel(block, ref_block) if self.aec_active else block
            self._hist_mic.append(block)
            self._hist_ref.append(ref_now)
            pending.append(clean)
            raw_pending.append(block)
            ref_levels.append(rms(ref_block))
            if self.aec_active and t - last_estimate >= ESTIMATE_EVERY_S:
                last_estimate = t
                self.delay.update(np.concatenate(self._hist_mic), np.concatenate(self._hist_ref))
            have = sum(len(p) for p in pending)
            if have >= FRAME:
                audio = np.concatenate(pending)
                raw = np.concatenate(raw_pending)
                self._publish(Frame(audio[:FRAME], rms(raw[:FRAME]), max(ref_levels), t))
                rest, raw_rest = audio[FRAME:], raw[FRAME:]
                pending = [rest] if rest.size else []
                raw_pending = [raw_rest] if raw_rest.size else []
                ref_levels = []

    def _cancel(self, mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
        from livekit import rtc

        r = (np.clip(ref, -1, 1) * 32767).astype(np.int16)
        m = (np.clip(mic, -1, 1) * 32767).astype(np.int16)
        rf = rtc.AudioFrame(r.tobytes(), RATE, 1, BLOCK)
        mf = rtc.AudioFrame(m.tobytes(), RATE, 1, BLOCK)
        self._apm.process_reverse_stream(rf)
        self._apm.process_stream(mf)
        return np.frombuffer(bytes(mf.data), dtype=np.int16).astype(np.float32) / 32767.0

    def output_changed(self) -> bool:
        """Has the default output device changed (TV to headphones) since the engine started?
        Then the loopback reference is the wrong device and the engine should be restarted."""
        if not self.aec_active:
            return False
        try:
            import pyaudiowpatch as pyaudio

            pa = pyaudio.PyAudio()
            try:
                wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
                name = str(pa.get_device_info_by_index(wasapi["defaultOutputDevice"])["name"])
            finally:
                pa.terminate()
        except Exception:
            return False
        return name not in self.loopback_name

    def describe(self) -> dict[str, Any]:
        return {
            "echo_cancel": self.aec_active,
            "loopback": self.loopback_name,
            "speaker_delay_ms": round(self.delay.delay_s * 1000),
            **({"error": self.error} if self.error else {}),
        }
