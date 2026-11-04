"""Phase 7: routing, voice handling, briefings, Google (faked), plugins, MCP, config."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from personal_os.actions.base import Origin, Tier
from personal_os.config.schema import Config
from personal_os.context import briefing, memory
from personal_os.context.route import route, strip_wake
from personal_os.core.clock import SimClock
from personal_os.integrations import google
from personal_os.llm.fake import FakeProvider
from personal_os.projects.sessions import rebuild_sessions
from personal_os.voice.main import Voice, is_yes
from personal_os.voice.tts import speakable
from tests.conftest import T0
from tests.test_actions import Env, plan
from tests.test_context import auth_session
from tests.test_projects import World


@pytest.fixture
def w(home: Path, config: Config) -> World:
    return World(home, config)


# --- routing and speech text -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Hey Donna, brief me", "briefing"),
        ("good morning", "briefing"),
        ("Donna, how was my day?", "briefing"),
        ("remember that the staging db is on port 5433", "remember"),
        ("remind me in two minutes to stretch", "do"),
        ("open the auth-service folder", "do"),
        ("where did I leave off on personal-os?", "ask"),
        ("what's the weather like", "ask"),
        ("stop", "stop"),
        ("Hey Donna, cancel.", "stop"),
    ],
)
def test_route(text: str, kind: str):
    assert route(text) == kind


def test_strip_wake_and_speakable():
    assert strip_wake("Hey Donna, what did I do yesterday?") == "what did I do yesterday?"
    assert strip_wake("donna open it") == "open it"
    said = speakable("You fixed `client.py` [3] [4]. See https://x.example/a **now**.", 200)
    assert said == "You fixed client.py. See a link now."
    long = speakable("One sentence here. " * 50, 120)
    assert len(long) <= 120 and long.endswith(".")


@pytest.mark.parametrize(
    ("answer", "ok"),
    [("yes", True), ("Yeah, go ahead.", True), ("confirm", True), ("no", False),
     ("yes, no wait", False), ("", False), ("hmm", False), ("don't", False)],
)  # fmt: skip
def test_spoken_yes(answer: str, ok: bool):
    assert is_yes(answer) is ok


# --- briefings ---------------------------------------------------------------------------------


class FakeGoogle(google.Google):
    def __init__(self) -> None:
        super().__init__(Path("."))

    def calendar(
        self, start: datetime, end: datetime, limit: int = 20
    ) -> list[google.CalendarEvent]:
        return [google.CalendarEvent("Standup", start + timedelta(hours=10), None, False),
                google.CalendarEvent("Holiday", start, None, True)]  # fmt: skip

    def inbox(self, top: int = 3) -> google.Inbox:
        return google.Inbox(4, [google.MailHeader("Asha Rao", "Release notes")][:top])


def test_morning_and_evening_briefings(w: World):
    auth_session(w)  # 09:00-09:40 on day 1, with a failing then passing pytest
    from personal_os.storage.db import transaction

    rebuild_sessions(w.conn, w.clock, w.at(-60), w.at(600))
    with transaction(w.conn):
        pass
    day2 = SimClock(T0 + timedelta(days=1, hours=1))
    b = briefing.build(w.conn, day2, "morning")
    assert b.title.startswith("Good") and "auth-service" in b.text
    assert any("Google isn't connected" in n for n in b.notes)
    g = briefing.build(w.conn, day2, "morning", google=FakeGoogle())
    assert "Standup" in g.text and "4 unread" in g.text and "Asha Rao about Release notes" in g.text
    e = briefing.build(w.conn, SimClock(T0 + timedelta(hours=10)), "evening")
    assert "Today you worked" in e.text and "1 commit" in e.text


def test_briefing_is_due_once_a_day(w: World):
    at_10 = SimClock(T0.replace(hour=10))
    assert briefing.due_kind(w.conn, at_10, "09:00", "18:30") == "morning"
    briefing.mark_delivered(w.conn, at_10, "morning")
    assert briefing.due_kind(w.conn, at_10, "09:00", "18:30") is None
    at_19 = SimClock(T0.replace(hour=19))
    assert briefing.due_kind(w.conn, at_19, "09:00", "18:30") == "evening"
    assert briefing.due_kind(w.conn, SimClock(T0.replace(hour=8)), "09:00", "18:30") is None


# --- Google (faked network and keyring) -----------------------------------------------------------


class FakeKeyring:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, s: str, k: str) -> str | None:
        return self.store.get((s, k))

    def set_password(self, s: str, k: str, v: str) -> None:
        self.store[(s, k)] = v

    def delete_password(self, s: str, k: str) -> None:
        self.store.pop((s, k), None)


def test_google_connect_flow_keeps_only_the_refresh_token(
    home: Path, monkeypatch: pytest.MonkeyPatch
):
    kr = FakeKeyring()
    monkeypatch.setattr(google, "_keyring", lambda: kr)
    posted: list[dict[str, str]] = []

    def fake_post(url: str, form: dict[str, str]) -> dict[str, Any]:
        posted.append({"url": url, **form})
        return {"access_token": "at", "refresh_token": "rt-123", "expires_in": 3600}

    monkeypatch.setattr(google, "_post", fake_post)
    secret = home / "client_secret.json"
    secret.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))

    def browser(url: str) -> None:  # stands in for you signing in
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert q["scope"][0].split() == google.SCOPES and q["code_challenge_method"] == ["S256"]
        redirect = q["redirect_uri"][0]
        threading.Thread(target=lambda: urllib.request.urlopen(
            f"{redirect}/?code=the-code&state={q['state'][0]}", timeout=5).read()).start()  # fmt: skip

    google.connect(home, secret, browser, timeout_s=10)
    assert kr.get_password(google.KEYRING_SERVICE, google.TOKEN_KEY) == "rt-123"
    assert posted[0]["code"] == "the-code" and posted[0]["code_verifier"]
    assert google.connected(home) and google.secret_path(home).exists()
    assert google.disconnect(home) and not google.connected(home)


def test_google_parsing(home: Path, monkeypatch: pytest.MonkeyPatch):
    g = google.Google(home)
    pages = {
        google.CAL_URL: {"items": [
            {"summary": "Standup", "start": {"dateTime": "2026-11-04T10:00:00+05:30"}, "end": {"dateTime": "2026-11-04T10:15:00+05:30"}},
            {"summary": "Holiday", "start": {"date": "2026-11-04"}, "end": {"date": "2026-11-05"}}]},
        google.GMAIL_URL: {"messages": [{"id": "m1"}], "resultSizeEstimate": 7},
        f"{google.GMAIL_URL}/m1": {"payload": {"headers": [
            {"name": "From", "value": "Asha Rao <asha@example.com>"}, {"name": "Subject", "value": "Release notes"}]}},
    }  # fmt: skip
    monkeypatch.setattr(g, "_get", lambda url, params: pages[url])
    events = g.calendar(T0, T0 + timedelta(days=1))
    assert [(e.title, e.all_day) for e in events] == [("Standup", False), ("Holiday", True)]
    inbox = g.inbox(top=1)
    assert inbox.unread == 7 and inbox.top[0].sender == "Asha Rao"


def test_google_tools_need_a_connection_and_taint(
    home: Path, config: Config, monkeypatch: pytest.MonkeyPatch
):
    env = Env(home, config)
    assert env.run("calendar_today").status == "blocked"
    monkeypatch.setattr(google, "connected", lambda d: True)
    monkeypatch.setattr(google, "Google", lambda d: FakeGoogle())
    out = env.run("mail_unread", top=1)
    assert out.ok and out.result.captured and "Asha Rao: Release notes" in out.result.data


# --- plugins -----------------------------------------------------------------------------------------


def test_plugins_load_only_when_enabled_and_tier3_stays_blocked(home: Path, config: Config):
    from personal_os.actions.plugins import EXAMPLE, plugin_dir
    from personal_os.actions.tools import build_registry

    folder = plugin_dir(config.data_dir)
    folder.mkdir(parents=True)
    (folder / "word_count.py").write_text(EXAMPLE)
    (folder / "danger.py").write_text(EXAMPLE.replace("word_count", "nuke").replace("Tier.READ", "Tier.BLOCKED")
                                      .replace("WordCount", "Nuke"))  # fmt: skip
    (folder / "broken.py").write_text("raise RuntimeError('oops')")
    (folder / "clash.py").write_text(
        EXAMPLE.replace('"word_count"', '"open_path"').replace("WordCount", "Clash")
    )
    assert build_registry(config).get("word_count") is None  # off by default
    cfg = config.model_copy(
        update={"actions": config.actions.model_copy(update={"plugins_enabled": True})}
    )
    reg = build_registry(cfg)
    assert reg.get("word_count") is not None and reg.get("nuke") is not None
    assert reg.get("open_path").__class__.__name__ == "OpenPath"  # not replaced
    env = Env(home, cfg)
    env.ex.registry = reg
    out = env.run("word_count", text="one two three")
    assert out.ok and out.result.data == "3"
    assert env.run("nuke", text="x").status == "blocked"
    from personal_os.actions.audit import history

    assert history(env.conn, out.action_id)[0].detail == "plugin tool"
    assert "nuke" not in [t.name for t in reg.for_planner()]


# --- MCP ------------------------------------------------------------------------------------------------


def test_mcp_server_is_read_only(w: World, config: Config):
    from personal_os.interfaces.mcp_server import server

    names = {t.name for t in asyncio.run(server.list_tools())}
    assert names == {"ask", "search", "sessions", "project_state", "list_projects"}


# --- the voice handler (no microphone) ------------------------------------------------------------------


class Heard:
    """Stands in for the microphone: returns queued phrases."""

    def __init__(self, *phrases: str) -> None:
        self.phrases = list(phrases)
        self.said: list[str] = []

    def listen(self) -> str:
        return self.phrases.pop(0) if self.phrases else ""

    def say(self, text: str) -> None:
        self.said.append(text)


def voice_for(env: Env, *phrases: str, provider: Any = None) -> tuple[Voice, Heard]:
    v = Voice(env.config)
    h = Heard(*phrases)
    v.listen = h.listen  # type: ignore[method-assign]
    v.say = h.say  # type: ignore[method-assign]
    v.provider = lambda: provider  # type: ignore[method-assign]
    return v, h


def test_voice_remember_needs_a_spoken_yes(home: Path, config: Config):
    env = Env(home, config)
    v, _h = voice_for(env, "no")
    assert v.handle("Hey Donna, remember that standup moved to 10:30") == "I won't remember it."
    v, _h = voice_for(env, "yes please")
    assert v.handle("remember that standup moved to 10:30") == "Remembered."
    from personal_os.storage.db import open_db

    facts = memory.all_facts(open_db(env.config.db_path))  # where the voice process writes
    assert [(f.text, f.source) for f in facts] == [("standup moved to 10:30", "voice")]


def test_voice_actions_confirm_by_voice(
    home: Path, config: Config, monkeypatch: pytest.MonkeyPatch
):
    import personal_os.actions.desktop as desktop_mod

    env = Env(home, config)
    monkeypatch.setattr(desktop_mod, "default_desktop", lambda: env.desktop)
    target = env.work / "voice.md"
    fake = FakeProvider(
        lambda s, u, schema: plan(("create_file", {"path": str(target), "content": "hi"}))
    )
    v, h = voice_for(env, "no", provider=fake)
    assert "0 of 1" in v.handle("create a file voice.md") and not target.exists()
    assert "Say yes or no" in h.said[0]
    v, h = voice_for(env, "yes", provider=fake)
    assert "created" in v.handle("create a file voice.md").lower() and target.exists()
    fake2 = FakeProvider(
        lambda s, u, schema: plan(("set_reminder", {"text": "stretch", "minutes": 2}))
    )
    v, h = voice_for(env, provider=fake2)
    assert v.handle("remind me in 2 minutes to stretch").startswith("I'll remind you at")
    assert h.said == []  # tier 1, asked directly: no confirmation needed


def test_voice_briefing_and_stop(home: Path, config: Config):
    env = Env(home, config)
    v, _h = voice_for(env)
    assert v.handle("good morning").startswith("Good")
    assert v.handle("stop") == ""


# --- config ---------------------------------------------------------------------------------------------


def test_set_value_adds_a_section_the_file_doesnt_have_yet(home: Path):
    from personal_os.config.loader import config_path, load_config, set_value

    load_config(home)
    path = config_path(home)
    text = path.read_text("utf-8")
    path.write_text(text.split("[voice]")[0], encoding="utf-8")  # an older file
    assert set_value("voice.enabled", True, home).voice.enabled
    assert "[voice]" in path.read_text("utf-8")
    with pytest.raises(KeyError):
        set_value("voice.nonsense", True, home)
    _ = (Origin, Tier)


# --- tracking the voice process ------------------------------------------------------------------


def test_stale_voice_status_reads_as_not_running(tmp_path: Path):
    from personal_os.voice.main import status

    assert status(tmp_path)["state"] == "not running"
    (tmp_path / "voice.json").write_text(json.dumps({"state": "ready", "pid": 999_999_9}))
    s = status(tmp_path)
    assert s["state"] == "not running" and s["running"] is False


def test_voice_history_keeps_the_last_30(tmp_path: Path):
    from personal_os.voice.main import HISTORY_KEEP, history, record_history

    for i in range(HISTORY_KEEP + 5):
        record_history(tmp_path, {"heard": f"phrase {i}"})
    kept = history(tmp_path, 100)
    assert len(kept) == HISTORY_KEEP and kept[-1]["heard"] == f"phrase {HISTORY_KEEP + 4}"


def test_a_voice_request_is_recorded(home: Path, config: Config, monkeypatch: pytest.MonkeyPatch):
    import winsound

    from personal_os.voice.main import history

    monkeypatch.setattr(winsound, "Beep", lambda *_a: None)
    env = Env(home, config)
    v, _h = voice_for(env, "good morning")
    v.respond("hotkey")
    (entry,) = history(env.config.runtime_dir)
    assert entry["heard"] == "good morning" and entry["route"] == "briefing"
    assert entry["reply"].startswith("Good") and "total_s" in entry
    v, _h = voice_for(env, "")
    v.respond("hotkey")
    assert history(env.config.runtime_dir)[-1]["route"] == "nothing heard"
