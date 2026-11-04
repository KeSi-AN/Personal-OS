"""Ollama over its local HTTP API, with the standard library's client (no SDK, no httpx).

Measured on the target laptop (GTX 1650, 4 GB): Ollama's own layer estimate leaves qwen3:4b
half on the CPU (7.5 tokens/s); asking for every layer on the GPU gives about 45 tokens/s at
an 8k context. If that load fails (another app holding VRAM), the call is retried with
Ollama's own choice and that choice is kept for the rest of the process.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import ValidationError

from personal_os.config.schema import LLMConfig
from personal_os.llm.provider import Health, LLMError, LLMUnavailable, T, normalize

QUERY_INSTRUCTION = (
    "Instruct: Given a question about the user's past computer activity, retrieve the records "
    "that answer it\nQuery: "
)
EMBED_CTX = 1024
EMBED_MAX_CHARS = 3000
_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class OllamaProvider:
    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.chat_model = cfg.fast_model
        self.embed_model = cfg.embedding_model
        self._gpu_layers: int | None = None if cfg.gpu_layers < 0 else cfg.gpu_layers
        self.last_call: dict[str, Any] = {}  # attempts, tokens and time of the last chat_json

    # --- transport ---------------------------------------------------------------------------

    def _request(self, path: str, body: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.cfg.base_url + path, data, {"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result: dict[str, Any] = json.load(resp)
                return result
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc).get("error", "")
            except (ValueError, OSError):
                detail = ""
            if exc.code == 404:
                raise LLMUnavailable(detail or f"{path}: not found") from None
            raise LLMError(f"ollama {exc.code}: {detail}"[:300]) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise LLMUnavailable(f"ollama not reachable at {self.cfg.base_url}: {exc}") from None

    def _options(self, *, gpu: bool, num_ctx: int, temperature: float) -> dict[str, Any]:
        opts: dict[str, Any] = {"num_ctx": num_ctx, "temperature": temperature}
        if not gpu:
            opts["num_gpu"] = 0
        elif self._gpu_layers is not None:
            opts["num_gpu"] = self._gpu_layers
        return opts

    def _with_gpu_fallback(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._request(path, body, self.cfg.timeout_s)
        except LLMUnavailable:
            raise
        except LLMError:
            if body["options"].get("num_gpu") in (None, 0) or self._gpu_layers is None:
                raise
            self._gpu_layers = None  # e.g. out of VRAM: let Ollama choose from now on
            body["options"].pop("num_gpu")
            return self._request(path, body, self.cfg.timeout_s)

    # --- chat ----------------------------------------------------------------------------------

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
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        last_error = ""
        stats: dict[str, Any] = {"attempts": 0, "prompt_tokens": 0, "output_tokens": 0}
        stats |= {"seconds": 0.0, "error": None}
        self.last_call = stats
        for _attempt in range(2):
            stats["attempts"] += 1
            body = {
                "model": self.chat_model,
                "messages": messages,
                "stream": False,
                "think": think,
                "format": schema.model_json_schema(),
                "keep_alive": self.cfg.keep_alive,
                "options": self._options(
                    gpu=True,
                    num_ctx=self.cfg.num_ctx,
                    temperature=temperature if temperature is not None else (0.6 if think else 0.2),
                )
                # A cap on output: a small model can loop forever (seen at temperature 0).
                | {"num_predict": max_tokens or (4096 if think else 1024)},
            }
            reply = self._with_gpu_fallback("/api/chat", body)
            stats["prompt_tokens"] += int(reply.get("prompt_eval_count") or 0)
            stats["output_tokens"] += int(reply.get("eval_count") or 0)
            stats["seconds"] += int(reply.get("total_duration") or 0) / 1e9
            content = _THINK_RE.sub("", reply.get("message", {}).get("content", "")).strip()
            try:
                obj = schema.model_validate_json(content)
                if validate is not None:
                    validate(obj)
                return obj
            except ValueError as exc:  # pydantic's ValidationError is a ValueError
                last_error = stats["error"] = _short_error(exc)
                messages += [
                    {"role": "assistant", "content": content[:4000]},
                    {
                        "role": "user",
                        "content": f"That reply was not valid: {last_error}. "
                        "Reply again with corrected JSON only.",
                    },
                ]
        raise LLMError(f"invalid structured output after a retry: {last_error}")

    # --- embeddings ----------------------------------------------------------------------------

    def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
        if not texts:
            return []
        inputs = [(QUERY_INSTRUCTION + t if query else t)[:EMBED_MAX_CHARS] for t in texts]
        body = {
            "model": self.embed_model,
            "input": inputs,
            "truncate": True,
            "keep_alive": self.cfg.keep_alive,
            # A question is embedded on the CPU (0.5 s) so the chat model keeps the GPU.
            "options": self._options(gpu=not query, num_ctx=EMBED_CTX, temperature=0),
        }
        reply = self._with_gpu_fallback("/api/embed", body)
        vectors = reply.get("embeddings") or []
        if len(vectors) != len(inputs):
            raise LLMError("embedding count mismatch")
        return [normalize(v, self.cfg.embedding_dims) for v in vectors]

    # --- health --------------------------------------------------------------------------------

    def health(self) -> Health:
        try:
            tags = self._request("/api/tags", None, 5)
            ps = self._request("/api/ps", None, 5)
        except LLMError as exc:
            return Health(reachable=False, error=str(exc))
        installed = sorted(m.get("name", "") for m in tags.get("models", []))
        wanted = {self.chat_model, self.embed_model}
        missing = sorted(m for m in wanted if not _installed(m, installed))
        loaded = [(m.get("name", ""), int(m.get("size_vram") or 0)) for m in ps.get("models", [])]
        return Health(True, installed, missing, loaded)


def _installed(model: str, installed: list[str]) -> bool:
    return model in installed or (":" not in model and f"{model}:latest" in installed)


def _short_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        errs = exc.errors()[:3]
        return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'reply'}: {e['msg']}" for e in errs)
    return str(exc)[:300]
