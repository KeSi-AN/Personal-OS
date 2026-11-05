"""The voice popup: a small always-on-top window showing the current voice turn. ADR-071.

Its own process, started by the voice process when a turn starts (hotkey, wake word, barge-in)
and gone a few seconds after the reply, so it costs nothing while closed. It talks to nobody:
it reads the voice status file and the pause and kill-switch markers in the runtime folder
(popup_view.read_view), about five times a second. The page is an HTML string served from the
package with a strict CSP, so pywebview opens no port and nothing loads from the internet.

If pywebview or the WebView2 runtime is missing, launching records why (doctor shows it) and
voice carries on without a popup.
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from personal_os.config.schema import Config
from personal_os.core import runtime
from personal_os.voice.popup_view import AutoClose, View, read_view

LOCK = "popup.lock"
STATE = "popup.json"  # last launch, last error: for doctor
WIDTH, HEIGHT = 360, 240
MARGIN = 20
TASKBAR = 56  # logical pixels kept clear at the bottom of the screen
WEBVIEW2_CLIENT = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def last_state(runtime_dir: Path) -> dict[str, Any]:
    return runtime.read_json(runtime_dir / STATE) or {}


def _update_state(runtime_dir: Path, **fields: Any) -> None:
    data = last_state(runtime_dir)
    data.update(fields)
    runtime.write_json_atomic(runtime_dir / STATE, data)


def record_error(runtime_dir: Path, message: str) -> None:
    """Why the popup couldn't open (no transcripts: exception names and setup problems)."""
    _update_state(runtime_dir, error=message[:300], error_at=_now())


def webview2_version() -> str | None:
    """The installed WebView2 runtime's version, or None (pywebview's Edge backend needs it)."""
    if sys.platform != "win32":
        return None
    import winreg

    places = [
        (winreg.HKEY_LOCAL_MACHINE, WEBVIEW2_CLIENT.replace("SOFTWARE", r"SOFTWARE\WOW6432Node")),
        (winreg.HKEY_LOCAL_MACHINE, WEBVIEW2_CLIENT),
        (winreg.HKEY_CURRENT_USER, WEBVIEW2_CLIENT),
    ]
    for hive, key in places:
        try:
            with winreg.OpenKey(hive, key) as k:
                version, _ = winreg.QueryValueEx(k, "pv")
        except OSError:
            continue
        if version and version != "0.0.0.0":
            return str(version)
    return None


def pywebview_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("webview") is not None


def unavailable_reason() -> str | None:
    if not pywebview_available():
        return "pywebview isn't installed"
    if webview2_version() is None:
        return "the WebView2 runtime isn't installed"
    return None


def doctor_line(cfg: Config) -> tuple[str, str | None]:
    """doctor's popup line, and a problem if the popup is on but can't open."""
    if not cfg.voice.popup:
        return "off ([voice] popup = false)", None
    reason = unavailable_reason()
    version = webview2_version()
    line = f"on, WebView2 {version}" if version else "on, WebView2 missing"
    state = last_state(cfg.runtime_dir)
    if state.get("error"):
        line += f"; last error {str(state.get('error_at', ''))[:16]}: {state['error']}"
    problem = f"the voice popup can't open: {reason}" if reason else None
    return line, problem


def is_open(runtime_dir: Path) -> bool:
    """Whether a popup holds the lock. (Probing takes the lock for an instant and gives it back.)"""
    lock = runtime.InstanceLock(runtime_dir / LOCK)
    try:
        lock.acquire()
    except runtime.AlreadyRunningError:
        return True
    lock.release()
    return False


def launch(cfg: Config, *, demo: bool = False) -> str:
    """Open the popup unless one is open: 'opened', 'already open' or 'unavailable: why'.
    Never raises for a missing runtime, and never waits for the window."""
    if is_open(cfg.runtime_dir):
        return "already open"
    reason = unavailable_reason()
    if reason:
        record_error(cfg.runtime_dir, reason)
        return f"unavailable: {reason}"
    from personal_os.interfaces.tray import spawn_module

    if demo:
        _spawn_demo()
    else:
        spawn_module("personal_os.interfaces.popup")
    _update_state(cfg.runtime_dir, launched_at=_now())
    return "opened"


def _spawn_demo() -> None:
    import subprocess

    from personal_os.core import spawn

    subprocess.Popen(
        spawn.module_command("personal_os.interfaces.popup", "--demo"),
        env=spawn.child_env(),
        creationflags=spawn.creation_flags(detached=True),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def position(screen_w: int, screen_h: int, corner: str) -> tuple[int, int]:
    """Top-left of the window for a corner, in logical pixels, clear of the taskbar."""
    right = corner.endswith("right")
    bottom = corner.startswith("bottom")
    x = screen_w - WIDTH - MARGIN if right else MARGIN
    y = screen_h - HEIGHT - MARGIN - TASKBAR if bottom else MARGIN
    return max(0, x), max(0, y)


# --- the demo: fake turns, so the states can be seen without speaking ---------------------

DEMO: list[tuple[float, View]] = [
    (1.8, View(True, "listening", "demo1", "hotkey")),
    (1.6, View(True, "thinking", "demo1", "hotkey", heard="What's the weather like?")),
    (
        3.0,
        View(
            True,
            "speaking",
            "demo1",
            "hotkey",
            heard="What's the weather like?",
            reply="This is a demo reply. Nothing here came from your activity.",
        ),
    ),
    (
        1.5,
        View(
            True,
            "done",
            "demo1",
            "hotkey",
            heard="What's the weather like?",
            reply="This is a demo reply. Nothing here came from your activity.",
        ),
    ),
    (1.4, View(True, "listening", "demo2", "wake")),
    (
        2.4,
        View(True, "listening", "demo2", "wake", heard="open brat", question="Did you mean Brave?"),
    ),
    (
        2.0,
        View(True, "speaking", "demo2", "wake", heard="yes", reply="Opening Brave (demo)."),
    ),
    (2.0, View(True, "done", "demo3", "hotkey", heard="", reply="", paused=True)),
    (
        2.0,
        View(True, "done", "demo3", "hotkey", killed=True),
    ),
    (0.0, View(True, "done", "demo4", "hotkey", heard="That's the demo.", reply="Closing soon.")),
]


def demo_view(elapsed: float) -> View:
    t = 0.0
    for duration, view in DEMO:
        t += duration
        if elapsed < t:
            return view
    return DEMO[-1][1]


# --- the window ---------------------------------------------------------------------------


class Api:
    """What the page may call. Only harmless things: read the view, pin, close, open the
    dashboard through the usual one-time login link."""

    def __init__(self, cfg: Config, *, demo: bool) -> None:
        self._cfg = cfg
        self._demo = demo
        self._started = time.monotonic()
        self._auto = AutoClose(cfg.voice.popup_autoclose_s)
        self._window: Any = None
        self._closing = False

    def view(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._demo:
            v = demo_view(now - self._started)
        else:
            v = read_view(self._cfg.runtime_dir, datetime.now(UTC))
        close, new_turn = self._auto.step(v, now)
        data = v.as_dict()
        data.update(pinned=self._auto.pinned, new_turn=new_turn)
        if close:
            self.close()
        return data

    def pin(self, on: bool) -> bool:
        self._auto.pin(bool(on))
        return self._auto.pinned

    def close(self) -> None:
        if self._closing or self._window is None:
            return
        self._closing = True
        # A moment later, so the page's call returns before the window goes away.
        timer = threading.Timer(0.25, self._window.destroy)
        timer.daemon = True
        timer.start()

    def focus(self) -> None:
        """Clicked: take keyboard focus now (it opened without it), so Esc works."""
        if sys.platform != "win32" or self._window is None:
            return
        try:
            import ctypes

            hwnd = int(self._window.native.Handle.ToInt32())
            user32 = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
            style = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, style & ~0x08000000)  # drop WS_EX_NOACTIVATE
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def open_dashboard(self) -> str:
        from personal_os.interfaces.tray import open_url

        if not runtime.daemon_info(self._cfg.runtime_dir).running:
            return "Capture isn't running, so the dashboard isn't either."
        from personal_os.core.clock import SystemClock
        from personal_os.storage.db import connect
        from personal_os.web import auth

        conn = connect(self._cfg.db_path)
        try:
            code = auth.new_login_code(conn, SystemClock())
        finally:
            conn.close()
        open_url(f"http://127.0.0.1:{self._cfg.api.port}/login?code={code}")
        return ""


def page() -> str:
    from importlib.resources import files

    return (files("personal_os.interfaces") / "popup.html").read_text("utf-8")


def _take_lock(runtime_dir: Path) -> runtime.InstanceLock | None:
    """The popup lock, retried briefly: the voice process may be probing it this instant."""
    lock = runtime.InstanceLock(runtime_dir / LOCK)
    for _ in range(5):
        try:
            lock.acquire()
            return lock
        except runtime.AlreadyRunningError:
            time.sleep(0.1)
    return None


def main(argv: list[str] | None = None) -> int:
    from personal_os.config.loader import load_config

    args = sys.argv[1:] if argv is None else argv
    demo = "--demo" in args
    cfg = load_config()
    lock = _take_lock(cfg.runtime_dir)
    if lock is None:
        return 0  # one is open already
    try:
        return _run(cfg, demo=demo)
    except Exception as exc:
        record_error(cfg.runtime_dir, f"{type(exc).__name__}: {exc}")
        return 0
    finally:
        lock.release()


def _run(cfg: Config, *, demo: bool) -> int:
    reason = unavailable_reason()
    if reason:
        record_error(cfg.runtime_dir, reason)
        return 0
    import webview

    api = Api(cfg, demo=demo)
    screens = webview.screens
    sw, sh = (screens[0].width, screens[0].height) if screens else (1280, 800)
    x, y = position(sw, sh, cfg.voice.popup_position)
    window = webview.create_window(
        "Personal OS voice",
        html=page(),
        js_api=api,
        width=WIDTH,
        height=HEIGHT,
        x=x,
        y=y,
        resizable=False,
        frameless=True,
        easy_drag=True,
        on_top=True,
        focus=False,
        background_color="#1d1d20",
    )
    api._window = window
    webview.start(gui="edgechromium", private_mode=True, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
