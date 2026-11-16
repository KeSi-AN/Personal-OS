"""Music from the source you chose (actions/music.py, ADR-084): only its players are touched, it
opens where you listen, and Spotify is prepared. A fake backend: no browser, no Windows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from personal_os.actions import music
from personal_os.actions.base import Origin
from personal_os.config.schema import Config
from personal_os.platform.media_sessions import Session
from tests.test_actions import Env

YTM, SPOTIFY = music.SOURCES["youtube_music"], music.SOURCES["spotify"]
YTM_APP = music.App("YouTube Music", "Brave._crx_cinhimbnkkghhklpknlkffjgod")
SPOTIFY_APP = music.App("Spotify", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify")
SPOTIFY_WEB = music.App("Spotify", "Brave._crx_pjibgcllelbfgfagdaldikeohf")


def tab(tid: int, url: str, title: str, audible: bool = False, used: float = 0) -> dict[str, Any]:
    return {"id": tid, "url": url, "title": title, "audible": audible, "lastAccessed": used}


LECTURE = tab(1, "https://www.youtube.com/watch?v=abc", "Linear algebra - YouTube", audible=True)


class Fake:
    """Tabs, media sessions and installed apps as a test sets them; records what's done."""

    def __init__(
        self, tabs: list[dict[str, Any]] | None = None, sessions: list[Session] | None = None,
        installed: list[music.App] | None = None,
    ) -> None:  # fmt: skip
        self._tabs = list(tabs or [])
        self._sessions = list(sessions or [])
        self._installed = list(installed or [])
        self.done: list[tuple[Any, ...]] = []
        self.app_opens_tab: dict[str, Any] | None = None  # launching the web app opens this

    def tabs(self) -> list[dict[str, Any]]:
        return list(self._tabs)

    def tab_media(self, tab_id: int, action: str) -> dict[str, Any] | None:
        t = next(t for t in self._tabs if t["id"] == tab_id)
        if action != "status":
            self.done.append(("tab", tab_id, action))
            if action in ("play", "pause", "toggle"):
                t["audible"] = action == "play" or (action == "toggle" and not t["audible"])
        return {"title": t["title"], "artist": "Artist", "playing": t["audible"]}

    def sessions(self) -> list[Session]:
        return list(self._sessions)

    def session_control(self, app_id: str, action: str) -> Session | None:
        self.done.append(("app", app_id, action))
        s = next((s for s in self._sessions if s.app_id == app_id), None)
        return None if s is None else Session(s.app_id, "Next song", s.artist, True)

    def installed(self) -> list[music.App]:
        return list(self._installed)

    def launch(self, argv: list[str]) -> None:
        self.done.append(("launch", argv[-1]))
        if self.app_opens_tab is not None:
            self._tabs.append(self.app_opens_tab)

    def open_tab(self, url: str) -> None:
        self.done.append(("open", url))

    def open_first(self, url: str, selector: str, reuse: str) -> dict[str, Any]:
        self.done.append(("open_first", url, reuse))
        return {"title": "Circles - YouTube Music"}

    def wait(self, seconds: float) -> None:
        pass


# --- which source ---------------------------------------------------------------------------


def test_the_source_from_config_and_from_what_you_name():
    assert music.chosen("youtube_music") is YTM and music.chosen("any") is None
    assert music.named("music", "spotify") is SPOTIFY  # "music": the one you chose
    assert music.named("", "any") is YTM  # nothing chosen: YouTube Music
    assert music.named("youtube", "spotify").key == "youtube"  # a video: YouTube itself
    assert music.named("Spotify", "youtube_music") is SPOTIFY


# --- only your source -------------------------------------------------------------------------


def test_pause_touches_your_music_never_a_lecture_in_another_tab():
    """It showed whichever media was available (16 Nov)."""
    ytm = tab(2, "https://music.youtube.com/watch?v=x", "Circles - YouTube Music", audible=True)
    b = Fake(tabs=[LECTURE, ytm])
    assert music.control(YTM, b, "pause") == music.Reply("Paused YouTube Music.", True)
    assert b.done == [("tab", 2, "pause")]


def test_nothing_from_your_source_means_nothing_is_paused():
    b = Fake(tabs=[LECTURE], sessions=[Session("Brave", "Linear algebra", "", True)])
    r = music.control(YTM, b, "pause")
    assert r == music.Reply("Nothing's playing on YouTube Music.", False) and b.done == []


def test_its_installed_app_is_found_by_its_session_not_a_browser_session():
    sessions = [Session("Brave", "a lecture", "", True),
                Session(YTM_APP.app_id, "Baarish Mein Phir", "Saahel", False)]  # fmt: skip
    b = Fake(sessions=sessions, installed=[YTM_APP])
    (p,) = music.players(YTM, b)
    assert (p.kind, p.ref, p.title) == ("app", YTM_APP.app_id, "Baarish Mein Phir")
    assert music.control(YTM, b, "next").text == "Next on YouTube Music: Next song."
    assert b.done == [("app", YTM_APP.app_id, "next")]


def test_the_one_playing_wins_then_the_tab_used_last():
    old = tab(2, "https://music.youtube.com/", "YouTube Music", used=10)
    new = tab(3, "https://music.youtube.com/", "YouTube Music", used=20)
    playing = tab(4, "https://music.youtube.com/watch", "Song | YouTube Music", audible=True)
    assert music.choose(music.players(YTM, Fake(tabs=[old, new]))).ref == 3  # type: ignore[union-attr]
    assert music.choose(music.players(YTM, Fake(tabs=[old, playing, new]))).ref == 4  # type: ignore[union-attr]


def test_whats_playing_reads_the_page_and_tidies_the_title():
    b = Fake(tabs=[LECTURE, tab(2, "https://music.youtube.com/w", "Aankhon Mein | YouTube Music",
                                audible=True)])  # fmt: skip
    now = music.now(YTM, b)
    assert (now.title, now.artist, now.playing, now.where) == (
        "Aankhon Mein",
        "Artist",
        True,
        "browser",
    )
    assert now.said() == "Aankhon Mein by Artist on YouTube Music (playing)"
    assert music.now(YTM, Fake(tabs=[LECTURE])).said() == "YouTube Music isn't open."
    assert music.playing_now(YTM, b) and not music.playing_now(YTM, Fake(tabs=[LECTURE]))


# --- "play music" with nothing open -----------------------------------------------------------


def test_play_with_nothing_open_opens_its_app_if_installed_else_the_website():
    b = Fake(installed=[YTM_APP])
    r = music.control(YTM, b, "play")
    assert r.text == "Opened YouTube Music in the app. What should I play?"
    assert b.done == [("launch", f"shell:AppsFolder\\{YTM_APP.app_id}")]
    b = Fake(installed=[YTM_APP])
    music.control(YTM, b, "play", open_in="browser")
    assert b.done == [("open", "https://music.youtube.com/")]
    b = Fake()
    assert "in the browser" in music.control(YTM, b, "play").text


def test_a_default_search_plays_when_nothing_is_open():
    b = Fake()
    r = music.control(YTM, b, "play", default_query="lofi")
    assert r.text == 'Playing "Circles" on YouTube Music'
    assert b.done == [
        ("open_first", "https://music.youtube.com/search?q=lofi", "music.youtube.com")
    ]


# --- a song by name ---------------------------------------------------------------------------


def test_a_song_plays_in_the_youtube_music_app_when_that_is_where_you_listen():
    b = Fake(installed=[YTM_APP])
    b.app_opens_tab = tab(7, "https://music.youtube.com/", "YouTube Music")
    r = music.play(YTM, b, "Circles Post Malone")
    assert r.ok and r.text == 'Playing "Circles" on YouTube Music'
    assert [d[0] for d in b.done] == ["launch", "open_first"]  # its tab is the one reused
    assert b.done[1][2] == "music.youtube.com"


def test_a_song_reuses_an_open_tab_without_starting_the_app():
    b = Fake(tabs=[tab(2, "https://music.youtube.com/", "YouTube Music")], installed=[YTM_APP])
    music.play(YTM, b, "Circles")
    assert [d[0] for d in b.done] == ["open_first"]


def test_spotify_opens_its_search_in_its_app_and_says_it_cant_start_the_song_yet():
    b = Fake(installed=[SPOTIFY_WEB, SPOTIFY_APP])
    r = music.play(SPOTIFY, b, "Blinding Lights")
    assert b.done == [("launch", "spotify:search:Blinding%20Lights")]  # the app, not the web app
    assert "the Spotify app" in r.text and "press play on the top result" in r.text
    b = Fake()
    music.play(SPOTIFY, b, "Blinding Lights")
    assert b.done == [("open", "https://open.spotify.com/search/Blinding%20Lights")]


def test_spotify_app_is_paused_and_skipped_through_its_media_session():
    b = Fake(sessions=[Session(SPOTIFY_APP.app_id, "Song", "Artist", True)],
             installed=[SPOTIFY_APP])  # fmt: skip
    assert music.control(SPOTIFY, b, "pause").ok
    assert b.done == [("app", SPOTIFY_APP.app_id, "pause")]


# --- the tools, voice and the island ----------------------------------------------------------


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    b = Fake()
    monkeypatch.setattr(music, "LiveBackend", lambda *_a, **_k: b)
    from personal_os.actions.tools import music as music_tool

    monkeypatch.setattr(music_tool, "backend", lambda _ctx: b)
    return b


def test_the_music_tool_acts_on_your_source_only(home: Path, config: Config, fake: Fake):
    fake._tabs = [LECTURE, tab(2, "https://music.youtube.com/w", "Song | YouTube Music", True)]
    env = Env(home, config)
    o = env.ex.request("music", {"action": "pause"}, origin=Origin.USER)
    assert o.ok and o.message == "Paused YouTube Music." and fake.done == [("tab", 2, "pause")]
    fake._tabs = [LECTURE]
    o = env.ex.request("music", {"action": "next"}, origin=Origin.USER)
    assert not o.ok and "Nothing's playing on YouTube Music" in o.message


def test_play_media_and_whats_playing_follow_the_source(home: Path, config: Config, fake: Fake):
    cfg = config.model_copy(update={"music": config.music.model_copy(update={"source": "spotify"})})
    env = Env(home, cfg)
    o = env.ex.request("play_media", {"query": "Levitating"}, origin=Origin.USER)
    assert o.ok and fake.done == [("open", "https://open.spotify.com/search/Levitating")]
    o = env.ex.request("whats_playing", {}, origin=Origin.USER)
    assert (
        o.ok and o.message == "Spotify isn't open." and o.result is not None and o.result.captured
    )


def test_voice_pause_is_your_music_and_never_the_media_key(home: Path, config: Config, fake: Fake):
    from tests.test_phase7 import voice_for

    fake._tabs = [LECTURE]
    v, _h = voice_for(Env(home, config))
    assert v.music_control("pause", "pause the music") == "Nothing's playing on YouTube Music."
    assert v.music_control("pause", "pause the video") is None  # a video: the browser tab
    v.cfg = config.model_copy(update={"music": config.music.model_copy(update={"source": "any"})})
    assert v.music_control("pause", "pause") is None  # "any": as before


def test_the_island_music_page(config: Config, fake: Fake):
    from personal_os.interfaces import island_data

    d = island_data.music(config, fake)
    assert d["open"] is False and d["source"] == "YouTube Music"
    assert island_data.music_action(config, "open", fake)["text"] == (
        "Opened YouTube Music in the browser."
    )
    fake._tabs = [tab(2, "https://music.youtube.com/w", "Song | YouTube Music", True)]
    d = island_data.music(config, fake)
    assert (d["open"], d["title"], d["playing"]) == (True, "Song", True)
    assert island_data.music_action(config, "pause", fake) == {
        "ok": True, "text": "Paused YouTube Music.",
    }  # fmt: skip
