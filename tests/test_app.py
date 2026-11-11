"""The desktop app (ADR-079): one window, pages asked for by the tray, the brief and the CLI."""

from __future__ import annotations

from pathlib import Path

from personal_os.config.schema import Config
from personal_os.core import runtime
from personal_os.interfaces import app, popup


def test_opens_the_app_at_a_page(config: Config, no_real_app: list[str], monkeypatch):
    monkeypatch.setattr(popup, "unavailable_reason", lambda: None)
    assert app.open_app(config, "/brief") == "opened"
    assert no_real_app == ["app /brief"]


def test_an_open_app_is_asked_to_show_the_page(config: Config, no_real_app: list[str]):
    lock = runtime.InstanceLock(config.runtime_dir / app.LOCK)
    lock.acquire()  # the app is open
    try:
        assert app.open_app(config, "/donna") == "shown"
    finally:
        lock.release()
    assert no_real_app == []  # no second window
    assert app.take_request(config.runtime_dir) == "/donna"
    assert app.take_request(config.runtime_dir) is None  # once


def test_without_webview2_it_opens_in_the_browser(
    home: Path, config: Config, no_real_app: list[str], monkeypatch
):
    from personal_os.storage.db import open_db

    open_db(config.db_path).close()
    monkeypatch.setattr(popup, "unavailable_reason", lambda: "no WebView2")
    assert app.open_app(config, "/brief") == "browser"
    assert len(no_real_app) == 1 and no_real_app[0].startswith("browser http://127.0.0.1:")
    assert "login?code=" in no_real_app[0] and no_real_app[0].endswith("&next=/brief")


def test_only_dashboard_pages():
    assert app.safe_path("/p/x") == "/p/x"
    for bad in ("https://evil.example/", "//evil.example/", r"/\evil", "", "brief"):
        assert app.safe_path(bad) == "/"
