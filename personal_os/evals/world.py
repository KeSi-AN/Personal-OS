"""The pretend world a bench case runs in: everything Donna can touch, recorded, nothing real.

Real: the model (Ollama), the turn code (voice/main.py `respond`), the router, rules, agent,
executor and its policy, and a copy of your database and settings.
Pretend: the desktop (windows, launches, keys), the browser extension (tabs, media, pages),
Windows' media sessions, speech in and out, the popup and the island. What each would have
done is kept in `effects`, in order, so a case can check it ("no Google search", "the
YouTube Music tab was paused").
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from personal_os.actions.apps import InstalledApp
from personal_os.actions.desktop import FakeDesktop, WindowInfo


@dataclass
class Call:
    """One tool the executor was asked to run, and how it went."""

    tool: str
    args: dict[str, Any]
    status: str
    message: str

    def render(self) -> str:
        args = ", ".join(f"{k}={v!r}" for k, v in self.args.items())
        return f"{self.tool}({args}) -> {self.status}: {self.message}"[:300]


@dataclass
class Tab:
    id: int
    url: str
    title: str
    audible: bool = False
    song: str = ""
    artist: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "url": self.url, "title": self.title, "audible": self.audible,
                "lastAccessed": float(self.id)}  # fmt: skip


def default_tabs(playing: bool = True) -> list[Tab]:
    return [
        Tab(1, "https://github.com/KeSi-AN/Personal-OS", "Personal-OS - GitHub"),
        Tab(2, "https://music.youtube.com/watch?v=x", "bad luck - YouTube Music", playing,
            "bad luck", "We Came As Romans"),
        Tab(3, "https://fmovies.example/watch/dune", "Watch Dune Part Two - Fmovies"),
    ]  # fmt: skip


def default_windows() -> list[WindowInfo]:
    return [
        WindowInfo(101, "Personal-OS - GitHub - Brave", "normal", "brave.exe"),
        WindowInfo(102, "main.py - personal_os - Visual Studio Code", "maximized", "Code.exe"),
        WindowInfo(103, "Claude", "normal", "claude.exe"),
    ]


@dataclass
class World:
    """One case's pretend machine. `effects` is everything that would have happened."""

    apps: list[InstalledApp]
    tabs: list[Tab] = field(default_factory=default_tabs)
    desktop: FakeDesktop = field(default_factory=FakeDesktop)
    calls: list[Call] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)
    spoken: list[str] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)
    answers: list[str] = field(default_factory=list)  # typed answers to questions, in order

    def __post_init__(self) -> None:
        self.desktop.open_windows = default_windows()

    # --- the browser extension -----------------------------------------------------------

    def tab(self, tab_id: Any) -> Tab | None:
        return next((t for t in self.tabs if t.id == tab_id), None)

    def browser(self, op: str, args: dict[str, Any] | None) -> Any:
        a = dict(args or {})
        self.effects.append(f"browser {op} {json.dumps(a, sort_keys=True)}")
        if op == "tabs.list":
            return [t.as_dict() for t in self.tabs]
        if op == "tabs.open":
            new = Tab(max((t.id for t in self.tabs), default=0) + 1, str(a.get("url", "")), "")
            self.tabs.append(new)
            return {"tabId": new.id}
        if op == "tabs.close":
            ids = set(a.get("tabIds") or [])
            self.tabs = [t for t in self.tabs if t.id not in ids]
            return {}
        if op == "media":
            return self._media(a)
        if op == "open_first":
            return {"title": ""}  # the song's page; its title unknown, as often on a fresh tab
        if op == "page.read":
            t = self.tab(a.get("tabId")) or (self.tabs[-1] if self.tabs else None)
            if t is None:
                return {}
            return {"title": t.title, "url": t.url, "text": f"{t.title}\n(page text)",
                    "elements": []}  # fmt: skip
        return {}

    def _media(self, a: dict[str, Any]) -> dict[str, Any]:
        action = str(a.get("action") or "status")
        t = self.tab(a.get("tabId")) or next((t for t in self.tabs if t.audible), None)
        if t is None:
            t = next((t for t in self.tabs if t.song), None)
        if t is None:
            return {}
        if action in ("play", "pause", "toggle"):
            t.audible = {"play": True, "pause": False}.get(action, not t.audible)
        return {"title": t.song or t.title, "artist": t.artist, "playing": t.audible}

    # --- the rest ------------------------------------------------------------------------

    def ask(self, question: str) -> str:
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else ""

    def all_effects(self) -> list[str]:
        """What happened on the pretend machine: the browser's and the desktop's, in order
        within each."""
        desk = [f"desktop {name} {json.dumps(arg)}" for name, arg in self.desktop.calls]
        return [*self.effects, *desk]


def copy_db(src: Path, dst: Path) -> None:
    """A consistent copy of a database in use (its WAL included), read-only at the source."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    a = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    b = sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()


def read_apps(path: Path) -> list[InstalledApp]:
    """The Start menu list the running Donna last read (runtime/apps.json): no scan."""
    try:
        data = json.loads(path.read_text("utf-8"))
        return [InstalledApp(a["name"], a["app_id"], a.get("exe")) for a in data["apps"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []


@contextlib.contextmanager
def patched(world_ref: list[World]) -> Iterator[None]:
    """Point every way out of the process at `world_ref[0]` (swapped per case) while open."""
    from unittest import mock

    from personal_os.actions import apps, desktop
    from personal_os.actions.executor import Executor
    from personal_os.browser import control
    from personal_os.interfaces import app, island, popup, tray
    from personal_os.platform import media_sessions
    from personal_os.voice import duck
    from personal_os.voice import main as voice_main

    original = Executor.request

    def request(self: Executor, tool: str, args: dict[str, Any], **kw: Any) -> Any:
        out = original(self, tool, args, **kw)
        world_ref[0].calls.append(Call(tool, dict(args), str(out.status), str(out.message)))
        return out

    def w() -> World:
        return world_ref[0]

    with contextlib.ExitStack() as stack:
        for target, name, value in [
            (Executor, "request", request),
            (desktop, "default_desktop", lambda: w().desktop),
            (apps, "installed", lambda *_a, **_k: w().apps),
            (apps, "installed_now", lambda *_a, **_k: w().apps),
            (control, "connected", lambda *_a, **_k: ["brave"]),
            (control, "send", lambda _rt, op, args=None, **_k: w().browser(op, args)),
            (media_sessions, "sessions", lambda *_a, **_k: []),
            (media_sessions, "control", lambda *_a, **_k: None),
            (duck, "_sessions", lambda: []),
            (voice_main, "mic_muted", lambda: False),
            (popup, "_spawn", lambda *_a: None),
            (island, "_spawn", lambda *_a: None),
            (app, "_spawn", lambda *_a: None),
            (tray, "open_url", lambda *_a: None),
        ]:
            stack.enter_context(mock.patch.object(target, name, value))
        yield
