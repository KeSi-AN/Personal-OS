"""Music from the one source you chose (config [music], ADR-084).

"Play music", pause, next, previous, "play <song>", what's playing, the island's Music page and
its playing bars all use only that source. A YouTube lecture in another tab, or a video
someone sent you, is left alone: "pause" means your music, not whatever Windows picks.

A source has players, found two ways:
- its website's tabs in your browser, through the extension (music.youtube.com,
  open.spotify.com), controlled tab by tab;
- its installed app, through Windows' media sessions: Spotify's app, or YouTube Music
  installed from the browser (its session carries the app's id, so it's told apart from a
  browser tab or another app).

`open_in` says where to start it when nothing is open: the installed app if there is one
("auto"), always the app, or always the website.

Spotify is prepared, not finished: its app or website can be opened, searched, paused,
skipped and read, but starting a particular song needs Spotify's own sign-in (its Web API),
which isn't set up, so "play <song>" opens Spotify's search for it and says so.

The browser, Windows and app launching come in through a Backend, so this is tested without
any of them (tests/test_music.py).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import quote, urlparse

from personal_os.browser import recipes
from personal_os.platform.media_sessions import Session

Action = Literal["play", "pause", "toggle", "next", "previous"]
ACTIONS = ("play", "pause", "toggle", "next", "previous")
APP_WAIT_S = 8.0  # an installed web app's tab takes a moment to appear after it's launched


@dataclass(frozen=True)
class Source:
    key: str
    name: str
    hosts: tuple[str, ...]  # its website, as tabs show it
    home: str
    site: str = ""  # recipes.Site that searches it and opens the first result (plays it)
    search: str = ""  # a search page with {q}, where a song can't be started for you
    apps: tuple[str, ...] = ()  # its installed app's name in the Start menu
    app_search: str = ""  # its app's own search link with {q} (spotify:search:...)


SOURCES = {
    "youtube_music": Source(
        "youtube_music", "YouTube Music", ("music.youtube.com",), "https://music.youtube.com/",
        site="youtube music", apps=("YouTube Music",),
    ),
    "spotify": Source(
        "spotify", "Spotify", ("open.spotify.com",), "https://open.spotify.com/",
        search="https://open.spotify.com/search/{q}", apps=("Spotify",),
        app_search="spotify:search:{q}",
    ),
    "youtube": Source(
        "youtube", "YouTube", ("www.youtube.com", "youtube.com", "m.youtube.com"),
        "https://www.youtube.com/", site="youtube", apps=("YouTube",),
    ),
}  # fmt: skip
# What "play X on <site>" can name, beside "music" (= the source you chose).
SITE_NAMES = {"youtube music": "youtube_music", "youtube": "youtube", "spotify": "spotify"}


_SAID_SITE = re.compile(
    r"\b(?:on|in|from|using|with|via)\s+(?:the\s+)?(youtube\s*music|yt\s*music|ytmusic|spotify|"
    r"youtube|yt)\b",
    re.I,
)


def site_in(text: str) -> str:
    """The service your words name ("... on YouTube Music", "from Spotify"), as play_media
    names it, or "" when they name none."""
    m = _SAID_SITE.search(text or "")
    if not m:
        return ""
    s = re.sub(r"\s+", " ", m[1].lower())
    if "music" in s:
        return "youtube music"
    return "spotify" if s == "spotify" else "youtube"


def chosen(source: str) -> Source | None:
    """The source from config, or None for "any" (whatever plays in the browser)."""
    return SOURCES.get(source)


def named(site: str, source: str) -> Source:
    """The source "play X on <site>" means: "music" (or nothing) is the one you chose (YouTube
    Music when that's "any"); a site you name is that site."""
    key = SITE_NAMES.get(site.lower().strip())
    if key:
        return SOURCES[key]
    return chosen(source) or SOURCES["youtube_music"]


# --- "play <song>" by rules -------------------------------------------------------------------

_PLAY_SONG = re.compile(
    r"^(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?(?:go\s+ahead\s+and\s+)?play\s+"
    r"(?:the\s+song\s+|the\s+track\s+|song\s+|track\s+|music\s*[-:,]\s*)?(?P<q>.+?)"
    r"(?:\s+(?:on|in|from|using|via)\s+(?:the\s+)?(?P<site>youtube\s*music|yt\s*music|"
    r"ytmusic|youtube|yt|spotify|my\s+music(?:\s+app)?|music))?"
    r"(?:\s+(?:there|on\s+it|in\s+it|for\s+me|please|now|donna))*\W*$",
    re.I,
)
# "Play" with these isn't a song: the music itself ("play music"), a video or a show, or the
# player's own controls. Those stay with the router and the agent.
_NOT_A_SONG = re.compile(
    r"^(?:(?:some|my|the|a|any)\s+)?(?:music|songs?|tunes?|tracks?|something(?:\s+\w+)?|"
    r"anything|it|that|this|them|again|it again|that again|next(?:\s+\w+)?|"
    r"(?:the\s+)?(?:next|previous|last)\s+\w+|previous|music\s+again)$|"
    r"\b(?:video|videos|episode|movie|film|show|series|podcast|trailer|the tab)\b",
    re.I,
)
_OPEN_SOURCE = re.compile(
    r"^(?:open|launch|start|go\s+to|bring\s+up)\s+(?:the\s+)?(youtube\s*music|yt\s*music|"
    r"spotify)(?:\s+app)?$",
    re.I,
)


def song_request(text: str) -> tuple[str, str] | None:
    """ "play blood sport by sleep token on youtube music" -> ("blood sport by sleep token",
    "youtube music"); "music" when no service is named (your chosen source). None when it isn't
    a song by name ("play music", "play the next episode", "play that video"). The model did
    this (4-5 s, and once it opened YouTube and searched by hand: 20 Nov bench)."""
    from personal_os.context.route import normalize, strip_polite, strip_wake

    t = strip_polite(normalize(strip_wake(text))).strip()
    m = _PLAY_SONG.match(t)
    if m is None:
        return None
    query = m["q"].strip(" ,.-:'\"")
    if len(query) < 2 or _NOT_A_SONG.search(query):
        return None
    said = re.sub(r"\s+", " ", (m["site"] or "").lower())
    if not said or said.startswith(("my music", "music")):
        site = "music"
    elif "music" in said:
        site = "youtube music"
    else:
        site = "spotify" if said == "spotify" else "youtube"
    return query, site


def open_then_play(parts: list[str]) -> list[str]:
    """ "open youtube music, and play blood sport there" -> ["play blood sport on youtube
    music"]: play_media opens the service itself (its app or a tab), so opening it first
    only starts it twice. Other parts are left as they are."""
    out: list[str] = []
    i = 0
    while i < len(parts):
        opened = _OPEN_SOURCE.match(parts[i].strip())
        nxt = song_request(parts[i + 1]) if opened and i + 1 < len(parts) else None
        if opened and nxt is not None and nxt[1] == "music":
            out.append(f"play {nxt[0]} on {re.sub(r'\s+', ' ', opened[1].lower())}")
            i += 2
            continue
        out.append(parts[i])
        i += 1
    return out


# --- players ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class App:
    name: str
    app_id: str

    @property
    def web(self) -> bool:
        """Installed from a browser (Brave/Chrome/Edge "install this site as an app"): its
        tab is the extension's to drive, like any tab."""
        return "_crx_" in self.app_id

    def argv(self) -> list[str]:
        return ["explorer.exe", f"shell:AppsFolder\\{self.app_id}"]


@dataclass(frozen=True)
class Player:
    kind: Literal["tab", "app"]
    ref: int | str  # a tab's id, or a media session's app id
    title: str
    artist: str
    playing: bool
    recent: float = 0.0  # a tab's last use, for "the one you used last"


class Backend(Protocol):
    def tabs(self) -> list[dict[str, Any]]: ...
    def tab_media(self, tab_id: int, action: str) -> dict[str, Any] | None: ...
    def sessions(self) -> list[Session]: ...
    def session_control(self, app_id: str, action: str) -> Session | None: ...
    def installed(self) -> list[App]: ...
    def launch(self, argv: list[str]) -> None: ...
    def open_tab(self, url: str) -> None: ...
    def open_first(self, url: str, selector: str, reuse: str) -> dict[str, Any]: ...
    def wait(self, seconds: float) -> None: ...


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _tidy(title: str, src: Source) -> str:
    for name in (src.name, "YouTube Music", "YouTube", "Spotify"):
        for sep in (" - ", " | ", " – "):
            title = title.removesuffix(sep + name)
    return title.strip()


def apps_for(src: Source, b: Backend) -> list[App]:
    """The source's installed apps: web apps first for YouTube (their tab can be driven),
    Spotify's own app first for Spotify."""
    names = {n.lower() for n in src.apps}
    found = [a for a in b.installed() if a.name.lower() in names]
    native_first = src.key == "spotify"
    return sorted(found, key=lambda a: a.web if native_first else not a.web)


def players(src: Source, b: Backend) -> list[Player]:
    out = [
        Player("tab", int(t["id"]), _tidy(str(t.get("title") or ""), src), "",
               bool(t.get("audible")), float(t.get("lastAccessed") or 0))
        for t in b.tabs()
        if _host(str(t.get("url") or "")) in src.hosts and t.get("id") is not None
    ]  # fmt: skip
    mine = {a.app_id for a in apps_for(src, b)}
    out += [Player("app", s.app_id, s.title, s.artist, s.playing) for s in b.sessions()
            if s.app_id in mine]  # fmt: skip
    return out


def choose(found: list[Player]) -> Player | None:
    """The one playing; else the tab used last; else its app."""
    if not found:
        return None
    playing = [p for p in found if p.playing]
    if playing:
        return playing[0]
    tabs = sorted((p for p in found if p.kind == "tab"), key=lambda p: -p.recent)
    return tabs[0] if tabs else found[0]


# --- what's playing --------------------------------------------------------------------------


@dataclass(frozen=True)
class Now:
    source: str  # its name ("YouTube Music")
    open: bool  # one of its players is there
    title: str = ""
    artist: str = ""
    playing: bool = False
    where: str = ""  # "app" or "browser"

    def said(self) -> str:
        if not self.open:
            return f"{self.source} isn't open."
        what = " by ".join(x for x in (self.title, self.artist) if x) or "nothing chosen yet"
        return f"{what} on {self.source} ({'playing' if self.playing else 'paused'})"


def now(src: Source, b: Backend) -> Now:
    p = choose(players(src, b))
    if p is None:
        return Now(src.name, False)
    title, artist = p.title, p.artist
    if p.kind == "tab":  # the page knows the song; the tab's title may be the site's name
        st = b.tab_media(int(p.ref), "status") or {}
        title = _tidy(str(st.get("title") or title), src)
        artist = str(st.get("artist") or "")
        playing = bool(st.get("playing", p.playing))
    else:
        playing = p.playing
    if title == src.name:
        title = ""
    return Now(src.name, True, title, artist, playing, "browser" if p.kind == "tab" else "app")


def playing_now(src: Source, b: Backend) -> bool:
    """Cheap: is one of the source's players making sound (for the island's bars)?"""
    return any(p.playing for p in players(src, b))


# --- doing ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    text: str
    ok: bool


def open_source(src: Source, b: Backend, open_in: str) -> str:
    """Start the source where `open_in` says: "the app" or "the browser"."""
    app = None if open_in == "browser" else next(iter(apps_for(src, b)), None)
    if app is not None:
        b.launch(app.argv())
        return "the app"
    b.open_tab(src.home)
    return "the browser"


def control(
    src: Source, b: Backend, action: str, *, open_in: str = "auto", default_query: str = ""
) -> Reply:
    """Play, pause, toggle, next or previous, on the source's player only. "Play" with nothing
    open starts it (the default search, or the source itself and a question)."""
    if action not in ACTIONS:
        return Reply(f"I can't {action} music.", False)
    p = choose(players(src, b))
    if p is None:
        if action in ("play", "toggle"):
            if default_query:
                return play(src, b, default_query, open_in=open_in)
            where = open_source(src, b, open_in)
            return Reply(f"Opened {src.name} in {where}. What should I play?", True)
        return Reply(f"Nothing's playing on {src.name}.", False)
    if action == "play" and p.playing:
        return Reply(f"{src.name} is already playing.", True)
    if action == "pause" and not p.playing:
        return Reply(f"{src.name} is already paused.", True)
    if p.kind == "tab":
        st = b.tab_media(int(p.ref), action)
        title = _tidy(str((st or {}).get("title") or p.title), src)
    else:
        sess = b.session_control(str(p.ref), action)
        st = None if sess is None else {"playing": sess.playing}
        title = sess.title if sess is not None else p.title
    if st is None:
        return Reply(f"{src.name} didn't respond.", False)
    if title == src.name:
        title = ""
    if action in ("next", "previous"):
        return Reply(f"{'Next' if action == 'next' else 'Previous'} on {src.name}"
                     + (f": {title}." if title else "."), True)  # fmt: skip
    playing = bool(st.get("playing")) if action == "toggle" else action == "play"
    return Reply(f"{'Playing' if playing else 'Paused'} {src.name}.", True)


def play(src: Source, b: Backend, query: str, *, open_in: str = "auto") -> Reply:
    """A song by name, from the source: YouTube Music and YouTube search and start the first
    result (in the installed web app when that's where you listen); Spotify opens its search."""
    query = " ".join(query.split())
    if src.site:
        site = recipes.find(src.site)
        assert site is not None and site.first
        host = src.hosts[0]
        have_tab = any(_host(str(t.get("url") or "")) in src.hosts for t in b.tabs())
        web_apps = [a for a in apps_for(src, b) if a.web] if open_in != "browser" else []
        if not have_tab and web_apps:
            b.launch(web_apps[0].argv())  # its window's tab is the one the song goes to
            waited = 0.0
            while waited < APP_WAIT_S and not any(
                _host(str(t.get("url") or "")) in src.hosts for t in b.tabs()
            ):
                b.wait(0.5)
                waited += 0.5
        data = b.open_first(recipes.search_url(site, query), site.first, host)
        title = _tidy(str((data or {}).get("title") or ""), src)
        if not title or title == src.name:
            # The tab still had the site's own title ("Playing "YouTube Music"", 17 Nov):
            # say what you asked for instead.
            title = query
        return Reply(f'Playing "{title}" on {src.name}', True)
    # Spotify: open its search, in its app if that's where you listen.
    app = None if open_in == "browser" else next(iter(apps_for(src, b)), None)
    if app is not None and not app.web and src.app_search:
        b.launch(["explorer.exe", src.app_search.format(q=quote(query))])
        where = "the Spotify app"
    else:
        b.open_tab(src.search.format(q=quote(query)))
        where = "the browser"
    return Reply(
        f'Opened {src.name} on a search for "{query}" in {where}. I can\'t start a {src.name} '
        "song by myself yet, so press play on the top result.",
        True,
    )


# --- the real backend ------------------------------------------------------------------------


class LiveBackend:
    """The browser through the extension, Windows' media sessions, the Start menu's apps."""

    def __init__(
        self, runtime_dir: Path, browser: str, launch: Callable[[list[str]], Any] | None = None,
        cancel: Any = None,
    ) -> None:  # fmt: skip
        self.runtime_dir, self.browser, self._launch, self.cancel = (
            runtime_dir, browser, launch, cancel,
        )  # fmt: skip

    def _send(self, op: str, args: dict[str, Any], timeout_s: float = 10) -> Any:
        from personal_os.browser import control

        return control.send(self.runtime_dir, op, args, browser=self.browser,
                            timeout_s=timeout_s, cancel=self.cancel)  # fmt: skip

    def tabs(self) -> list[dict[str, Any]]:
        from personal_os.browser import control

        if not control.connected(self.runtime_dir):
            return []
        try:
            found = self._send("tabs.list", {}, timeout_s=4)
        except Exception:
            return []
        return [t for t in found if isinstance(t, dict)] if isinstance(found, list) else []

    def tab_media(self, tab_id: int, action: str) -> dict[str, Any] | None:
        try:
            return dict(self._send("media", {"action": action, "tabId": tab_id}, 5) or {})
        except Exception:
            return None

    def sessions(self) -> list[Session]:
        from personal_os.platform import media_sessions

        return media_sessions.sessions()

    def session_control(self, app_id: str, action: str) -> Session | None:
        from personal_os.platform import media_sessions

        return media_sessions.control(app_id, action)

    def installed(self) -> list[App]:
        from personal_os.actions import apps

        try:  # never waits on Windows' list: a stalled scan once held "play music" 60 s
            return [App(a.name, a.app_id) for a in apps.installed_now(self.runtime_dir)]
        except Exception:
            return []

    def launch(self, argv: list[str]) -> None:
        if self._launch is not None:
            self._launch(argv)
            return
        from personal_os.actions.desktop import default_desktop

        default_desktop().launch(argv)

    def open_tab(self, url: str) -> None:
        self._send("tabs.open", {"url": url, "newTab": True})

    def open_first(self, url: str, selector: str, reuse: str) -> dict[str, Any]:
        return dict(self._send("open_first", {"url": url, "selector": selector, "reuse": reuse},
                               timeout_s=30) or {})  # fmt: skip

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)
