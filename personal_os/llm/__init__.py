"""LLM access behind one interface (DESIGN.md §6.2)."""

from __future__ import annotations

from personal_os.config.schema import LLMConfig
from personal_os.llm.provider import Health, LLMError, LLMProvider, LLMUnavailable

__all__ = ["Health", "LLMError", "LLMProvider", "LLMUnavailable", "make_provider"]


def make_provider(cfg: LLMConfig) -> LLMProvider:
    from personal_os.llm.ollama import OllamaProvider

    if cfg.provider != "ollama":
        raise ValueError(f'unknown llm.provider {cfg.provider!r} (only "ollama" is built in)')
    return OllamaProvider(cfg)
