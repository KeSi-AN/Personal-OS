"""`ask` reads what pages said: relevant passages, the fullest capture, no self-references."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from personal_os.config.schema import Config
from personal_os.context import ask as ask_mod
from personal_os.context.evidence import fetch_rows, lines_from_rows, relevant_passages
from personal_os.core.clock import to_ms
from personal_os.core.events import PageExtract
from personal_os.llm.fake import FakeProvider
from tests.test_context import command, search
from tests.test_projects import World

INTRO = "The best local speech-to-text (STT) models depend on your use case and hardware. " * 3
STT_ANSWER = (
    INTRO
    + "Top local STT options: NVIDIA Canary-Qwen 2.5B leads on accuracy. Moonshine runs on tiny"
    " edge devices. Whisper large-v3 is the all-rounder. "
    + "Unrelated footer text about privacy and cookies. "
    * 10
)
TTS_ANSWER = "The best small local TTS (text-to-speech) model right now is Kokoro-82M. " * 4


@pytest.fixture
def w(home: Path, config: Config) -> World:
    return World(home, config)


def visit(w: World, a: float, b: float, url: str, title: str, text: str) -> None:
    w.visit(a, b, url, title, extract=PageExtract(title=title, main_text=text))


def rows(w: World) -> list[Any]:
    return fetch_rows(w.conn, to_ms(w.at(-10)), to_ms(w.at(120)))


def test_relevant_passages_find_the_answer_in_the_middle():
    passage = relevant_passages(STT_ANSWER, ["local", "stt", "model"])
    assert "Canary-Qwen" in passage
    assert relevant_passages("nothing here", ["stt"]) == "nothing here"


def test_a_page_carries_what_it_says_about_the_question(w: World):
    url = "https://www.google.com/search?q=local+best+STT"
    visit(w, 0, 1, url, "local best STT - Google Search", STT_ANSWER[:300])  # still loading
    visit(w, 1, 3, url, "local best STT - Google Search", STT_ANSWER)  # the full answer
    plain = [x for x in lines_from_rows(rows(w)) if x.kind == "page"]
    focused = [
        x for x in lines_from_rows(rows(w), focus=["local", "stt", "model"]) if x.kind == "page"
    ]
    assert len(focused) == 1  # one page, visited twice
    assert "Canary-Qwen" not in plain[0].detail  # the first 200 characters: the intro only
    assert "Canary-Qwen" in focused[0].detail  # the fullest capture, the part about it


def test_own_questions_and_repeated_searches_are_not_evidence(w: World):
    command(w, 5, 'uv run personal-os ask "which STT model was suggested?"', ok=True)
    command(w, 6, "pytest -q", ok=True)
    for minute in (7, 7.1, 7.2):
        search(w, minute, "local best STT")
    lines = lines_from_rows(rows(w))
    texts = " ".join(x.text for x in lines)
    assert "personal-os ask" not in texts and "pytest -q" in texts
    assert sum(1 for x in lines if x.kind == "search") == 1


def test_word_order_tells_speech_to_text_from_text_to_speech(w: World):
    visit(w, 0, 3, "https://www.google.com/search?q=stt", "best local speech to text",
          "The best local speech-to-text models: Canary-Qwen 2.5B and Moonshine. " * 3)  # fmt: skip
    visit(w, 10, 13, "https://www.google.com/search?q=tts", "best local text to speech",
          "The best local text-to-speech model: Kokoro-82M. " * 3)  # fmt: skip
    assert "speech to text" in ask_mod.phrases("the best local speech to text model")
    lines = lines_from_rows(rows(w), focus=["best", "local", "speech", "text", "model"])
    kept = ask_mod._keep_closest(
        lines, ["best", "local", "speech", "text", "model"], "the best local speech to text model"
    )
    assert [x.text for x in kept if x.kind == "page"] == [
        "'best local speech to text' (www.google.com), 3m"
    ]


def test_own_commands_and_revisits_do_not_crowd_search_out(w: World):
    for minute in range(12):  # asking the question again and again
        command(w, minute, 'uv run personal-os ask "which local STT model was suggested"', ok=True)
    url = "https://www.google.com/search?q=local+best+STT"
    for minute in (20, 20.5, 21, 21.5):
        visit(w, minute, minute + 0.4, url, "local best STT - Google Search", STT_ANSWER)
    hits = [type("H", (), {"kind": "event", "id": r["id"]})() for r in rows(w)]
    useful = ask_mod._useful_hits(w.conn, hits)
    kinds = {w.conn.execute("SELECT type FROM events WHERE id = ?", (h.id,)).fetchone()[0]
             for h in useful}  # fmt: skip
    assert "command" not in kinds and len(useful) == 1  # one page, none of the questions


def test_the_same_page_text_is_stored_once(w: World):
    url = "https://example.org/article"
    for minute in (0, 1, 2):
        visit(w, minute, minute + 0.5, url, "Article", "Exactly the same long text. " * 50)
    visit(w, 3, 3.5, url, "Article", "The text changed: an update. " * 50)
    stored = [
        r[0] for r in w.conn.execute("SELECT length(main_text) FROM page_extracts ORDER BY ts")
    ]
    assert stored[0] > 0 and stored[1:3] == [0, 0]  # repeats keep their row, not the text
    assert stored[3] > 0  # changed text is stored


def test_the_exact_words_beat_look_alikes_and_a_second_look_is_taken(w: World):
    visit(w, 0, 3, "https://www.google.com/search?q=local+best+STT",
          "local best STT - Google Search", STT_ANSWER)  # fmt: skip
    visit(w, 10, 13, "https://www.google.com/search?q=small+local+TTS",
          "small local TTS model - Google Search", TTS_ANSWER)  # fmt: skip
    prompts: list[str] = []

    def reply(system: str, user: str, schema: Any) -> dict[str, Any]:
        prompts.append(user)
        if "Note: records" not in user:
            return {"answered": False, "sentences": [], "confidence": "low"}
        ref = int(user.split("Note: records [")[1].split("]")[0])
        return {"answered": True, "confidence": "high",
                "sentences": [{"text": "Canary-Qwen 2.5B was suggested.", "refs": [ref]}]}  # fmt: skip

    answer = ask_mod.ask(
        w.conn, FakeProvider(reply), "which local STT model was suggested to me?", w.clock.now()
    )
    assert answer.answered and "Canary-Qwen" in answer.text
    assert len(prompts) == 2  # it looked again
    data = prompts[0]
    assert "local best STT" in data and "Kokoro" not in data  # the TTS page was a look-alike
