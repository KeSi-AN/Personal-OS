"""Phase 8: the voice popup. The live turn in the status file, atomic writes, the one-popup
lock, falling back without pywebview or WebView2, the pure view, and auto-close/pin.
No window is opened (conftest replaces the spawn) and no audio is played."""

from __future__ import annotations

import json
import os
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from personal_os.actions import killswitch
from personal_os.config.schema import Config
from personal_os.core import runtime
from personal_os.interfaces import popup
from personal_os.voice import main as voice_main
from personal_os.voice.popup_view import AutoClose, View, _paused, read_view, view_from
from tests.conftest import T0
from tests.test_actions import Env
from tests.test_phase7 import voice_for


@pytest.fixture
def quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    import winsound

    monkeypatch.setattr(winsound, "Beep", lambda *_a: None)


@pytest.fixture
def voice_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """This test process passes for the voice process in the liveness check."""
    import psutil

    class Proc:
        def __init__(self, _pid: int) -> None:
            pass

        def cmdline(self) -> list[str]:
            return ["python", "-m", "personal_os.voice.main"]

    monkeypatch.setattr(psutil, "Process", Proc)


@pytest.fixture
def available(monkeypatch: pytest.MonkeyPatch) -> None:
    """As if pywebview and WebView2 were installed, whatever this machine has."""
    monkeypatch.setattr(popup, "unavailable_reason", lambda: None)


# --- the status file ------------------------------------------------------------------------


def test_a_turn_is_published_live_and_old_fields_stay(
    home: Path, config: Config, quiet: None, voice_process: None, monkeypatch: pytest.MonkeyPatch
):
    launched: list[Config] = []
    monkeypatch.setattr(voice_main, "_launch_popup", launched.append)
    env = Env(home, config)
    v, _h = voice_for(env, "good morning")
    seen: list[dict[str, Any]] = []
    write = v.write_status

    def spy(state: str, **extra: Any) -> None:
        write(state, **extra)
        data = json.loads((env.config.runtime_dir / "voice.json").read_text("utf-8"))
        seen.append(data)

    v.write_status = spy  # type: ignore[method-assign]
    v.respond("hotkey")

    states = [d["turn"]["state"] for d in seen if "turn" in d]
    assert states[0] == "listening" and states[-1] == "done"
    assert "thinking" in states and "speaking" in states
    thinking = next(d for d in seen if d.get("turn", {}).get("state") == "thinking")
    assert thinking["turn"]["heard"] == "good morning"  # shown as soon as it's heard
    assert thinking["turn"]["reply"] == ""
    final = json.loads((env.config.runtime_dir / "voice.json").read_text("utf-8"))
    turn = final["turn"]
    assert turn["trigger"] == "hotkey" and turn["id"] and turn["reply"].startswith("Good")
    assert turn["done_at"] and turn["started_at"]
    # Backward compatible: the top-level state and pid are still there for status, log, web.
    assert final["state"] == "ready" and final["pid"] == os.getpid()
    assert voice_main.status(env.config.runtime_dir)["running"] is True
    assert voice_main.history(env.config.runtime_dir)[-1]["heard"] == "good morning"
    assert len(launched) == 1  # the popup is asked for once per turn

    first_id = turn["id"]
    v2, _ = voice_for(env, "")
    v2.respond("wake")
    turn2 = json.loads((env.config.runtime_dir / "voice.json").read_text("utf-8"))["turn"]
    assert turn2["id"] != first_id and turn2["heard"] == "" and turn2["trigger"] == "wake"


def test_popup_off_means_no_launch(home: Path, config: Config, quiet: None, monkeypatch):
    launched: list[Config] = []
    monkeypatch.setattr(voice_main, "_launch_popup", launched.append)
    env = Env(
        home, config.model_copy(update={"voice": config.voice.model_copy(update={"popup": False})})
    )
    v, _h = voice_for(env, "good morning")
    v.respond("hotkey")
    assert launched == []


def test_a_popup_failure_never_reaches_voice(config: Config, monkeypatch: pytest.MonkeyPatch):
    def boom(_cfg: Config, **_k: Any) -> str:
        raise RuntimeError("window exploded")

    monkeypatch.setattr(popup, "launch", boom)
    voice_main._launch_popup(config)  # doesn't raise
    assert "window exploded" in popup.last_state(config.runtime_dir)["error"]


def test_atomic_write_retries_while_a_reader_holds_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    real = os.replace
    fails = [2]

    def flaky(src: Any, dst: Any) -> None:
        if fails[0]:
            fails[0] -= 1
            raise PermissionError("in use")
        real(src, dst)

    monkeypatch.setattr(runtime.os, "replace", flaky)
    monkeypatch.setattr(runtime.time, "sleep", lambda _s: None)
    runtime.write_json_atomic(tmp_path / "x.json", {"a": 1})
    assert runtime.read_json(tmp_path / "x.json") == {"a": 1}

    fails[0] = 10_000
    with pytest.raises(PermissionError):
        runtime.write_json_atomic(tmp_path / "x.json", {"a": 2})
    assert runtime.read_json(tmp_path / "x.json") == {"a": 1}  # never half-written
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]  # no temp file left


def test_atomic_writes_with_a_real_reader_polling(tmp_path: Path):
    path = tmp_path / "voice.json"
    runtime.write_json_atomic(path, {"n": 0})
    stop = threading.Event()
    bad: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                text = path.read_text("utf-8")
            except OSError:
                continue  # the popup treats this as "try again next poll"
            try:
                json.loads(text)
            except ValueError:
                bad.append(text)

    t = threading.Thread(target=reader)
    t.start()
    try:
        for n in range(300):
            runtime.write_json_atomic(path, {"n": n, "turn": {"heard": "x" * 200}})
    finally:
        stop.set()
        t.join()
    assert bad == []
    assert runtime.read_json(path)["n"] == 299  # type: ignore[index]


# --- one popup at a time; falling back ------------------------------------------------------


def test_the_lock_allows_one_popup(config: Config, available: None, no_real_popup: list[bool]):
    rt = config.runtime_dir
    assert popup.is_open(rt) is False
    held = runtime.InstanceLock(rt / popup.LOCK)
    held.acquire()
    try:
        assert popup.is_open(rt) is True
        assert popup.launch(config) == "already open"
        assert no_real_popup == []
        assert popup._take_lock(rt) is None  # a second popup process gives up quietly
    finally:
        held.release()
    assert popup.launch(config) == "opened"
    assert popup.launch(config, demo=True) == "opened"
    assert no_real_popup == [False, True]
    assert popup.last_state(rt)["launched_at"]


def test_second_popup_process_exits_without_a_window(config: Config, monkeypatch):
    monkeypatch.setattr(popup, "_run", lambda *_a, **_k: pytest.fail("opened a second window"))
    held = runtime.InstanceLock(config.runtime_dir / popup.LOCK)
    held.acquire()
    try:
        monkeypatch.setattr(popup.time, "sleep", lambda _s: None)
        assert popup.main([]) == 0
    finally:
        held.release()


@pytest.mark.parametrize(
    ("pywebview", "webview2", "reason"),
    [
        (False, "1.0", "pywebview isn't installed"),
        (True, None, "the WebView2 runtime isn't installed"),
    ],
)
def test_missing_pywebview_or_webview2_falls_back(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    no_real_popup: list[bool],
    pywebview: bool,
    webview2: str | None,
    reason: str,
):
    monkeypatch.setattr(popup, "pywebview_available", lambda: pywebview)
    monkeypatch.setattr(popup, "webview2_version", lambda: webview2)
    assert popup.launch(config) == f"unavailable: {reason}"
    assert no_real_popup == []
    assert popup.last_state(config.runtime_dir)["error"] == reason
    line, problem = popup.doctor_line(config)
    assert problem and reason in problem and reason in line
    # The popup process itself, if started anyway, records why and exits cleanly.
    (config.runtime_dir / popup.STATE).unlink()
    assert popup.main([]) == 0
    assert popup.last_state(config.runtime_dir)["error"] == reason


def test_doctor_when_the_popup_is_off(config: Config):
    off = config.model_copy(update={"voice": config.voice.model_copy(update={"popup": False})})
    assert popup.doctor_line(off) == ("off ([voice] popup = false)", None)


# --- the view -------------------------------------------------------------------------------


def test_view_from_status():
    assert view_from({"running": False}, paused=False, killed=False).state == "not running"
    assert (
        view_from({"running": True, "state": "ready"}, paused=False, killed=False).state == "idle"
    )
    turn = {"id": "a1", "trigger": "wake", "state": "speaking", "heard": "hi", "reply": "Hello."}
    v = view_from({"running": True, "turn": turn, "other": "ignored"}, paused=True, killed=True)
    assert (v.state, v.turn_id, v.trigger, v.heard, v.reply) == (
        "speaking",
        "a1",
        "wake",
        "hi",
        "Hello.",
    )
    assert v.paused and v.killed
    assert set(v.as_dict()) == {
        "voice_running", "state", "turn_id", "trigger", "heard", "reply",
        "question", "error", "paused", "killed",
    }  # fmt: skip
    err = view_from({"running": True, "error": "hotkey taken"}, paused=False, killed=False)
    assert err.error == "hotkey taken"


def test_read_view_shows_pause_and_kill_switch(config: Config, quiet: None, voice_process: None):
    rt = config.runtime_dir
    rt.mkdir(parents=True, exist_ok=True)
    now = T0
    assert read_view(rt, now) == View(False, "not running")
    v = voice_main.Voice(config)
    v.turn = {"id": "t1", "trigger": "hotkey", "state": "thinking", "heard": "what's up"}
    v.write_status("thinking")
    (rt / "paused").write_text("", "utf-8")
    killswitch.engage(rt, now, "test")
    view = read_view(rt, now)
    assert view.voice_running and view.state == "thinking" and view.heard == "what's up"
    assert view.paused and view.killed


def test_paused_marker(tmp_path: Path):
    assert _paused(tmp_path, T0) is False
    marker = tmp_path / "paused"
    marker.write_text("", "utf-8")
    assert _paused(tmp_path, T0) is True  # until resumed
    marker.write_text((T0 + timedelta(minutes=5)).isoformat(), "utf-8")
    assert _paused(tmp_path, T0) is True
    marker.write_text((T0 - timedelta(minutes=5)).isoformat(), "utf-8")
    assert _paused(tmp_path, T0) is False  # the pause ran out


# --- auto-close and pin ---------------------------------------------------------------------


def turn(state: str, tid: str = "t1") -> View:
    return View(True, state, tid)


def test_autoclose_after_the_reply():
    a = AutoClose(5)
    assert a.step(turn("listening"), 0) == (False, True)  # a new turn
    assert a.step(turn("speaking"), 3) == (False, False)
    assert a.step(turn("done"), 10) == (False, False)  # the timer starts when it's done
    assert a.step(turn("done"), 14.9)[0] is False
    assert a.step(turn("done"), 15)[0] is True


def test_pinned_never_closes_and_unpinning_restarts_the_delay():
    a = AutoClose(5)
    a.step(turn("done"), 0)
    a.pin(True)
    assert a.step(turn("done"), 100)[0] is False
    a.pin(False)
    assert a.step(turn("done"), 101)[0] is False
    assert a.step(turn("done"), 105.5)[0] is False
    assert a.step(turn("done"), 106)[0] is True


def test_a_new_turn_reuses_the_window_and_restarts():
    a = AutoClose(5)
    a.step(turn("done", "t1"), 0)
    assert a.step(turn("listening", "t2"), 4) == (False, True)  # cleared for the new turn
    assert a.step(turn("done", "t2"), 6) == (False, False)
    assert a.step(turn("done", "t2"), 10)[0] is False  # 5 s from t2's end, not t1's
    assert a.step(turn("done", "t2"), 11)[0] is True


def test_voice_stopping_closes_it_too():
    a = AutoClose(5)
    a.step(View(False, "not running"), 0)
    assert a.step(View(False, "not running"), 5)[0] is True


# --- the window's pieces --------------------------------------------------------------------


def test_demo_cycles_every_state_with_fake_data():
    t, states = 0.0, []
    for duration, view in popup.DEMO:
        states.append(view.state)
        assert popup.demo_view(t + duration / 2) == view or duration == 0
        t += duration
    assert {"listening", "thinking", "speaking", "done"} <= set(states)
    assert any(v.question for _d, v in popup.DEMO)
    assert any(v.paused for _d, v in popup.DEMO) and any(v.killed for _d, v in popup.DEMO)
    assert popup.demo_view(t + 60).state == "done"  # then it auto-closes like a real turn


@pytest.mark.parametrize(
    ("corner", "expected"),
    [
        ("bottom-right", (1920 - popup.WIDTH - 20, 1080 - popup.HEIGHT - 20 - popup.TASKBAR)),
        ("top-right", (1920 - popup.WIDTH - 20, 20)),
        ("bottom-left", (20, 1080 - popup.HEIGHT - 20 - popup.TASKBAR)),
        ("top-left", (20, 20)),
    ],
)
def test_position(corner: str, expected: tuple[int, int]):
    assert popup.position(1920, 1080, corner) == expected


def test_page_is_local_only():
    html = popup.page()
    assert "Content-Security-Policy" in html and "default-src 'none'" in html
    assert "connect-src 'none'" in html
    for remote in ("http://", "https://", "//cdn", "<link", 'src="'):
        assert remote not in html  # nothing loaded from anywhere
    for var in ("--bg", "--card", "--accent"):
        assert var in html  # the dashboard's palette


def test_api_close_and_pin_without_a_window(config: Config):
    api = popup.Api(config, demo=True)
    assert api.pin(True) is True and api.pin(False) is False
    api.close()  # no window yet: nothing to do, no error
    data = api.view()
    assert data["state"] == "listening" and data["pinned"] is False and data["new_turn"] is True
