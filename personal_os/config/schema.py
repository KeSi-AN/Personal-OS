"""Typed configuration. The TOML file is the source of truth; this validates it."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WindowObserverConfig(_Section):
    enabled: bool = True
    poll_interval_s: float = Field(1.0, gt=0)
    min_focus_s: float = Field(2.0, ge=0)
    title_stable_s: float = Field(3.0, ge=0)


class IdleObserverConfig(_Section):
    enabled: bool = True
    threshold_s: int = Field(300, ge=30)


class FsObserverConfig(_Section):
    enabled: bool = False
    debounce_s: float = Field(2.0, ge=0)
    burst_events_per_s: int = Field(50, ge=1)


class GitObserverConfig(_Section):
    enabled: bool = True
    safety_poll_active_s: int = Field(300, ge=10)
    safety_poll_inactive_s: int = Field(3600, ge=60)


class ToggleConfig(_Section):
    enabled: bool = False


class ObserversConfig(_Section):
    window: WindowObserverConfig = WindowObserverConfig()
    idle: IdleObserverConfig = IdleObserverConfig()
    fs: FsObserverConfig = FsObserverConfig()
    git: GitObserverConfig = GitObserverConfig()
    terminal: ToggleConfig = ToggleConfig(enabled=True)
    browser: ToggleConfig = ToggleConfig()
    editor: ToggleConfig = ToggleConfig()
    clipboard: ToggleConfig = ToggleConfig()


class PrivacyConfig(_Section):
    allowlist_mode: bool = False
    allowed_domains: list[str] = []
    min_dwell_s: float = 5
    blocked_apps: list[str] = []
    blocked_domains: list[str] = []
    blocked_domain_keywords: list[str] = []
    sensitive_domains: list[str] = []
    no_text_domains: list[str] = []
    redact_title_patterns: list[str] = []
    sensitive_keywords: list[str] = []
    secret_path_globs: list[str] = []
    secret_dir_names: list[str] = []

    @field_validator("allowed_domains", "blocked_domains", "sensitive_domains", "no_text_domains")
    @classmethod
    def _lower_domains(cls, v: list[str]) -> list[str]:
        return [d.strip().lower().lstrip(".") for d in v if d.strip()]


class RetentionConfig(_Section):
    raw_days: int = Field(30, ge=1)
    sensitive_days: int = Field(7, ge=1)
    page_text_days: int = Field(90, ge=1)
    terminal_output_days: int = Field(14, ge=1)
    clipboard_hours: int = Field(24, ge=1)


class LLMConfig(_Section):
    provider: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    fast_model: str = "qwen3:4b"
    reasoning_model: str = "qwen3:4b"
    embedding_model: str = "qwen3-embedding:0.6b"
    enabled: bool = True
    num_ctx: int = Field(8192, ge=2048)
    gpu_layers: int = Field(99, ge=-1)  # -1 = let Ollama decide (it offloads too few here)
    embedding_dims: int = Field(512, ge=32, le=1024)
    timeout_s: float = Field(180, gt=0)
    keep_alive: str = "2m"
    only_on_ac_power: bool = True
    min_user_idle_s: int = 120
    skip_when_fullscreen: bool = True
    summarize_min_active_s: int = Field(300, ge=0)

    @field_validator("base_url")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        from urllib.parse import urlsplit

        host = urlsplit(v).hostname or ""
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("llm.base_url must be on this machine (127.0.0.1 or localhost)")
        return v.rstrip("/")


class VoiceConfig(_Section):
    enabled: bool = False
    stt_model: str = "small.en"
    # Heard first (about 0.6 s on this CPU); stt_model re-hears only what the fast path can't
    # place. "" uses stt_model alone.
    stt_fast_model: str = "base.en"
    wake_word_enabled: bool = False  # needs a trained model in voice/wakewords/ (docs/VOICE.md)
    wake_threshold: float = Field(0.5, gt=0, lt=1)
    # Quietest sound level (RMS) that counts as speech; `personal-os voice mic` shows yours.
    mic_floor: float = Field(0.002, gt=0, lt=0.5)
    vad_silence_ms: int = Field(600, ge=200, le=3000)  # quiet this long ends your turn
    router_threshold: float = Field(0.85, gt=0.5, le=1.0)  # name match needed to act at once
    # hotkey: Ctrl+Alt+Shift+V stops a reply. voice: talking over it does too (best with
    # headphones; off while other apps play sound).
    barge_in: Literal["voice", "hotkey"] = "hotkey"
    piper_voice: str = "en_US-lessac-medium"
    piper_length_scale: float = Field(0.95, gt=0.5, lt=2.0)  # under 1 speaks faster
    tts: Literal["windows", "piper", "speechify"] = "piper"
    windows_voice: str = "Microsoft Zira Desktop"  # female, US English; or Hazel (UK)
    speechify_voice: str = "geffen_32"
    speechify_model: str = "simba-3.5"
    max_spoken_chars: int = Field(600, ge=100, le=2000)


class BriefingsConfig(_Section):
    enabled: bool = False
    morning_after: str = Field("09:00", pattern=r"^\d{2}:\d{2}$")
    evening_after: str = Field("18:30", pattern=r"^\d{2}:\d{2}$")
    speak: bool = True


class LanConfig(_Section):
    enabled: bool = False
    port: int = Field(47822, ge=1024, le=65535)


class ApiConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(47821, ge=1024, le=65535)
    lan: LanConfig = LanConfig()

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # The core API must never listen on the network; phone access is a separate listener.
        if v not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("api.host must be a loopback address")
        return v


class CommandSpec(_Section):
    argv: list[str] = Field(min_length=1)
    cwd: str | None = None
    timeout_s: float | None = Field(None, gt=0)


class ActionsConfig(_Section):
    auto_allow_tiers: list[int] = [0, 1]
    allowed_paths: list[Path] = []
    # When false, actions touch only allowed_paths: watched folders are captured, not acted on.
    act_in_watched_roots: bool = True
    confirm_tools: list[str] = []
    rate_per_minute: int = Field(30, ge=1)
    tier2_rate_per_minute: int = Field(10, ge=1)
    command_timeout_s: float = Field(120, gt=0)
    undo_keep_days: int = Field(7, ge=1)
    plugins_enabled: bool = False  # load tools from plugins/ in the data folder
    apps: dict[str, list[str]] = {}
    commands: dict[str, CommandSpec] = {}

    @field_validator("auto_allow_tiers")
    @classmethod
    def _max_tier_one(cls, v: list[int]) -> list[int]:
        if any(t not in (0, 1) for t in v):
            raise ValueError("only tiers 0 and 1 can be auto-allowed")
        return v


class Config(_Section):
    data_dir: Path
    watched_roots: list[Path] = []
    observers: ObserversConfig = ObserversConfig()
    privacy: PrivacyConfig = PrivacyConfig()
    retention: RetentionConfig = RetentionConfig()
    llm: LLMConfig = LLMConfig()
    api: ApiConfig = ApiConfig()
    actions: ActionsConfig = ActionsConfig()
    voice: VoiceConfig = VoiceConfig()
    briefings: BriefingsConfig = BriefingsConfig()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "personal_os.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def spool_dir(self) -> Path:
        return self.data_dir / "spool"

    @property
    def runtime_dir(self) -> Path:
        return self.data_dir / "runtime"
