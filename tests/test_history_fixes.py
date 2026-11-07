"""What "what did I do today?" is built from (7 Nov): a `git pull` of 15 commits made
elsewhere became "you made 15 commits", automated activity while you were away became working
time, and voice answered from what was open instead of from your history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from personal_os.config.schema import Config
from personal_os.context.evidence import fetch_rows, lines_from_rows, own_commit
from personal_os.context.route import about_my_past
from personal_os.core.clock import to_ms
from personal_os.core.events import Event, GitCommit
from personal_os.observers.git import _arrived
from personal_os.observers.gitutil import parse_log, parse_reflog
from personal_os.projects.sessions import build_sessions
from tests.conftest import T0
from tests.test_actions import Env
from tests.test_phase7 import voice_for
from tests.test_projects import AUTH, World

NOW = datetime(2026, 11, 7, 18, 50, tzinfo=UTC)


def test_commits_a_pull_brought_in_are_not_made_here():
    old = (NOW - timedelta(days=1)).isoformat()
    pulled_tip = "pull origin main: Fast-forward"
    # The tip of a fast-forward is in the reflog as the pull; the commits under it aren't.
    assert _arrived("pull origin main: Fast-forward", pulled_tip, old, NOW)
    assert _arrived(None, pulled_tip, NOW.isoformat(), NOW)  # under a pull: arrived
    assert not _arrived("commit: fix the popup", "commit: fix the popup", NOW.isoformat(), NOW)
    assert not _arrived("commit (amend): fix", "", NOW.isoformat(), NOW)
    assert _arrived("checkout: moving from a to b", "", NOW.isoformat(), NOW)
    assert _arrived(None, "", old, NOW)  # no reflog at all: an old commit arrived
    assert not _arrived(None, "", (NOW - timedelta(minutes=2)).isoformat(), NOW)


def test_the_log_and_reflog_are_parsed():
    log = b"a1\x1fp1\x1fme@x.com\x1f2026-11-07T18:00:00+05:30\x1ffix: one\n\nbody\x1e\n"
    (c,) = parse_log(log, with_date=True)
    assert (c.sha, c.committed_at, c.message) == (
        "a1",
        "2026-11-07T18:00:00+05:30",
        "fix: one\n\nbody",
    )
    assert parse_reflog(b"a1\x1fcommit: one\na1\x1fcheckout: x\nb2\x1fpull: y\n") == {
        "a1": "commit: one", "b2": "pull: y",
    }  # fmt: skip


def test_own_commit():
    assert own_commit({"author_is_me": True})
    assert own_commit({})  # older events: made here, as before
    assert not own_commit({"author_is_me": False})  # someone else's (all 14 on 7 Nov)
    assert not own_commit({"author_is_me": True, "pulled": True})


def test_pulled_commits_are_one_line_and_never_listed_as_yours(home: Path, config: Config):
    w = World(home, config)
    for i in range(15):
        w.add(Event.point(
            GitCommit(repo_root=AUTH, sha=f"{i:040d}", message=f"theirs {i}", author_is_me=False),
            w.at(0), observer_version="t",
        ))  # fmt: skip
    w.commit(5, AUTH, "mine")
    w.window(0, 30, "session.py - auth-service - Visual Studio Code")
    w.resolve()
    rows = fetch_rows(w.conn, to_ms(T0), to_ms(T0 + timedelta(hours=1)), sources=("git",))
    lines = lines_from_rows(rows)
    pulled = [x for x in lines if x.kind == "pulled"]
    commits = [x for x in lines if x.kind == "commit"]
    assert len(pulled) == 1 and pulled[0].text.startswith("pulled 15 commits into auth-service")
    assert len(pulled[0].event_ids) == 15
    assert [x.text.split("'")[1] for x in commits] == ["mine"]
    (s,) = build_sessions(w.conn, T0, T0 + timedelta(hours=1))
    assert s.stats["commits"] == ["mine"] and s.stats["pulled"] == 15


def test_questions_about_your_past_are_recognised():
    assert about_my_past("What did I do today?")
    assert about_my_past("where did I leave off on personal-os?")
    assert about_my_past("How long was I on YouTube yesterday?")
    assert not about_my_past("what's on my screen?")
    assert not about_my_past("what did I ask you to remind me?")  # reminders answer that
    assert not about_my_past("what did I say earlier?")  # the conversation answers that
    assert not about_my_past("open brave")


def test_voice_answers_your_past_from_history_like_the_cli(home: Path, config: Config, monkeypatch):
    import personal_os.context.ask as ask_mod

    asked: list[str] = []

    class Answer:
        question = "what did I do today?"
        text = "You worked on auth-service."
        sentences = (("You worked on auth-service.", [1]),)

        def cited(self) -> list[object]:
            return []

    def fake_ask(conn: object, provider: object, q: str, now: object, **_k: object) -> Answer:
        asked.append(q)
        return Answer()

    monkeypatch.setattr(ask_mod, "ask", fake_ask)
    agent_used: list[str] = []
    v, _h = voice_for(Env(home, config))
    v.agent = lambda conn, request: agent_used.append(request) or "from the agent"  # type: ignore[method-assign,assignment,func-returns-value]
    assert v.handle("Donna, what did I do today?") == "You worked on auth-service."
    assert asked and not agent_used


def test_silence_to_a_yes_or_no_is_not_called_declining(home: Path, config: Config):
    from types import SimpleNamespace

    v, h = voice_for(Env(home, config))  # nothing will be heard
    ex = SimpleNamespace(pending={"tool": "close_app", "args": {"app": "vscode"}})
    assert v.confirm("close_app (tier 1)\nProceed?", ex) is False
    assert v.unanswered and h.said == ["Close vscode? Say yes or no."]
