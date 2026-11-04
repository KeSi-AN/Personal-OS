"""A deterministic stand-in for tests and the simulator: scripted replies, hashed-word vectors."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from typing import Any

from personal_os.llm.provider import Health, LLMError, LLMUnavailable, T, normalize

Responder = Callable[[str, str, type[Any]], dict[str, Any] | str]
_WORD = re.compile(r"[a-z0-9]+")


class FakeProvider:
    """`responder(system, user, schema)` returns the reply (a dict or raw text). Every call is
    recorded in `calls`. Vectors are bags of hashed words, so texts sharing words are close."""

    chat_model = "fake-chat"
    embed_model = "fake-embed"

    def __init__(
        self, responder: Responder | None = None, *, dims: int = 64, available: bool = True
    ) -> None:
        self.responder = responder
        self.dims = dims
        self.available = available
        self.calls: list[tuple[str, str]] = []
        self.embedded: list[str] = []

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
        if not self.available or self.responder is None:
            raise LLMUnavailable("fake provider has no responder")
        self.calls.append((system, user))
        reply = self.responder(system, user, schema)
        text = reply if isinstance(reply, str) else json.dumps(reply)
        try:
            obj = schema.model_validate_json(text)
            if validate is not None:
                validate(obj)
        except ValueError as exc:
            raise LLMError(f"invalid structured output: {exc}") from None
        return obj

    def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
        if not self.available:
            raise LLMUnavailable("fake provider unavailable")
        self.embedded.extend(texts)
        out = []
        for text in texts:
            vec = [0.0] * self.dims
            for word in _WORD.findall(text.lower()):
                h = int.from_bytes(hashlib.blake2b(word.encode(), digest_size=4).digest(), "big")
                vec[h % self.dims] += 1.0
            out.append(normalize(vec))
        return out

    def health(self) -> Health:
        if not self.available:
            return Health(reachable=False, error="fake provider unavailable")
        return Health(True, [self.chat_model, self.embed_model])
