"""Speech out (DESIGN.md §6.5): Speechify (opt-in cloud), Piper (local), Windows (built in).

Every voice produces a WAV file that's played with winsound (stdlib), so playback can be
stopped ("stop") the same way whichever voice spoke.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol

from personal_os.config.schema import VoiceConfig
from personal_os.core.logging import get_logger

_log = get_logger("voice.tts")
KEYRING_SERVICE = "personal-os"
SPEECHIFY_KEY = "speechify-api-key"
SPEECHIFY_URL = "https://api.speechify.ai/v1/audio/speech"


class TTSError(RuntimeError):
    pass


class Voice(Protocol):
    name: str

    def synthesize(self, text: str, out: Path) -> None: ...


def speakable(text: str, limit: int) -> str:
    """Text as it should be said: no citation marks, code ticks or markdown, within a limit."""
    text = re.sub(r"\s*\[\d+\]", "", text)
    text = re.sub(r"[`*_#>]", "", text)
    text = re.sub(r"https?://\S+", "a link", text)
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    return (cut[: end + 1] if end > limit // 2 else cut.rstrip() + "…").strip()


class WindowsVoice:
    """The Windows speech engine through PowerShell's System.Speech: offline, always there."""

    name = "windows"

    def synthesize(self, text: str, out: Path) -> None:
        script = (
            "Add-Type -AssemblyName System.Speech;"
            "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
            "$s.Rate=1;$s.SetOutputToWaveFile($env:POS_WAV);"
            "$s.Speak([IO.File]::ReadAllText($env:POS_TXT));$s.Dispose()"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(text)
            txt = f.name
        try:
            env = dict(os.environ, POS_WAV=str(out), POS_TXT=txt)  # text never on a command line
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                env=env,
                capture_output=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if r.returncode != 0 or not out.exists():
                raise TTSError("the Windows voice failed")
        finally:
            os.unlink(txt)


class SpeechifyVoice:
    """Opt-in cloud voice: only the sentence being spoken is sent, with your own API key."""

    name = "speechify"

    def __init__(self, cfg: VoiceConfig) -> None:
        self.cfg = cfg

    @staticmethod
    def key() -> str | None:
        try:
            import keyring

            value: str | None = keyring.get_password(KEYRING_SERVICE, SPEECHIFY_KEY)
            return value
        except Exception:
            return None

    def synthesize(self, text: str, out: Path) -> None:
        key = self.key()
        if not key:
            raise TTSError("no Speechify API key (personal-os voice speechify-key)")
        body = {
            "input": text[:2000],
            "voice_id": self.cfg.speechify_voice,
            "audio_format": "wav",
            "model": self.cfg.speechify_model,
        }
        req = urllib.request.Request(
            SPEECHIFY_URL,
            json.dumps(body).encode(),
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            raise TTSError(f"Speechify answered {exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise TTSError(f"Speechify unreachable: {exc}") from None
        audio = data.get("audio_data")
        if not audio:
            raise TTSError("Speechify returned no audio")
        out.write_bytes(base64.b64decode(audio))


class PiperVoice:
    """Local neural voice, if the piper package and a voice model are installed."""

    name = "piper"

    def __init__(self, voice_dir: Path) -> None:
        self.voice_dir = voice_dir

    def model(self) -> Path | None:
        found = sorted(self.voice_dir.glob("*.onnx")) if self.voice_dir.is_dir() else []
        return found[0] if found else None

    def synthesize(self, text: str, out: Path) -> None:
        model = self.model()
        if model is None:
            raise TTSError("no Piper voice in " + str(self.voice_dir))
        try:
            import wave

            from piper import PiperVoice as _Piper  # type: ignore[import-not-found,unused-ignore]
        except ImportError:
            raise TTSError("the piper package isn't installed") from None
        voice = _Piper.load(str(model))
        with wave.open(str(out), "wb") as w:
            voice.synthesize_wav(text, w)


def voices(cfg: VoiceConfig, data_dir: Path) -> list[Voice]:
    """The configured voice first, then the local fallbacks."""
    chain: list[Voice] = []
    if cfg.tts == "speechify":
        chain.append(SpeechifyVoice(cfg))
    if cfg.tts in ("speechify", "piper"):
        chain.append(PiperVoice(data_dir / "voice" / "piper"))
    chain.append(WindowsVoice())
    return chain


class Speaker:
    def __init__(self, cfg: VoiceConfig, data_dir: Path) -> None:
        self.cfg = cfg
        self.data_dir = data_dir
        self.last_voice: str | None = None

    def synthesize(self, text: str, out: Path) -> str:
        """Write speech for `text` to `out`; returns which voice was used."""
        text = speakable(text, self.cfg.max_spoken_chars)
        errors = []
        for voice in voices(self.cfg, self.data_dir):
            try:
                voice.synthesize(text, out)
                self.last_voice = voice.name
                return voice.name
            except TTSError as exc:
                errors.append(f"{voice.name}: {exc}")
                _log.warning("voice unavailable", extra={"fields": {"voice": voice.name}})
        raise TTSError("; ".join(errors))

    def say(self, text: str, *, wait: bool = True) -> str | None:
        if not text.strip():
            return None
        out = Path(tempfile.gettempdir()) / f"pos-say-{os.getpid()}.wav"
        used = self.synthesize(text, out)
        play(out, wait=wait)
        return used


def play(path: Path, *, wait: bool = True) -> None:
    import winsound

    flags = winsound.SND_FILENAME | (0 if wait else winsound.SND_ASYNC)
    winsound.PlaySound(str(path), flags)


def stop() -> None:
    import winsound

    winsound.PlaySound(None, winsound.SND_PURGE)
