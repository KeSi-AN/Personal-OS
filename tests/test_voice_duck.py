"""Music turned down while Donna listens and talks; "close the tab" closes the tab in front."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from personal_os.actions.base import Origin
from personal_os.config.schema import Config
from personal_os.voice.duck import FILE, Ducker
from tests.test_actions import Env
from tests.test_phase7 import voice_for


class Volume:
    def __init__(self, level: float) -> None:
        self.level = level

    def GetMasterVolume(self) -> float:
        return self.level

    def SetMasterVolume(self, level: float, _ctx: Any) -> None:
        self.level = level


class Proc:
    def __init__(self, pid: int, name: str) -> None:
        self.pid, self._name = pid, name

    def name(self) -> str:
        return self._name


class Session:
    def __init__(self, pid: int, name: str, level: float) -> None:
        self.Process = Proc(pid, name)
        self.SimpleAudioVolume = Volume(level)


def test_other_apps_are_turned_down_for_a_turn_and_back_after(tmp_path: Path):
    brave, spotify, quiet = (
        Session(1, "brave.exe", 0.8),
        Session(2, "Spotify.exe", 1.0),
        Session(3, "x.exe", 0.0),
    )
    d = Ducker(tmp_path, 0.2, sessions=lambda: [brave, spotify, quiet])
    with d.turn():
        assert brave.SimpleAudioVolume.level == pytest.approx(0.16)
        assert spotify.SimpleAudioVolume.level == pytest.approx(0.2)
        assert quiet.SimpleAudioVolume.level == 0.0  # muted apps are left alone
        assert (tmp_path / FILE).exists()  # kept, in case voice dies mid-turn
        with d.turn():  # nested: still turned down once, not 0.2 x 0.2
            assert brave.SimpleAudioVolume.level == pytest.approx(0.16)
    assert brave.SimpleAudioVolume.level == 0.8 and spotify.SimpleAudioVolume.level == 1.0
    assert not (tmp_path / FILE).exists()


def test_volumes_a_crash_left_down_are_restored_at_start(tmp_path: Path):
    brave = Session(1, "brave.exe", 0.8)
    Ducker(tmp_path, 0.2, sessions=lambda: [brave]).duck()  # ...and voice dies here
    assert brave.SimpleAudioVolume.level == pytest.approx(0.16)
    Ducker(tmp_path, 0.2, sessions=lambda: [brave]).recover()
    assert brave.SimpleAudioVolume.level == 0.8 and not (tmp_path / FILE).exists()


def test_ducking_off(tmp_path: Path):
    brave = Session(1, "brave.exe", 0.8)
    with Ducker(tmp_path, 1.0, sessions=lambda: [brave]).turn():
        assert brave.SimpleAudioVolume.level == 0.8


def test_a_whole_conversation_is_ducked(home: Path, config: Config):
    brave = Session(1, "brave.exe", 0.5)
    v, _h = voice_for(Env(home, config), "what time is it")
    v.ducker.sessions = lambda: [brave]
    seen: list[float] = []
    real = v.respond

    def respond(trigger: str, text: str | None = None) -> bool:
        seen.append(brave.SimpleAudioVolume.level)
        return real(trigger, text)

    v.respond = respond  # type: ignore[method-assign]
    v.converse("hotkey")
    assert seen and all(x == pytest.approx(0.1) for x in seen)
    assert brave.SimpleAudioVolume.level == 0.5


@pytest.mark.parametrize("which", ["current", "this tab", "", "the tab", "Current."])
def test_the_tab_in_front_is_the_current_tab(
    home: Path, config: Config, monkeypatch: pytest.MonkeyPatch, which: str
):
    from personal_os.browser import control

    tabs = [
        {"id": 1, "title": "Docs", "url": "https://docs.python.org", "active": False},
        {"id": 2, "title": "Marana Mass - YouTube", "url": "https://youtube.com/w", "active": True},
    ]
    sent: list[tuple[str, Any]] = []

    def send(_rt: Any, op: str, args: Any = None, **_k: Any) -> Any:
        sent.append((op, args))
        return tabs if op == "tabs.list" else None

    monkeypatch.setattr(control, "send", send)
    env = Env(home, config)
    out = env.ex.request("browser_tab", {"action": "close", "which": which}, origin=Origin.USER)
    assert out.ok, out.message
    assert sent[-1] == ("tabs.close", {"tabIds": [2]})


def test_close_the_tab_needs_no_model():
    from personal_os.voice import router
    from tests.test_voice_fastpath import INDEX

    for said in ("Close the tab.", "close this tab", "close tab"):
        intent = router.route(said, INDEX)
        assert intent is not None and intent.calls == [("browser_tab", {"action": "close"})]
