"""Browser control through the extension (ADR-076): the command channel through the native
host, the browser tools and their policy. A fake browser stands in for the extension."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from personal_os.actions.base import Origin
from personal_os.browser import control, recipes
from personal_os.browser.native_host import Host, detect_browser
from personal_os.config.schema import Config
from tests.test_actions import Confirm, Env

Handler = Callable[[str, dict[str, Any]], dict[str, Any]]


class FakeBrowser:
    """Runs the real host's command claiming, and answers like the extension would."""

    def __init__(self, home: Path, handler: Handler, browser: str = "brave") -> None:
        self.host = Host(home, browser)
        self.handler = handler
        self.seen: list[tuple[str, dict[str, Any]]] = []
        self.stop = threading.Event()
        self.marker = self.host.register()
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self) -> None:
        while not self.stop.is_set():
            for cmd in self.host.take_commands():
                self.seen.append((cmd["op"], cmd["args"]))
                res = self.handler(cmd["op"], cmd["args"])
                self.host.handle({"type": "result", "id": cmd["id"], **res})
            time.sleep(0.02)

    def close(self) -> None:
        self.stop.set()
        self.thread.join()
        self.marker.unlink(missing_ok=True)


@pytest.fixture
def browser(config: Config):
    made: list[FakeBrowser] = []

    def start(handler: Handler, name: str = "brave") -> FakeBrowser:
        b = FakeBrowser(config.runtime_dir.parent, handler, name)
        made.append(b)
        return b

    yield start
    for b in made:
        b.close()


def ok(data: Any = None) -> dict[str, Any]:
    return {"ok": True, "data": data}


TABS = [
    {"id": 1, "title": "YouTube Music", "url": "https://music.youtube.com/", "active": False},
    {"id": 2, "title": "Docs - Python", "url": "https://docs.python.org/3/", "active": True},
    {"id": 3, "title": "Lo-fi beats - YouTube", "url": "https://www.youtube.com/watch?v=x"},
]


# --- the channel ----------------------------------------------------------------------------


def test_send_and_receive(config: Config, browser):
    b = browser(lambda op, args: ok({"echo": op, "args": args}))
    got = control.send(config.runtime_dir, "tabs.list", {"x": 1})
    assert got == {"echo": "tabs.list", "args": {"x": 1}}
    assert control.connected(config.runtime_dir) == ["brave"]
    assert b.seen == [("tabs.list", {"x": 1})]
    assert list((config.runtime_dir / "browser" / "res").glob("*.json")) == []  # cleaned up


def test_errors_come_back_as_errors(config: Config, browser):
    browser(lambda op, args: {"ok": False, "error": "that tab isn't a web page"})
    with pytest.raises(control.BrowserError, match="isn't a web page"):
        control.send(config.runtime_dir, "page.read")


def test_no_browser_connected(config: Config):
    with pytest.raises(control.BrowserUnavailable, match="isn't connected"):
        control.send(config.runtime_dir, "tabs.list")


def test_stale_markers_are_ignored(config: Config):
    hosts = config.runtime_dir / "browser" / "hosts"
    hosts.mkdir(parents=True)
    (hosts / "brave-1.json").write_text(json.dumps({"browser": "brave", "pid": 999_999_9}))
    assert control.connected(config.runtime_dir) == []
    assert not (hosts / "brave-1.json").exists()


def test_a_command_nobody_takes_is_withdrawn(config: Config, monkeypatch):
    hosts = config.runtime_dir / "browser" / "hosts"
    hosts.mkdir(parents=True)
    (hosts / "brave-x.json").write_text(json.dumps({"browser": "brave", "pid": os.getpid()}))
    with pytest.raises(control.BrowserError, match="didn't answer"):
        control.send(config.runtime_dir, "tabs.list", timeout_s=0.3)
    assert list((config.runtime_dir / "browser" / "cmd").glob("*.json")) == []  # never run late


def test_commands_go_to_the_chosen_browser_only(config: Config, browser):
    brave = browser(lambda op, args: ok("brave"), "brave")
    chrome = browser(lambda op, args: ok("chrome"), "chrome")
    assert control.send(config.runtime_dir, "tabs.list", browser="chrome") == "chrome"
    assert control.send(config.runtime_dir, "tabs.list", browser="brave") == "brave"
    assert len(brave.seen) == 1 and len(chrome.seen) == 1


def test_host_drops_stale_and_malformed_commands(tmp_path: Path):
    host = Host(tmp_path, "brave")
    host.cmd_dir.mkdir(parents=True)
    now = time.time()
    (host.cmd_dir / "a.json").write_text(json.dumps({"id": "old", "op": "x", "at": now - 999}))
    (host.cmd_dir / "b.json").write_text(json.dumps({"id": "../bad", "op": "x", "at": now}))
    (host.cmd_dir / "c.json").write_text(json.dumps({"id": "good", "op": "x", "at": now}))
    (host.cmd_dir / "d.json").write_text(
        json.dumps({"id": "other", "op": "x", "at": now, "browser": "edge"})
    )
    assert [c["id"] for c in host.take_commands(now)] == ["good"]
    assert [f.name for f in host.cmd_dir.iterdir()] == ["d.json"]  # edge's is left for edge


def test_results_only_with_our_own_ids(tmp_path: Path):
    host = Host(tmp_path, "brave")
    host.handle({"type": "result", "id": "../../evil", "ok": True})
    assert not host.res_dir.exists() or list(host.res_dir.iterdir()) == []
    host.handle({"type": "result", "id": "abc123", "ok": True, "data": 1})
    assert json.loads((host.res_dir / "abc123.json").read_text())["data"] == 1


def test_results_are_written_even_while_paused(tmp_path: Path):
    host = Host(tmp_path, "brave")
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "paused").write_text("")
    host.handle({"type": "result", "id": "r1", "ok": True})
    assert (host.res_dir / "r1.json").exists()  # an answer to you, not capture


def test_detect_browser_falls_back_to_the_launcher_argument():
    assert detect_browser("brave") == "brave"  # under pytest no browser is an ancestor


# --- recipes --------------------------------------------------------------------------------


def test_recipes():
    yt = recipes.find("YT Music")
    assert yt is not None and yt.name == "youtube music" and yt.first
    assert recipes.search_url(yt, "when I was your man") == (
        "https://music.youtube.com/search?q=when+I+was+your+man"
    )
    assert recipes.find("nope") is None


# --- the tools ------------------------------------------------------------------------------


def test_play_media_finds_and_starts_it(home: Path, config: Config, browser):
    b = browser(lambda op, args: ok({"title": "When I Was Your Man - YouTube Music", "url": "u"}))
    env = Env(home, config)
    o = env.ex.request(
        "play_media", {"query": "When I Was Your Man Bruno Mars"}, origin=Origin.USER
    )
    assert o.ok and o.message == 'Playing "When I Was Your Man" on youtube music'
    op, args = b.seen[0]
    assert op == "open_first"
    assert args["url"] == "https://music.youtube.com/search?q=When+I+Was+Your+Man+Bruno+Mars"
    assert "watch?v=" in args["selector"]


def test_browser_open_search_and_url(home: Path, config: Config, browser):
    b = browser(lambda op, args: ok())
    env = Env(home, config)
    assert env.ex.request(
        "browser_open", {"query": "lofi", "site": "youtube"}, origin=Origin.USER
    ).ok
    assert env.ex.request(
        "browser_open", {"url": "github.com/notifications"}, origin=Origin.USER
    ).ok
    assert [a["url"] for _, a in b.seen] == [
        "https://www.youtube.com/results?search_query=lofi",
        "https://github.com/notifications",
    ]


def test_switch_and_close_tabs_by_their_words(home: Path, config: Config, browser):
    def handler(op: str, args: dict[str, Any]) -> dict[str, Any]:
        return ok(TABS) if op == "tabs.list" else ok()

    b = browser(handler)
    env = Env(home, config)
    o = env.ex.request("browser_tab", {"action": "switch", "which": "music"}, origin=Origin.USER)
    assert o.ok and o.message == 'Switched to "YouTube Music"'
    assert ("tabs.activate", {"tabId": 1}) in b.seen
    o = env.ex.request(
        "browser_tab", {"action": "close", "which": "youtube", "all": True}, origin=Origin.USER
    )
    assert o.ok and o.message == "Closed 2 tabs"
    o = env.ex.request("browser_tab", {"action": "switch", "which": "netflix"}, origin=Origin.USER)
    assert not o.ok and 'no open tab has "netflix"' in o.message


def test_media_and_whats_playing(home: Path, config: Config, browser):
    status = {"title": "Lo-fi beats", "artist": "Chill", "playing": False}
    browser(lambda op, args: ok(status))
    env = Env(home, config)
    o = env.ex.request("browser_media", {"action": "pause"}, origin=Origin.USER)
    assert o.ok and o.message == "Pause: Lo-fi beats - Chill is now paused"
    o = env.ex.request("whats_playing", {}, origin=Origin.USER)
    assert o.ok and o.result is not None and o.result.captured


def test_clicking_always_asks(home: Path, config: Config, browser):
    b = browser(lambda op, args: ok("clicked"))
    confirm = Confirm(False)
    env = Env(home, config, confirm)
    o = env.ex.request("browser_act", {"action": "click", "label": "Subscribe"}, origin=Origin.USER)
    assert confirm.prompts and not o.ok and b.seen == []  # declined: nothing sent


def test_reading_a_page_is_captured_and_numbers_its_elements(home: Path, config: Config, browser):
    page = {
        "title": "Docs", "url": "https://docs.python.org/3/", "text": "Welcome",
        "elements": [{"ref": 1, "role": "link", "label": "Tutorial"}],
    }  # fmt: skip
    browser(lambda op, args: ok(page))
    env = Env(home, config)
    o = env.ex.request("browser_read", {}, origin=Origin.USER)
    assert o.ok and o.result is not None and o.result.captured
    assert "[1] link: Tutorial" in str(o.result.data)


def test_page_text_cant_be_sent_out_by_a_later_call(home: Path, config: Config, browser):
    secret = "the launch code is 4471 9928 1123 and nobody may know it"
    page = {"title": "x", "url": "https://a.example/", "text": secret, "elements": []}
    b = browser(lambda op, args: ok(page) if op == "page.read" else ok())
    env = Env(home, config)
    read = env.ex.request("browser_read", {}, origin=Origin.USER)
    o = env.ex.request(
        "browser_open", {"query": secret}, origin=Origin.PLANNED, tainted=True,
        read_so_far=[str(read.result.data)],  # type: ignore[union-attr]
    )  # fmt: skip
    assert not o.ok and [op for op, _ in b.seen] == ["page.read"]  # blocked before sending


def test_without_the_extension_the_tools_say_so(home: Path, config: Config):
    env = Env(home, config)
    o = env.ex.request("play_media", {"query": "x"}, origin=Origin.USER)
    assert not o.ok and "isn't connected" in o.message


def test_play_media_reuses_the_site_tab(home: Path, config: Config, browser):
    b = browser(lambda op, args: ok({"title": "Circles - YouTube Music"}))
    env = Env(home, config)
    env.ex.request("play_media", {"query": "Circles Post Malone"}, origin=Origin.USER)
    assert b.seen[0][1]["reuse"] == "music.youtube.com"  # the extension plays it in that tab


def test_group_the_first_three_tabs(home: Path, config: Config, browser):
    tabs = [
        {"id": 10 + i, "index": i, "window": 1, "title": f"Tab {i}", "url": f"https://x{i}.example/",
         "active": i == 0}
        for i in range(5)
    ] + [{"id": 99, "index": 0, "window": 2, "title": "Other window", "url": "https://o.example/"}]  # fmt: skip

    def handler(op: str, args: dict[str, Any]) -> dict[str, Any]:
        return ok(tabs) if op == "tabs.list" else ok()

    b = browser(handler)
    env = Env(home, config)
    o = env.ex.request(
        "browser_tab",
        {"action": "group", "positions": [1, 2, 3], "name": "Entertainment"},
        origin=Origin.USER,
    )
    assert o.ok and o.message == 'Grouped 3 tabs as "Entertainment"'
    assert ("tabs.group", {"tabIds": [10, 11, 12], "title": "Entertainment"}) in b.seen
    o = env.ex.request("browser_tab", {"action": "group", "positions": [9]}, origin=Origin.USER)
    assert not o.ok and "aren't that many tabs" in o.message


def test_voice_pause_goes_to_the_browser_tab(home: Path, config: Config, browser, monkeypatch):
    import winsound

    from tests.test_phase7 import voice_for
    from tests.test_voice_fastpath import INDEX

    monkeypatch.setattr(winsound, "Beep", lambda *_a: None)
    b = browser(
        lambda op, args: ok({"title": "Circles", "artist": "Post Malone", "playing": False})
    )
    env = Env(home, config)
    v, _h = voice_for(env)
    v.index._index = INDEX
    v.last_audio = None
    v.turn = {"id": "t"}  # a turn in progress, as in real use
    reply = v.fast("pause music")
    assert ("media", {"action": "pause", "seconds": 0}) in b.seen
    assert reply == "Pausing." and _h.said == ["Pausing."]  # said first, then paused
