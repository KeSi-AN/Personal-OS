"""Reminders that must be seen (shown in the popup, not a toast Windows may hide), questions
about reminders, and local times in the voice log."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from personal_os.actions import reminders
from personal_os.config.schema import Config
from personal_os.core.clock import SimClock
from personal_os.interfaces import alerts, popup
from personal_os.voice.popup_view import AutoClose, View, read_view
from tests.conftest import T0


@pytest.fixture
def available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(popup, "unavailable_reason", lambda: None)


# --- alert files ----------------------------------------------------------------------------


def test_alerts_are_kept_until_dismissed_and_expire(tmp_path: Path):
    alerts.add(tmp_path, "r1", "Reminder", "stretch", T0)
    alerts.add(tmp_path, "r2", "Reminder", "water", T0 + timedelta(minutes=1))
    got = alerts.pending(tmp_path, T0 + timedelta(minutes=2))
    assert [a["text"] for a in got] == ["stretch", "water"]
    assert alerts.dismiss(tmp_path, "r1") is True
    assert alerts.dismiss(tmp_path, "r1") is False
    assert [a["id"] for a in alerts.pending(tmp_path, T0)] == ["r2"]
    assert alerts.pending(tmp_path, T0 + timedelta(hours=13)) == []  # expired, and removed
    assert list((tmp_path / alerts.DIR).iterdir()) == []


@pytest.mark.parametrize("bad", ["../voice", "..\\x", "a/b", "", "x" * 81, "r1.json"])
def test_dismiss_only_takes_our_own_ids(tmp_path: Path, bad: str):
    (tmp_path / "voice.json").write_text("{}", "utf-8")
    assert alerts.dismiss(tmp_path, bad) is False
    assert (tmp_path / "voice.json").exists()
    with pytest.raises(ValueError):
        alerts.add(tmp_path, bad or "../x", "t", "x", T0)


def test_show_opens_the_popup(config: Config, available: None, no_real_popup: list[bool]):
    assert alerts.show(config, "r1", "Reminder", "stretch", T0) is True
    assert no_real_popup == [False]
    assert alerts.pending(config.runtime_dir, T0)[0]["text"] == "stretch"


def test_show_falls_back_when_the_popup_cannot_open(config: Config, monkeypatch):
    monkeypatch.setattr(popup, "unavailable_reason", lambda: "the WebView2 runtime isn't installed")
    assert alerts.show(config, "r1", "Reminder", "stretch", T0) is False
    assert alerts.pending(config.runtime_dir, T0) == []  # it'll be a toast instead


def fire(config: Config, monkeypatch: pytest.MonkeyPatch, *, popup_ok: bool) -> list[str]:
    """Run the daemon's reminder announcement with fakes; returns what was toasted."""
    from personal_os import daemon
    from personal_os.interfaces import notify

    toasts: list[str] = []
    monkeypatch.setattr(notify, "toast", lambda title, text, **_k: toasts.append(text) or True)
    monkeypatch.setattr(alerts, "show", lambda *_a, **_k: popup_ok)
    voice_off = config.model_copy(
        update={"voice": config.voice.model_copy(update={"enabled": False})}
    )
    fake = SimpleNamespace(_fresh_config=lambda: voice_off, clock=SimClock(T0), tray=None)
    due = [reminders.Reminder("r1", "stretch", T0)]
    daemon.Daemon._announce_reminders(fake, due)  # type: ignore[arg-type]
    return toasts


def test_a_due_reminder_goes_to_the_popup_not_a_hideable_toast(config: Config, monkeypatch):
    """The bug: during a full-screen app Windows hid the toast, so only the voice was heard."""
    assert fire(config, monkeypatch, popup_ok=True) == []


def test_without_the_popup_it_is_still_a_toast(config: Config, monkeypatch):
    assert fire(config, monkeypatch, popup_ok=False) == ["stretch"]


def test_the_popup_stays_while_a_reminder_is_unread(config: Config):
    a = AutoClose(5)
    alert = ({"id": "r1", "title": "Reminder", "text": "stretch", "at": T0.isoformat()},)
    assert a.step(View(False, "not running", alerts=alert), 0)[0] is False
    assert a.step(View(False, "not running", alerts=alert), 600)[0] is False
    assert a.step(View(False, "not running"), 601)[0] is False  # dismissed: the delay starts
    assert a.step(View(False, "not running"), 606)[0] is True


def test_read_view_includes_alerts(config: Config):
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    alerts.add(config.runtime_dir, "r1", "Reminder", "stretch", T0)
    v = read_view(config.runtime_dir, T0)
    assert [a["text"] for a in v.alerts] == ["stretch"]


def test_api_dismiss(config: Config):
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    alerts.add(config.runtime_dir, "r1", "Reminder", "stretch", T0)
    api = popup.Api(config, demo=False)
    assert api.dismiss("r1") is True
    assert alerts.pending(config.runtime_dir, T0) == []


def test_popup_page_builds_alerts_without_html_parsing():
    html = popup.page()
    assert "renderAlerts" in html and "textContent = a.text" in html
    assert "innerHTML = a." not in html


# --- questions about reminders --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "is_q"),
    [
        ("What was the reminder I asked you to give me?", True),
        ("What reminders do I have?", True),
        ("Do I have any reminders today", True),
        ("Hey Donna, what did I ask you to remind me about?", True),
        ("remind me in two minutes to stretch", False),  # a request, not a question
        ("set a reminder for 5 pm", False),
        ("what did I work on yesterday", False),
    ],
)
def test_reminder_questions(text: str, is_q: bool):
    assert reminders.is_question(text) is is_q


def test_reminder_answer_lists_upcoming_and_recent(conn: Any):
    clock = SimClock(T0)
    reminders.add(conn, clock, "stretch", T0 - timedelta(minutes=5), "a1")
    reminders.take_due(conn, clock)  # fired
    reminders.add(conn, clock, "call mum", T0 + timedelta(hours=2), "a2")
    lines = reminders.answer(conn, T0)
    local = (T0 + timedelta(hours=2)).astimezone()
    assert lines[0].startswith('You have 1 reminder coming up: "call mum" at ')
    assert f"{local:%H:%M}" in lines[0]
    assert lines[1].startswith('In the last day: "stretch" at ')


def test_no_reminders(conn: Any):
    assert reminders.answer(conn, T0) == ["You have no reminders coming up."]


def test_ask_answers_reminder_questions_without_the_model(conn: Any):
    from personal_os.context.ask import ask

    reminders.add(conn, SimClock(T0), "stretch", T0 + timedelta(minutes=2), "a1")
    a = ask(conn, None, "What was the reminder I asked you to give me?", T0)
    assert a.mode == "facts" and a.answered
    assert '"stretch"' in a.text


# --- local times ----------------------------------------------------------------------------


def test_voice_log_times_are_local():
    from personal_os.voice.main import local_time

    at = datetime(2026, 11, 5, 20, 37, 31, tzinfo=UTC)
    assert local_time(at.isoformat()) == at.astimezone().strftime("%H:%M:%S")
    assert local_time("garbage") == ""


# --- reminder times as speech recognition writes them ---------------------------------------


@pytest.mark.parametrize(
    ("at", "hhmm"),
    [
        ("2:10", "02:10"),
        ("2.10", "02:10"),
        ("17.30", "17:30"),
        ("5 p.m.", "17:00"),
        ("5 p. m.", "17:00"),
        ("5:30 p.m.", "17:30"),
        ("5 PM", "17:00"),
        ("12 a.m.", "00:00"),
        ("5 o'clock", "05:00"),
    ],
)
def test_spoken_times(at: str, hhmm: str):
    assert f"{reminders.due_time(T0, None, at).astimezone():%H:%M}" == hhmm


# --- one voice, never two at once -----------------------------------------------------------


def test_the_daemon_uses_the_configured_piper_voice(tmp_path: Path):
    from personal_os.voice.tts import PiperVoice

    for name in ("en_US-amy-medium", "en_US-hfc_female-medium"):
        (tmp_path / f"{name}.onnx").write_bytes(b"")
    assert PiperVoice(tmp_path, "en_US-hfc_female-medium").model() == (
        tmp_path / "en_US-hfc_female-medium.onnx"
    )
    assert PiperVoice(tmp_path, "missing").model() == tmp_path / "en_US-amy-medium.onnx"


def test_announcements_are_queued_for_the_voice_process(tmp_path: Path, monkeypatch):
    from personal_os.voice import main as voice_main

    monkeypatch.setattr(voice_main, "status", lambda _rt: {"running": False})
    assert voice_main.queue_speech(tmp_path, "Reminder: stretch") is False  # caller speaks
    monkeypatch.setattr(voice_main, "status", lambda _rt: {"running": True})
    assert voice_main.queue_speech(tmp_path, "Reminder: stretch") is True
    assert voice_main.queue_speech(tmp_path, "Reminder: water") is True
    assert voice_main.take_speech(tmp_path) == ["Reminder: stretch", "Reminder: water"]
    assert voice_main.take_speech(tmp_path) == []  # each said once
    voice_main.queue_speech(tmp_path, "old news")
    assert voice_main.take_speech(tmp_path, max_age_s=-1) == []  # too old: dropped, not said


def test_the_voice_process_speaks_queued_announcements_when_idle(
    home: Path, config: Config, monkeypatch
):
    from personal_os.voice import main as voice_main
    from tests.test_actions import Env
    from tests.test_phase7 import voice_for

    env = Env(home, config)
    v, h = voice_for(env)
    env.config.runtime_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(voice_main, "status", lambda _rt: {"running": True})
    voice_main.queue_speech(env.config.runtime_dir, "Reminder: stretch")
    v.speak_queued()
    assert h.said == ["Reminder: stretch"]


def test_the_daemon_hands_speech_to_voice(config: Config, monkeypatch):
    from personal_os import daemon
    from personal_os.voice import main as voice_main
    from personal_os.voice import tts

    queued: list[str] = []
    monkeypatch.setattr(voice_main, "queue_speech", lambda _rt, text: queued.append(text) or True)
    monkeypatch.setattr(tts.Speaker, "say", lambda *_a, **_k: pytest.fail("spoke in the daemon"))
    daemon.Daemon._speak(SimpleNamespace(), config, "Reminder: stretch")  # type: ignore[arg-type]
    assert queued == ["Reminder: stretch"]
