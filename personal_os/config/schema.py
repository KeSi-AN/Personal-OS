"""Typed configuration. The TOML file is the source of truth; this validates it."""

from __future__ import annotations

from pathlib import Path

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
    redact_title_patterns: list[str] = []
    sensitive_keywords: list[str] = []
    secret_path_globs: list[str] = []
    secret_dir_names: list[str] = []

    @field_validator("allowed_domains", "blocked_domains", "sensitive_domains")
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
    only_on_ac_power: bool = True
    min_user_idle_s: int = 120


class LanConfig(_Section):
    enabled: bool = False


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


class ActionsConfig(_Section):
    auto_allow_tiers: list[int] = [0, 1]

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
