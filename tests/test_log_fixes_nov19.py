"""Fixes from the voice log of 19 Nov: a Google search for "youtube music", the wrong site,
"Playing "YouTube Music"", a reply that left an action out, and a 60 s wait on the app list."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from personal_os.actions import agent, apps, music
from personal_os.actions.base import Origin
from personal_os.config.schema import Config
from personal_os.llm.fake import FakeProvider
from tests.test_actions import Env
from tests.test_agent import Script, answer, use
from tests.test_music import Fake


def test_opening_a_site_by_name_opens_it_not_a_search(home: Path, config: Config, monkeypatch):
    from personal_os.actions.tools import browser as browser_tools

    sent: list[tuple[str, Any]] = []
    monkeypatch.setattr(browser_tools, "_send", lambda _ctx, op, args=None: sent.append((op, args)))
    env = Env(home, config)
    o = env.ex.request("browser_open", {"site": "youtube music"}, origin=Origin.USER)
    assert o.ok and o.message == "Opened Youtube Music"
    assert sent == [("tabs.open", {"url": "https://music.youtube.com/", "newTab": True})]


def test_a_bare_open_beside_play_media_is_dropped():
    class C:
        def __init__(self, tool: str, args: dict[str, Any]) -> None:
            self.tool, self.args = tool, args

    calls = [C("browser_open", {"site": "youtube music", "new_tab": True}),
             C("play_media", {"query": "Blood Sport", "site": "youtube music"})]  # fmt: skip
    assert [c.tool for c in agent.drop_redundant_opens(calls)] == ["play_media"]
    keep = [
        C("browser_open", {"query": "lofi", "site": "youtube"}),
        C("play_media", {"query": "x"}),
    ]
    assert len(agent.drop_redundant_opens(keep)) == 2  # a search is its own request


def test_the_service_you_name_wins_over_the_models_pick():
    assert music.site_in("play blood sport by sleep token on youtube music") == "youtube music"
    assert music.site_in("play it on Spotify") == "spotify"
    assert music.site_in("play the trailer on youtube") == "youtube"
    assert music.site_in("play blood sport") == ""
    assert music.named(music.site_in("x on youtube music") or "youtube", "spotify").key == (
        "youtube_music"
    )


def test_play_media_uses_the_service_in_your_words(home: Path, config: Config, monkeypatch):
    from personal_os.actions.tools import music as music_tool

    b = Fake()
    monkeypatch.setattr(music_tool, "backend", lambda _ctx: b)
    env = Env(home, config)
    o = env.ex.request("play_media", {"query": "blood sport sleep token", "site": "youtube"},
                       origin=Origin.PLANNED,
                       request_text="play blood sport by sleep token on youtube music")  # fmt: skip
    assert o.ok and "on YouTube Music" in o.message
    assert b.done[0][1].startswith("https://music.youtube.com/search")


def test_a_title_the_tab_hasnt_updated_yet_is_replaced_by_what_you_asked_for():
    b = Fake()
    b.open_first = lambda url, sel, reuse: {"title": "YouTube Music"}  # type: ignore[method-assign]
    r = music.play(music.SOURCES["youtube_music"], b, "Raaton Aditya Rikhari")
    assert r.text == 'Playing "Raaton Aditya Rikhari" on YouTube Music'


def test_every_action_is_in_the_reply(home: Path, config: Config, monkeypatch):
    """You asked for music and a reminder; the reply mentioned only one (19 Nov)."""
    from personal_os.actions.tools import music as music_tool

    monkeypatch.setattr(music_tool, "backend", lambda _ctx: Fake())
    env = Env(home, config)
    script = Script(
        use(("play_media", {"query": "Zulfein", "site": "music"}),
            ("set_reminder", {"text": "stretch", "minutes": 20})),
        answer("Playing Zulfein now. Enjoy!"),  # the model's summary leaves the reminder out
    )  # fmt: skip
    r = agent.respond("play zulfein and remind me in 20 minutes to stretch", env.ex,
                      FakeProvider(script))  # fmt: skip
    # Both, in the actions' own words (the fake player answers "Circles").
    assert r.reply.startswith("Playing ") and "remind you at" in r.reply and "stretch" in r.reply
    assert "Enjoy" not in r.reply


def test_the_app_list_never_waits_on_windows(tmp_path: Path, monkeypatch):
    """A stalled Start-menu scan held "play music" for 60 s (17 Nov)."""
    real = apps  # its own functions (_installed_now), not the tests' stand-ins
    slow: list[float] = []

    def stalled() -> list[apps.InstalledApp]:
        slow.append(time.time())
        time.sleep(2)
        return [apps.InstalledApp("YouTube Music", "Brave._crx_x")]

    monkeypatch.setattr(real, "read_installed", stalled)
    monkeypatch.setattr(real, "_memo", None)
    t = time.monotonic()
    assert real._installed_now(tmp_path) == []  # nothing cached yet: none, at once
    assert time.monotonic() - t < 0.5
    deadline = time.monotonic() + 5
    while not (tmp_path / real.CACHE).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert [a.name for a in real._installed_now(tmp_path)] == ["YouTube Music"]  # refreshed
    assert len(slow) == 1


# --- speed: no second model call after plain actions; the prompt read ahead -------------------


def test_actions_alone_need_no_second_model_call(home: Path, config: Config, monkeypatch):
    from personal_os.actions.tools import music as music_tool

    monkeypatch.setattr(music_tool, "backend", lambda _ctx: Fake())
    env = Env(home, config)
    provider = FakeProvider(Script(use(("set_reminder", {"text": "stretch", "minutes": 20}),
                                       ("music", {"action": "play"}))))  # fmt: skip
    r = agent.respond("remind me in 20 minutes to stretch and play music", env.ex, provider)
    assert len(provider.calls) == 1  # one model call (about 2-3 s), not two (+5-7 s)
    assert "stretch" in r.reply and "YouTube Music" in r.reply


def test_a_failed_action_still_gets_a_second_try(home: Path, config: Config):
    env = Env(home, config)
    provider = FakeProvider(Script(use(("open_app", {"app": "nonexistent-app-xyz"})),
                                   final="I couldn't find that app."))  # fmt: skip
    agent.respond("open nonexistent app xyz", env.ex, provider)
    assert len(provider.calls) == 2


class Primer:
    def __init__(self) -> None:
        self.primed: list[str] = []

    def prime(self, system: str) -> float:
        self.primed.append(system)
        return 0.1


def primed(home: Path, config: Config, *, keep: bool, gaming: bool = False) -> list[str]:
    from personal_os.core import modes
    from tests.test_phase7 import voice_for

    cfg = config.model_copy(update={"llm": config.llm.model_copy(update={"keep_on_gpu": keep})})
    p = Primer()
    v, _h = voice_for(Env(home, cfg), provider=p)
    if gaming:
        modes.set_mode(cfg.runtime_dir, "gaming", by="test")
    v.prime_agent()
    deadline = time.monotonic() + 5
    while v._priming.locked() and time.monotonic() < deadline:
        time.sleep(0.02)
    return p.primed


def test_the_agent_prompt_is_read_ahead_when_the_model_stays_on_the_gpu(home: Path, config: Config):
    from personal_os.actions.tools import build_registry  # noqa: F401  (the prompt needs tools)

    got = primed(home, config, keep=True)
    assert len(got) == 1 and "Decide first:" in got[0]  # the agent's own prompt


def test_never_primed_when_the_model_isnt_kept_loaded(home: Path, config: Config):
    assert primed(home, config, keep=False) == []


def test_never_primed_in_gaming_mode(home: Path, config: Config):
    assert primed(home, config, keep=True, gaming=True) == []
