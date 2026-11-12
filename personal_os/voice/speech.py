"""Streaming speech out (spec §4, §9): Piper, sentence by sentence, stoppable at once.

Piper turns each sentence into audio as it goes, so speaking starts when the first sentence is
ready rather than after the whole reply. Audio is played in 20 ms blocks; between blocks the
interrupt is checked, so new speech or a key press stops playback within a block or two
(well under 200 ms). The level being played is kept for the barge-in echo gate.

If no Piper voice is installed, the Windows voice (voice/tts.py) speaks instead.
"""

from __future__ import annotations

import math
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from personal_os.config.schema import VoiceConfig
from personal_os.core.logging import get_logger
from personal_os.voice import tts

_log = get_logger("voice.speech")
BLOCK_MS = 20


class Player:
    """Plays float32 mono audio; `interrupt` stops it between 20 ms blocks."""

    def __init__(self) -> None:
        self.level = 0.0  # RMS of the block now playing (0 when silent)
        self.playing = threading.Event()
        self.started_at = 0.0

    def play(self, audio: np.ndarray, rate: int, interrupt: threading.Event | None = None) -> bool:
        """True if it played to the end, False if interrupted."""
        import sounddevice as sd

        block = max(1, rate * BLOCK_MS // 1000)
        data = np.asarray(audio, dtype=np.float32).reshape(-1)
        self.playing.set()
        self.started_at = time.monotonic()
        try:
            with sd.OutputStream(
                samplerate=rate, channels=1, dtype="float32", latency="low", blocksize=block
            ) as out:
                for i in range(0, len(data), block):
                    if interrupt is not None and interrupt.is_set():
                        out.abort()
                        return False
                    chunk = data[i : i + block]
                    self.level = float(np.sqrt(np.mean(np.square(chunk)))) if chunk.size else 0.0
                    out.write(chunk.reshape(-1, 1))
            return True
        finally:
            self.level = 0.0
            self.playing.clear()


def tone(freqs: tuple[float, ...], ms: int, rate: int = 22_050, volume: float = 0.35) -> np.ndarray:
    n = rate * ms // 1000
    fade = max(1, rate // 200)
    t = np.arange(n) / rate
    wave = sum(np.sin(2 * math.pi * f * t) for f in freqs) / len(freqs)
    env = np.minimum(1.0, np.minimum(np.arange(n) / fade, (n - np.arange(n)) / fade))
    return (wave * env * volume).astype(np.float32)


# "Listening" (one rising note) and "got it" (two quick notes): heard before any work starts.
EARCONS = {
    "listen": np.concatenate([tone((660,), 70), tone((880,), 90)]),
    "ack": np.concatenate([tone((988,), 55), np.zeros(600, np.float32), tone((1319,), 70)]),
    "error": tone((330,), 160),
    "more": tone((784,), 60, volume=0.18),  # quieter: "anything else?" after a reply
}


class Speaker:
    """say(): Piper if a voice is installed, else the Windows voice. Stoppable either way."""

    def __init__(self, cfg: VoiceConfig, data_dir: Path) -> None:
        self.cfg = cfg
        self.data_dir = data_dir
        self.player = Player()
        self._piper: Any = None
        self._piper_failed = False
        self._lock = threading.Lock()
        self.fallback = tts.Speaker(cfg, data_dir)
        self.last_voice: str | None = None

    def voice_file(self) -> Path | None:
        folder = self.data_dir / "voice" / "piper"
        wanted = folder / f"{self.cfg.piper_voice}.onnx"
        if wanted.exists():
            return wanted
        found = sorted(folder.glob("*.onnx")) if folder.is_dir() else []
        return found[0] if found else None

    def piper(self) -> Any:
        with self._lock:
            if self._piper is None and not self._piper_failed and self.cfg.tts != "windows":
                path = self.voice_file()
                try:
                    if path is None:
                        raise FileNotFoundError("no Piper voice")
                    from piper import PiperVoice

                    self._piper = PiperVoice.load(str(path))
                except Exception as exc:
                    self._piper_failed = True
                    _log.warning("piper unavailable", extra={"fields": {"why": str(exc)[:120]}})
            return self._piper

    def warm(self) -> None:
        self.piper()

    def voice_key(self) -> str:
        """Names the voice speech is made in: a recording made in another voice or at another
        speed would sound wrong next to the replies."""
        voice = self.piper()
        path = self.voice_file()
        if voice is not None and path is not None:
            return f"piper-{path.stem}-{self.cfg.piper_length_scale:g}"
        return f"windows-{self.cfg.windows_voice}"

    def render(self, text: str) -> tuple[np.ndarray, int]:
        """`text` as float32 mono audio and its sample rate, in the voice replies use (never a
        cloud voice: a recording is kept, and nothing is sent anywhere to make it)."""
        voice = self.piper()
        if voice is not None:
            from piper import SynthesisConfig

            config = SynthesisConfig(length_scale=self.cfg.piper_length_scale)
            chunks = list(voice.synthesize(text, syn_config=config))
            if chunks:
                audio = np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32)
                return audio, int(chunks[0].sample_rate)
        import tempfile
        import wave

        out = Path(tempfile.gettempdir()) / f"pos-render-{threading.get_ident()}.wav"
        try:
            tts.WindowsVoice(self.cfg.windows_voice).synthesize(text, out)
            with wave.open(str(out), "rb") as w:
                rate, frames = w.getframerate(), w.readframes(w.getnframes())
                width, channels = w.getsampwidth(), w.getnchannels()
        finally:
            out.unlink(missing_ok=True)
        if width != 2:
            raise tts.TTSError("the Windows voice made audio that isn't 16-bit")
        pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        return pcm.reshape(-1, channels).mean(axis=1).astype(np.float32), rate

    def earcon(self, kind: str) -> None:
        try:
            self.player.play(EARCONS[kind], 22_050)
        except Exception:
            tts.beep()

    def say(self, text: str, interrupt: threading.Event | None = None) -> bool:
        """Speak `text`; False if interrupted. Starts at the first sentence."""
        text = tts.speakable(text, self.cfg.max_spoken_chars)
        if not text:
            return True
        voice = self.piper()
        if voice is None:
            self.fallback.say(text, interrupt=interrupt)
            self.last_voice = self.fallback.last_voice
            return not (interrupt is not None and interrupt.is_set())
        from piper import SynthesisConfig

        config = SynthesisConfig(length_scale=self.cfg.piper_length_scale)
        self.last_voice = "piper"
        for chunk in voice.synthesize(text, syn_config=config):  # one sentence at a time
            if interrupt is not None and interrupt.is_set():
                return False
            if not self.player.play(chunk.audio_float_array, chunk.sample_rate, interrupt):
                return False
        return True
