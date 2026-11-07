"""A model call can be stopped mid-answer ("stop" while Donna is thinking): it streams, and
hanging up ends it at once. A fake Ollama on localhost; nothing else is contacted."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import BaseModel

from personal_os.config.schema import LLMConfig
from personal_os.llm.ollama import OllamaProvider
from personal_os.llm.provider import LLMCancelled


class Answer(BaseModel):
    reply: str


class SlowOllama(BaseHTTPRequestHandler):
    """Streams `{"reply": "hello"}` a few characters at a time, `delay` apart."""

    delay = 0.0
    hung_up: list[bool] = []  # noqa: RUF012  # shared with the test, on purpose

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body["stream"] is True
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        text = json.dumps({"reply": "hello"})
        try:
            for i in range(0, len(text), 3):
                chunk = {"message": {"content": text[i : i + 3]}, "done": False}
                self.wfile.write((json.dumps(chunk) + "\n").encode())
                self.wfile.flush()
                time.sleep(self.delay)
            done = {"message": {"content": ""}, "done": True, "eval_count": 7}
            self.wfile.write((json.dumps(done) + "\n").encode())
        except OSError:
            SlowOllama.hung_up.append(True)

    def log_message(self, *_a: object) -> None: ...


@pytest.fixture
def ollama() -> Iterator[OllamaProvider]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    cfg = LLMConfig(base_url=f"http://127.0.0.1:{server.server_address[1]}", timeout_s=10)
    try:
        yield OllamaProvider(cfg)
    finally:
        server.shutdown()


def test_a_streamed_answer_is_the_whole_answer(ollama: OllamaProvider):
    SlowOllama.delay = 0.0
    ollama.cancel = threading.Event()
    assert ollama.chat_json("s", "u", Answer).reply == "hello"
    assert ollama.last_call["output_tokens"] == 7


def test_stopping_hangs_up_at_once(ollama: OllamaProvider):
    SlowOllama.delay = 0.3  # about two seconds for the whole answer
    ollama.cancel = threading.Event()
    threading.Timer(0.4, ollama.cancel.set).start()
    t = time.monotonic()
    with pytest.raises(LLMCancelled):
        ollama.chat_json("s", "u", Answer)
    assert time.monotonic() - t < 1.0


def test_an_already_stopped_turn_never_calls_the_model(ollama: OllamaProvider):
    ollama.cancel = threading.Event()
    ollama.cancel.set()
    with pytest.raises(LLMCancelled):
        ollama.chat_json("s", "u", Answer)
    assert ollama.last_call["attempts"] == 0
