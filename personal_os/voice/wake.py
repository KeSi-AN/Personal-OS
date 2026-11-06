"""The wake word, as in JARVIS (github.com/Namish-Dev/JARVIS): Vosk listens, always, for "Donna".

Vosk (Kaldi, `vosk-model-small-en-us-0.15`, ~68 MB) transcribes everything it hears, on this
laptop, and the assistant wakes when a wake word (`voice.wake_words`, default "donna") is in
it as a word: "Donna", "hey Donna", "hello Donna", "Donna, open Brave"; not "Madonna" or
"gonna". While a reply is playing, "stop talking", "stop speaking", "be quiet" or "shut up"
stop it, and the wake word stops it and listens, as in JARVIS.

Differences from JARVIS, both about the hand-over:

- It reacts to Vosk's partial results, not only the final one, so it wakes while you're still
  talking, and the audio from the start of that sentence is handed to the recorder: "Donna,
  open Brave" in one breath keeps "open Brave".
- It listens on the audio engine's echo-cancelled microphone (voice/engine.py) when there is
  one, so a video, music or Donna herself saying "Donna" doesn't wake it.

While a request is being recorded the detector rests (the recorder has the audio).
Nothing is stored; Vosk's text is used only to look for the wake word.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from personal_os.core.logging import get_logger

_log = get_logger("voice.wake")

VOSK_DIR = "vosk-model-small-en-us-0.15"
VOSK_URL = f"https://alphacephei.com/vosk/models/{VOSK_DIR}.zip"
STOP_PHRASES = ("stop talking", "stop speaking", "be quiet", "shut up")
DEBOUNCE_S = 2.0  # JARVIS waits 2 s after a wake before listening for another
RING_S = 8.0  # the sentence so far, handed over on wake


def model_path(data_dir: Path) -> Path:
    return data_dir / "models" / VOSK_DIR


def available(data_dir: Path) -> bool:
    d = model_path(data_dir)
    return (d / "am" / "final.mdl").exists() and (d / "conf" / "model.conf").exists()


def wake_pattern(words: list[str]) -> re.Pattern[str]:
    """The wake words as whole words: "donna" matches "hey donna", not "madonna"."""
    alts = "|".join(re.escape(w.strip().lower()) for w in words if w.strip()) or "donna"
    return re.compile(rf"(?<![a-z'])(?:{alts})(?![a-z'])")


class VoskWake:
    """Listens for the wake word; `on_wake(preroll)` when it's heard, `on_stop()` for "stop
    talking" while `speaking()`. Audio comes from the audio engine, else its own mic stream."""

    def __init__(
        self,
        data_dir: Path,
        words: list[str],
        *,
        on_wake: Callable[[list[np.ndarray]], None],
        on_stop: Callable[[], None],
        busy: Callable[[], bool],
        speaking: Callable[[], bool],
        engine: Any = None,
    ) -> None:
        self.data_dir = data_dir
        self.pattern = wake_pattern(words)
        self.on_wake = on_wake
        self.on_stop = on_stop
        self.busy = busy
        self.speaking = speaking
        self.engine = engine
        self.stop = threading.Event()
        self.ring: deque[np.ndarray] = deque(maxlen=int(RING_S * 16000 / 512))
        self.last_wake = 0.0
        self.last: dict[str, Any] = {}  # what the last wake or stop heard, for `voice status`
        self.thread: threading.Thread | None = None
        self._rec: Any = None

    def start(self) -> None:
        import vosk

        vosk.SetLogLevel(-1)
        self._model = vosk.Model(str(model_path(self.data_dir)))
        self._rec = vosk.KaldiRecognizer(self._model, 16000)
        self.thread = threading.Thread(target=self._run, name="pos-wake", daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        if self.thread is not None:
            self.thread.join(2)

    # --- audio in ---------------------------------------------------------------------------

    def _run(self) -> None:
        try:
            if self.engine is not None:
                with self.engine.subscribe() as q:
                    while not self.stop.is_set():
                        try:
                            frame = q.get(timeout=0.5)
                        except queue.Empty:
                            continue
                        self.feed(frame.audio)
            else:
                self._run_own_mic()
        except Exception as exc:
            _log.warning(
                "wake word stopped",
                extra={"fields": {"error": f"{type(exc).__name__}: {exc}"[:200]}},
            )

    def _run_own_mic(self) -> None:
        import sounddevice as sd

        with sd.InputStream(samplerate=16000, channels=1, dtype="float32", blocksize=4000) as s:
            while not self.stop.is_set():
                block, _overflow = s.read(4000)
                self.feed(block[:, 0])

    # --- recognition --------------------------------------------------------------------------

    def feed(self, audio: np.ndarray) -> None:
        if self.busy():  # the recorder has the audio: start fresh afterwards
            if self.ring:
                self._rec.Reset()
                self.ring.clear()
            return
        self.ring.append(np.asarray(audio, dtype=np.float32))
        pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
        if self._rec.AcceptWaveform(pcm):
            text = json.loads(self._rec.Result()).get("text", "")
            self.heard(text)
            self.ring.clear()  # the sentence is over
        else:
            partial = json.loads(self._rec.PartialResult()).get("partial", "")
            if partial:
                self.heard(partial)

    def heard(self, text: str) -> None:
        t = text.lower().strip()
        if not t:
            return
        if self.speaking() and any(p in t for p in STOP_PHRASES):
            self.last = {"heard": t, "did": "stopped the reply"}
            _log.info("stop phrase", extra={"fields": self.last})
            self._rec.Reset()
            self.ring.clear()
            self.on_stop()
            return
        if not self.pattern.search(t):
            return
        now = time.monotonic()
        if now - self.last_wake < DEBOUNCE_S:
            return
        self.last_wake = now
        self.last = {"heard": t, "did": "woke"}
        _log.info("wake word", extra={"fields": self.last})
        preroll = list(self.ring)
        self._rec.Reset()
        self.ring.clear()
        self.on_wake(preroll)
