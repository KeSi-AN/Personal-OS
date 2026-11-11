"""Modes: work, gaming, chill and what each one changes."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from personal_os.actions.base import Origin
from personal_os.config.schema import Config
from personal_os.context.gate import JobGate
from personal_os.core import modes
from tests.conftest import T0
from tests.test_actions import Env


def test_switching_and_aliases(tmp_path: Path):
    assert modes.current(tmp_path) == "work"  # the default
    assert modes.normalize("Leisure mode") == "chill" and modes.normalize("games") == "gaming"
    assert modes.normalize("party") is None
    assert modes.set_mode(tmp_path, "gaming", by="voice", now=T0) is True
    assert modes.set_mode(tmp_path, "gaming", by="voice", now=T0) is False
    st = modes.state(tmp_path)
    assert (st["mode"], st["by"], st["previous"]) == ("gaming", "voice", "work")
    assert [h["mode"] for h in modes.history(tmp_path, T0 - timedelta(hours=1))] == ["gaming"]


def test_auto_gaming_comes_and_goes(tmp_path: Path):
    modes.set_mode(tmp_path, "chill", by="voice", now=T0)
    assert modes.auto_gaming_step(tmp_path, True, T0) == "entered"
    assert modes.current(tmp_path) == "gaming"
    assert modes.auto_gaming_step(tmp_path, False, T0 + timedelta(minutes=1)) == ""  # not yet
    assert modes.auto_gaming_step(tmp_path, False, T0 + timedelta(minutes=4)) == "left"
    assert modes.current(tmp_path) == "chill"  # back to what you had


def test_auto_gaming_never_undoes_your_choice(tmp_path: Path):
    modes.set_mode(tmp_path, "gaming", by="voice", now=T0)  # you chose it
    assert modes.auto_gaming_step(tmp_path, False, T0 + timedelta(hours=1)) == ""
    assert modes.current(tmp_path) == "gaming"


def test_gaming_pauses_background_model_work(tmp_path: Path, config: Config):
    gate = JobGate(config.llm, idle_seconds=lambda: 10_000, fullscreen=lambda: False,
                   paused=lambda: False, ac_power=lambda: True,
                   mode_allows=lambda: modes.effects(tmp_path).background_jobs)  # fmt: skip
    assert gate.check().ok
    modes.set_mode(tmp_path, "gaming", by="auto", now=T0)
    st = gate.check()
    assert not st.ok and "gaming" in st.reason


def test_effects_are_practical():
    g, w, c = modes.EFFECTS["gaming"], modes.EFFECTS["work"], modes.EFFECTS["chill"]
    assert (g.wake_word, g.popup, g.speak_announcements, g.followup, g.background_jobs, g.unload_model) == (
        False, False, False, False, False, True,
    )  # fmt: skip
    assert w.nudge_chill_min == 15 and c.nudge_chill_min == 0 and c.wake_word is True


def test_set_mode_tool_and_undo(home: Path, config: Config):
    env = Env(home, config)
    o = env.ex.request("set_mode", {"mode": "leisure"}, origin=Origin.USER)
    assert o.ok and o.message.startswith("Chill mode.")
    assert modes.current(env.config.runtime_dir) == "chill"
    assert env.ex.undo(o.action_id).ok and modes.current(env.config.runtime_dir) == "work"
    assert not env.ex.request("set_mode", {"mode": "party"}, origin=Origin.USER).ok


def test_voice_obeys_gaming_mode(home: Path, config: Config, monkeypatch: Any):
    from personal_os.voice import main as voice_main
    from tests.test_phase7 import voice_for

    env = Env(home, config)
    v, h = voice_for(env)
    env.config.runtime_dir.mkdir(parents=True, exist_ok=True)
    modes.set_mode(env.config.runtime_dir, "gaming", by="auto", now=T0)
    launched: list[Any] = []
    monkeypatch.setattr(voice_main, "_launch_popup", launched.append)
    v.launch_popup()
    assert launched == []  # no window over the game
    monkeypatch.setattr(voice_main, "status", lambda _rt: {"running": True})
    voice_main.queue_speech(env.config.runtime_dir, "Reminder: stretch")
    v.speak_queued()
    assert h.said == []  # not said over the game


def test_work_mode_nudges_when_chill_takes_over(home: Path, config: Config, monkeypatch: Any):
    from personal_os import daemon
    from personal_os.context import activity
    from personal_os.context import now as now_mod
    from personal_os.interfaces import alerts

    env = Env(home, config)
    shown: list[str] = []
    monkeypatch.setattr(alerts, "show", lambda _c, _i, title, text, _at: shown.append(text) or True)
    monkeypatch.setattr(now_mod, "snapshot", lambda *_a, **_k: now_mod.Now(at=T0))
    b = activity.Breakdown(T0, T0)
    b.by_category["chill"] = 17 * 60_000
    b.by_site["youtube.com"] = 17 * 60_000
    monkeypatch.setattr(activity, "breakdown", lambda *_a, **_k: b)
    spoken: list[str] = []
    fake = SimpleNamespace(
        config=env.config, clock=env.clock, control_conn=env.conn, llm=None, last_nudge=None,
        _fresh_config=lambda: env.config, _speak=lambda _c, t: spoken.append(t),
    )  # fmt: skip
    fake._nudge = lambda m, bb: daemon.Daemon._nudge(fake, m, bb)  # type: ignore[attr-defined]
    daemon.Daemon._mode_tick(fake)  # type: ignore[arg-type]
    assert shown and "17 min of chill (youtube.com) in work mode" in shown[0]
    daemon.Daemon._mode_tick(fake)  # type: ignore[arg-type]
    assert len(shown) == 1  # not again within 30 minutes
    modes.set_mode(env.config.runtime_dir, "chill", by="voice", now=T0)
    fake.last_nudge = None
    daemon.Daemon._mode_tick(fake)  # type: ignore[arg-type]
    assert len(shown) == 1  # chill mode: no nudges
