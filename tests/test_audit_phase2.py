"""Phase 2: a request never waits behind background work or a prompt read ahead; the prompt
is smaller; claims without a subject are checked."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from personal_os.config.schema import Config
from personal_os.llm.provider import LLMCancelled
from tests.test_actions import Env

NOW = datetime(2026, 11, 21, 10, 0).astimezone()

# --- background model jobs give way to a conversation --------------------------------------


@pytest.mark.parametrize(
    ("st", "busy"),
    [
        ({"running": True, "state": "thinking"}, True),
        ({"running": True, "state": "ready", "turn": {"done_at": (NOW - timedelta(seconds=30)).isoformat()}}, True),
        ({"running": True, "state": "ready", "turn": {"done_at": (NOW - timedelta(minutes=5)).isoformat()}}, False),
        ({"running": False, "state": "not running"}, False),
    ],
)  # fmt: skip
def test_donna_in_a_conversation_holds_background_jobs(
    st: dict[str, Any], busy: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    from personal_os.context import gate
    from personal_os.voice import main as voice_main

    monkeypatch.setattr(voice_main, "status", lambda _rt: st)
    assert gate.voice_busy(tmp_path, NOW) is busy


def test_the_gate_says_why(config: Config):
    from personal_os.context.gate import JobGate

    g = JobGate(config.llm, idle_seconds=lambda: 9999, fullscreen=lambda: False,
                paused=lambda: False, ac_power=lambda: True, voice_busy=lambda: True)  # fmt: skip
    assert g.check().reason == "Donna is in a conversation"


def test_a_cancelled_job_stops_the_run_and_marks_nothing_failed(
    config: Config, monkeypatch: pytest.MonkeyPatch, home: Path
):
    from personal_os.context import jobs
    from personal_os.core.clock import SystemClock
    from personal_os.storage.db import open_db

    monkeypatch.setattr(jobs, "pending_sessions", lambda *_a, **_k: [("s1", None), ("s2", None)])
    seen: list[str] = []

    def summarize(conn: Any, provider: Any, clock: Any, session: str, pack: Any) -> Any:
        seen.append(session)
        raise LLMCancelled("stopped")

    monkeypatch.setattr(jobs, "summarize_session", summarize)
    report = jobs.run_llm_jobs(open_db(home / "j.db"), SimpleNamespace(), SystemClock(),
                               config.llm, lambda: False)  # fmt: skip
    assert report.stopped_early and report.failed == 0 and seen == ["s1"]


def test_a_cancelled_summary_is_not_stored_as_failed(monkeypatch: pytest.MonkeyPatch):
    from personal_os.context import summarizer

    stored: list[Any] = []
    monkeypatch.setattr(summarizer, "_store", lambda *a: stored.append(a))

    class Cancelled:
        chat_model = "m"

        def chat_json(self, *_a: Any, **_k: Any) -> Any:
            raise LLMCancelled("stopped")

    session = SimpleNamespace()
    pack = SimpleNamespace(refs=lambda: set())
    monkeypatch.setattr(summarizer, "session_prompt", lambda *_a: "x")
    with pytest.raises(LLMCancelled):
        summarizer.summarize_session(None, Cancelled(), None, session, pack=pack)  # type: ignore[arg-type]
    assert stored == []


# --- a turn never waits behind the prompt read ahead -----------------------------------------


def test_a_turn_stops_the_prompt_being_read_ahead(home: Path, config: Config):
    from tests.test_phase7 import voice_for

    v, _h = voice_for(Env(home, config))
    stop = threading.Event()
    v._prime_stop = stop
    v.respond("typed", "hi donna")
    assert stop.is_set()


def test_priming_can_be_cancelled(config: Config, monkeypatch: pytest.MonkeyPatch):
    from personal_os.llm.ollama import OllamaProvider

    p = OllamaProvider(config.llm)
    got: list[Any] = []
    monkeypatch.setattr(p, "_send", lambda path, body, on_text=None, cancel=None:
                        got.append(cancel) or {"total_duration": 1e9})  # fmt: skip
    stop = threading.Event()
    p.prime("system", cancel=stop)
    assert got == [stop]


# --- a smaller prompt: project ids, their folders filled in -----------------------------------


@pytest.fixture
def projects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    from personal_os.actions import planner

    folder = tmp_path / "REA" / "shop_app"
    folder.mkdir(parents=True)
    fake = [SimpleNamespace(id="shop-app", roots=[folder]),
            SimpleNamespace(id="none", roots=[])]  # fmt: skip
    monkeypatch.setattr(planner, "load_projects", lambda _conn: fake)
    return folder


def test_the_prompt_lists_project_ids_not_folders(home: Path, config: Config, projects: Path):
    from personal_os.actions.planner import tools_block

    block = tools_block(Env(home, config).ex)
    assert "shop-app" in block and str(projects) not in block and "shop_app" not in block


def test_a_project_id_becomes_its_folder(home: Path, config: Config, projects: Path):
    from personal_os.actions.planner import project_paths

    env = Env(home, config)
    calls = [SimpleNamespace(tool="open_app", args={"app": "vscode", "target": "shop-app"}),
             SimpleNamespace(tool="open_path", args={"path": "Shop-App"}),
             SimpleNamespace(tool="open_path", args={"path": "notes.txt"})]  # fmt: skip
    project_paths(calls, env.ex)
    assert calls[0].args == {"app": "vscode", "target": str(projects)}
    assert calls[1].args == {"path": str(projects)}
    assert calls[2].args == {"path": "notes.txt"}


# --- claims with no subject ----------------------------------------------------------------------


def test_claims_without_a_subject_are_found():
    from personal_os.actions.agent import claims

    assert claims("Playing your music now.") == ["Playing"]
    assert claims("Sure. Opened Brave for you.") == ["Opened"]
    assert claims("The song playing is Bad Luck.") == []  # describes, doesn't claim


def test_a_bare_claim_with_no_tool_is_corrected(home: Path, config: Config):
    from personal_os.actions import agent
    from personal_os.llm.fake import FakeProvider

    env = Env(home, config)

    def model(system: str, user: str, schema: Any) -> Any:
        return {"decide": "answer", "calls": [], "reply": "Opened Brave for you."}

    out = agent.respond("brave please", env.ex, FakeProvider(model), max_steps=1)
    assert "Opened" not in out.reply
