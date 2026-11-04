"""The LLMProvider interface (DESIGN.md §6.2). Models are swappable behind it."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """The model answered, but not usefully (bad JSON twice, a server-side error)."""


class LLMUnavailable(LLMError):
    """The runtime isn't reachable or the model isn't installed."""


@dataclass
class Health:
    reachable: bool
    installed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    loaded: list[tuple[str, int]] = field(default_factory=list)  # name, bytes in VRAM
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.reachable and not self.missing


class LLMProvider(Protocol):
    chat_model: str
    embed_model: str

    def chat_json(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        think: bool = False,
        validate: Callable[[T], None] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> T:
        """Structured output: the reply must parse as `schema`, then pass `validate` (which
        raises ValueError with a reason). One retry shows the model what was wrong."""
        ...

    def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
        """Unit-length vectors. `query=True` marks a question (asymmetric retrieval)."""
        ...

    def health(self) -> Health: ...


def normalize(vec: Sequence[float], dims: int | None = None) -> list[float]:
    v = list(vec[:dims] if dims else vec)
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]
