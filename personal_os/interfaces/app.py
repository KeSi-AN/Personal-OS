"""The desktop app: the dashboard in its own window instead of a browser tab. ADR-079.

Its own process (pywebview on WebView2, like the voice popup), started by the tray, the
startup brief, the popup's "open dashboard" and `personal-os app`. One at a time: a second
open asks the running app to show a page instead (a request file in the runtime folder,
read twice a second), and brings it to the front.

It signs in the same way the browser did: a one-time login code from the database, with
`next=` naming the page. Links to other sites open in your browser, not in the app.

Without pywebview or the WebView2 runtime it falls back to the browser, so the dashboard
always opens.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from personal_os.config.schema import Config
from personal_os.core import runtime

LOCK = "app.lock"
REQUEST = "app_open.json"  # {"path": "/brief", "at": ...}: show this page, come to the front
TITLE = "Donna · Personal OS"
WIDTH, HEIGHT = 1280, 860


def is_open(runtime_dir: Path) -> bool:
    lock = runtime.InstanceLock(runtime_dir / LOCK)
    try:
        lock.acquire()
    except runtime.AlreadyRunningError:
        return True
    lock.release()
    return False


def safe_path(path: str) -> str:
    """A page of the dashboard only: never another host."""
    path = str(path or "/")
    return path if path.startswith("/") and not path.startswith("//") and "\\" not in path else "/"


def login_url(cfg: Config, path: str = "/") -> str:
    from urllib.parse import quote

    from personal_os.core.clock import SystemClock
    from personal_os.storage.db import connect
    from personal_os.web import auth

    conn = connect(cfg.db_path)
    try:
        code = auth.new_login_code(conn, SystemClock())
    finally:
        conn.close()
    path = safe_path(path)
    nxt = f"&next={quote(path, safe='/')}" if path != "/" else ""
    return f"http://127.0.0.1:{cfg.api.port}/login?code={code}{nxt}"


def open_app(cfg: Config, path: str = "/") -> str:
    """Show this dashboard page in the app: 'shown' (the app was open), 'opened', or
    'browser' (no app runtime here: opened in the browser instead)."""
    path = safe_path(path)
    if is_open(cfg.runtime_dir):
        runtime.write_json_atomic(
            cfg.runtime_dir / REQUEST, {"path": path, "at": datetime.now(UTC).isoformat()}
        )
        return "shown"
    from personal_os.interfaces import popup

    if popup.unavailable_reason():
        from personal_os.interfaces.tray import open_url

        open_url(login_url(cfg, path))
        return "browser"
    _spawn(path)
    return "opened"


def _spawn(path: str) -> None:
    """Start the app process windowless and detached (tests replace this)."""
    import subprocess

    from personal_os.core import spawn

    subprocess.Popen(
        spawn.module_command("personal_os.interfaces.app", "--path", path),
        env=spawn.child_env(),
        creationflags=spawn.creation_flags(detached=True),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def take_request(runtime_dir: Path) -> str | None:
    """The page another open asked for, once (the request file is removed)."""
    p = runtime_dir / REQUEST
    data = runtime.read_json(p)
    if not isinstance(data, dict):
        return None
    with contextlib.suppress(OSError):
        p.unlink()
    return safe_path(str(data.get("path") or "/"))


NOT_RUNNING = """<!doctype html><meta charset=utf-8><body style="background:#141416;color:#ddd;
font:15px system-ui;display:grid;place-items:center;height:90vh"><div>Capture isn't running, so the
dashboard isn't either. Start it from the Start menu or with <code>personal-os daemon start</code>,
then open this again.</div>"""


def _front(window: Any) -> None:
    with contextlib.suppress(Exception):
        window.restore()
        window.show()
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = int(window.native.Handle.ToInt32())
        user32 = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _watch(cfg: Config, window: Any, stop: threading.Event) -> None:
    """Requests from other opens: load the page and come to the front."""
    while not stop.wait(0.5):
        path = take_request(cfg.runtime_dir)
        if path is None:
            continue
        try:
            window.load_url(login_url(cfg, path))
        except Exception:
            continue
        _front(window)


def main(argv: list[str] | None = None) -> int:
    from personal_os.config.loader import load_config

    args = sys.argv[1:] if argv is None else argv
    path = args[args.index("--path") + 1] if "--path" in args[:-1] else "/"
    cfg = load_config()
    lock = runtime.InstanceLock(cfg.runtime_dir / LOCK)
    for _ in range(5):
        try:
            lock.acquire()
            break
        except runtime.AlreadyRunningError:
            time.sleep(0.1)
    else:
        open_app(cfg, path)  # one is open: show the page there
        return 0
    try:
        return _run(cfg, safe_path(path))
    except Exception as exc:
        from personal_os.interfaces import popup

        popup.record_error(cfg.runtime_dir, f"app: {type(exc).__name__}: {exc}")
        return 0
    finally:
        lock.release()


def _run(cfg: Config, path: str) -> int:
    import webview

    with contextlib.suppress(OSError):
        (cfg.runtime_dir / REQUEST).unlink()  # an old request isn't for this window
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    if runtime.daemon_info(cfg.runtime_dir).running:
        window = webview.create_window(TITLE, url=login_url(cfg, path), width=WIDTH,
                                       height=HEIGHT, min_size=(720, 520),
                                       background_color="#141416", text_select=True)  # fmt: skip
    else:
        window = webview.create_window(TITLE, html=NOT_RUNNING, width=640, height=240)
    if window is None:
        return 0
    stop = threading.Event()
    watcher = threading.Thread(target=_watch, args=(cfg, window, stop), daemon=True)
    window.events.shown += lambda: watcher.start()
    try:
        webview.start(gui="edgechromium", private_mode=True, debug=False)
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
