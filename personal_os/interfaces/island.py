"""The island: Donna at the top of the screen, like the Mac's Dynamic Island. ADR-082.

One process for as long as voice runs (started by the voice process, which checks on it every
10 s). It's a small black pill at the top centre of the screen with the mouse; resting the
pointer on it, saying "Donna", "show my projects", or a reminder coming due opens it on a page
(Donna, Today, Music, Projects), and a two-finger swipe, the ‹ › arrows or the dots move
between pages. It hides while a full-screen app or game is on its screen, unless you called.

A pill over an app is a ghost (island_logic, ADR-083): see-through and click-through, so the
browser's tabs or a title bar under it still work; holding the pointer against the top edge
on it opens it. Hover is read from the pointer's position here, since a click-through
window's page never sees the mouse.

The window is the island: no transparent margins around it. Its rounded shape
is a window region, and its size is animated by a spring here in Python at 60 frames a second
(a spring keeps its speed when the target changes mid-way, so a change of mind is smooth).
The page draws only what's inside, in a fixed-width layer, so nothing reflows as it grows.

The Donna page is the voice popup's (interfaces/popup.py): the same Api underneath (typed
prompts, Talk, Stop, Pin, reminders, links), the same status file, the same "seen" handshake
so she speaks only once the reply is on screen. Pages other than Donna read a few lines from
the database while shown (interfaces/island_data.py); the only writes are your own to-dos and
notes (ticked off, added), as the dashboard does.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from personal_os.config.schema import Config
from personal_os.core import runtime
from personal_os.interfaces import island_logic as L
from personal_os.interfaces import popup

LOCK = "island.lock"
PAGE_FILE = "island_page.json"  # {"page": "projects", "at": ...}: open on this page
TITLE = "Donna"
ACTIVE = ("listening", "thinking", "speaking")
DEBUG = bool(os.environ.get("POS_ISLAND_DEBUG"))  # decisions printed (for development)


# --- other processes: open a page, start the island -----------------------------------------


def request_page(runtime_dir: Path, page: str) -> None:
    """Ask the island to open on a page ("show my projects"), or "hide"."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime.write_json_atomic(runtime_dir / PAGE_FILE, {"page": page, "at": time.time()})


def take_page(runtime_dir: Path, max_age_s: float = 5.0) -> str | None:
    path = runtime_dir / PAGE_FILE
    if not path.exists():
        return None
    data = runtime.read_json(path) or {}
    path.unlink(missing_ok=True)
    try:
        fresh = time.time() - float(data.get("at", 0)) <= max_age_s
    except (TypeError, ValueError):
        return None
    page = str(data.get("page", ""))
    return page if fresh and (page in L.PAGES or page == "hide") else None


def is_open(runtime_dir: Path) -> bool:
    lock = runtime.InstanceLock(runtime_dir / LOCK)
    try:
        lock.acquire()
    except runtime.AlreadyRunningError:
        return True
    lock.release()
    return False


def wanted(cfg: Config) -> bool:
    return cfg.voice.enabled and cfg.voice.popup and cfg.voice.popup_style == "island"


def launch(cfg: Config) -> str:
    """Start the island unless it runs: 'opened', 'already open' or 'unavailable: why'."""
    if is_open(cfg.runtime_dir):
        return "already open"
    reason = popup.unavailable_reason()
    if reason:
        popup.record_error(cfg.runtime_dir, reason)
        return f"unavailable: {reason}"
    _spawn()
    return "opened"


def _spawn() -> None:
    """The island process, windowless and detached (tests replace this)."""
    import subprocess

    from personal_os.core import spawn

    subprocess.Popen(
        spawn.module_command("personal_os.interfaces.island"),
        env=spawn.child_env(),
        creationflags=spawn.creation_flags(detached=True),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


# --- Windows: monitors, shape, full screen, sound -------------------------------------------

if sys.platform == "win32":
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32")
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]  # fmt: skip
    user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.GetForegroundWindow.restype = wintypes.HWND
    gdi32.CreateRoundRectRgn.restype = wintypes.HRGN
    gdi32.CreateRectRgn.restype = wintypes.HRGN
    gdi32.CombineRgn.argtypes = [wintypes.HRGN, wintypes.HRGN, wintypes.HRGN, ctypes.c_int]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]

HWND_TOPMOST = -1
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_APPWINDOW = 0x80, 0x08000000, 0x40000
# A layered window can be see-through (LWA_ALPHA), and a transparent one lets clicks through
# to what's under it. WebView2 still draws in it (checked on this laptop: the page blends with
# what's behind it, and the window under the pointer becomes the app's).
WS_EX_LAYERED, WS_EX_TRANSPARENT, LWA_ALPHA = 0x80000, 0x20, 0x2
SW_HIDE, SW_SHOWNOACTIVATE = 0, 4


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]  # fmt: skip


def cursor_monitor() -> tuple[int, tuple[int, int, int, int], float]:
    """The monitor with the mouse: (handle, its bounds in physical pixels, its DPI scale)."""
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    mon = user32.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
    return _monitor(mon)


def _monitor(mon: int) -> tuple[int, tuple[int, int, int, int], float]:
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(mon, ctypes.byref(info))
    r = info.rcMonitor
    dpi_x, dpi_y = ctypes.c_uint(96), ctypes.c_uint(96)
    with contextlib.suppress(Exception):
        ctypes.windll.shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
    return int(mon or 0), (r.left, r.top, r.right, r.bottom), dpi_x.value / 96.0


def fullscreen_on(monitor: int, bounds: tuple[int, int, int, int], own: int) -> bool:
    """A full-screen app, game or presentation in front on this monitor."""
    fg = user32.GetForegroundWindow()
    if not fg or fg == own:
        return False
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(fg, cls, 64)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        return False  # the desktop and the taskbar aren't apps
    if user32.MonitorFromWindow(fg, 2) != monitor:
        return False
    r = wintypes.RECT()
    user32.GetWindowRect(fg, ctypes.byref(r))
    # Exactly the monitor: a maximized window overhangs it by its borders (-8, -8, ...) and
    # isn't full screen, even with the taskbar hidden.
    return (r.left, r.top, r.right, r.bottom) == bounds


def music_playing(skip: set[int]) -> bool:
    """Another app (not voice, not this) is making sound now."""
    try:
        import comtypes
        from pycaw.pycaw import AudioUtilities, IAudioMeterInformation

        with contextlib.suppress(OSError):
            comtypes.CoInitialize()
        for s in AudioUtilities.GetAllSessions():
            if s.Process is None or s.Process.pid in skip or s.State != 1:
                continue
            meter = s._ctl.QueryInterface(IAudioMeterInformation)
            if float(meter.GetPeakValue()) > 0.01:
                return True
    except Exception:
        return False
    return False


# --- the shape: a spring per edge -------------------------------------------------------------


class Spring:
    """x'' = -k (x - target) - c x'. Stiff enough to feel quick, a touch under-damped so it
    settles with the faintest overshoot, the way the Mac's island moves."""

    def __init__(self, value: float, k: float = 340.0, c: float = 30.0) -> None:
        self.x, self.v, self.target, self.k, self.c = value, 0.0, value, k, c

    def step(self, dt: float) -> None:
        a = -self.k * (self.x - self.target) - self.c * self.v
        self.v += a * dt
        self.x += self.v * dt

    @property
    def settled(self) -> bool:
        return abs(self.x - self.target) < 0.5 and abs(self.v) < 5.0


class Shape:
    """Animates the window to a target box (physical pixels) with a corner radius."""

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self.springs: dict[str, Spring] = {}
        self.wake = threading.Event()
        self.visible = True
        self.hide_when_settled = False  # going away: shrink into the top edge, then hide
        self.last: tuple[int, int, int, int, int] | None = None
        self._lock = threading.Lock()
        self.alpha, self.alpha_target = 255.0, 255  # see-through as a ghost (island_logic)
        self.click_through = False

    def go(
        self, cx: float, top: float, w: float, h: float, r: float, *, jump: bool = False
    ) -> None:
        with self._lock:
            for name, value in (("cx", cx), ("top", top), ("w", w), ("h", h), ("r", r)):
                s = self.springs.get(name)
                if s is None or jump:
                    self.springs[name] = Spring(value)
                else:
                    s.target = value
        self.wake.set()

    def ghost(self, alpha: int, click_through: bool) -> None:
        """Fade to `alpha` (0-255) and let clicks through to what's under it, or not. The
        clicks change at once, so a click meant for the app is never caught mid-fade."""
        if click_through != self.click_through:
            self.click_through = click_through
            with contextlib.suppress(Exception):
                style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE) & ~WS_EX_TRANSPARENT
                if click_through:
                    style |= WS_EX_TRANSPARENT
                user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED)
        if alpha != self.alpha_target:
            self.alpha_target = alpha
            self.wake.set()

    def _fade(self) -> bool:
        """One frame of the fade; True once it's there. About a sixth of a second end to end."""
        gap = self.alpha_target - self.alpha
        if abs(gap) < 2:
            if self.alpha != self.alpha_target:
                self.alpha = float(self.alpha_target)
                user32.SetLayeredWindowAttributes(self.hwnd, 0, int(self.alpha), LWA_ALPHA)
            return True
        self.alpha += gap * 0.3
        level = max(0, min(255, round(self.alpha)))
        user32.SetLayeredWindowAttributes(self.hwnd, 0, level, LWA_ALPHA)
        return False

    def run(self) -> None:
        frame = 1 / 60
        while True:
            self.wake.wait()
            t = time.perf_counter()
            faded = self._fade()
            with self._lock:
                for s in self.springs.values():
                    for _ in range(2):  # two half-steps a frame: stable at this stiffness
                        s.step(frame / 2)
                done = all(s.settled for s in self.springs.values())
                if done:
                    for s in self.springs.values():
                        s.x, s.v = s.target, 0.0
                    if faded:
                        self.wake.clear()
                box = {k: s.x for k, s in self.springs.items()}
            if box:
                self.apply(box)
            if done and self.hide_when_settled:
                self.hide_when_settled = False
                self.show(False)
            time.sleep(max(0.0, frame - (time.perf_counter() - t)))

    def apply(self, b: dict[str, float]) -> None:
        w, h = max(8, round(b["w"])), max(8, round(b["h"]))
        x, y = round(b["cx"] - w / 2), round(b["top"])
        r = max(2, round(b["r"]))
        key = (x, y, w, h, r)
        if key == self.last:
            return
        self.last = key
        # The shape first, then the size: growing, the new shape is already there to clip
        # the bigger window; shrinking, it clips the old one before it gets smaller. The other
        # way round shows one frame of square corners. Flush with the screen's top edge: the
        # top corners square, the bottom ones round.
        rgn = gdi32.CreateRoundRectRgn(0, -2 * r, w + 1, h + 1, 2 * r, 2 * r)
        user32.SetWindowRgn(self.hwnd, rgn, False)  # the window owns the region from here
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, w, h, SWP_NOACTIVATE)

    def show(self, on: bool) -> None:
        if on != self.visible:
            self.visible = on
            user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE if on else SW_HIDE)


# --- the window's API -------------------------------------------------------------------------


class IslandApi(popup.Api):
    """The popup's Api (view, ask, talk, stop, pin, hold, dismiss, links, dashboard) plus what
    the island page needs: hover, pages, its measured height, cards, music controls. Closing
    collapses it to the pill; it never destroys the window."""

    def __init__(self, cfg: Config, island: Island) -> None:
        super().__init__(cfg, demo=False)
        self._island = island

    # popup.Api behaviour that doesn't apply: no resizing here, and close means collapse.
    def _resize(self, big: bool) -> None:
        return None

    def view(self) -> dict[str, Any]:
        self._in_view = True  # a close from here is the turn's auto-close, not you
        try:
            return super().view()
        finally:
            self._in_view = False

    def close(self) -> None:
        """✕, Esc: back to the pill. From the popup's auto-close (inside view()): the turn is
        over and read, which only ends the turn (a page you asked for, or the mouse on it,
        keeps the island open)."""
        if getattr(self, "_in_view", False):
            self._island.turn_over()
        else:
            self._island.collapse()

    def hover(self, on: bool) -> bool:
        """The page's own mouseenter/leave. Hover is read from the pointer's position in
        Python instead (Island.poll_pointer): a ghost pill lets clicks through, so its page
        never sees the mouse. Kept so the page's calls still succeed."""
        return True

    def page(self, name: str) -> str:
        return self._island.set_page(str(name))

    def measured(self, height: int) -> bool:
        self._island.voice_h = int(height)
        self._island.poke()
        return True

    def typing(self, on: bool) -> bool:
        self._island.typing = bool(on)
        self._island.poke()
        return True

    def cards(self, name: str) -> dict[str, Any]:
        """The data for a page while it's shown (Donna's comes from the voice status)."""
        from personal_os.interfaces import island_data
        from personal_os.storage.db import open_db

        name = str(name)
        if name == "music":  # your music source only (config [music], ADR-084)
            try:
                return island_data.music(self._island.cfg)
            except Exception as exc:
                return {"available": False, "why": f"Couldn't check ({type(exc).__name__})."}
        if name not in ("today", "projects", "notes"):
            return {}
        try:
            conn = open_db(self._cfg.db_path)
        except Exception:
            return {"error": "Can't read your history right now."}
        try:
            now = datetime.now(UTC)
            if name == "today":
                return island_data.today(conn, now)
            if name == "notes":
                return island_data.notes(conn, now)
            return {"projects": island_data.projects(conn, now)}
        except Exception as exc:
            return {"error": f"Couldn't load this ({type(exc).__name__})."}
        finally:
            conn.close()

    def _write(self, fn: Any, *args: Any) -> Any:
        """One change to your own items (a to-do ticked, a note added), as the dashboard does."""
        from personal_os.core.clock import SystemClock
        from personal_os.storage.db import open_db

        conn = open_db(self._cfg.db_path)
        try:
            return fn(conn, SystemClock(), *args)
        finally:
            conn.close()

    def todo(self, item_id: str, done: bool) -> bool:
        """☐ / ☑ on Today: tick a to-do off, or back on."""
        from personal_os.interfaces import island_data

        try:
            return bool(self._write(island_data.set_todo, str(item_id), bool(done)))
        except Exception:
            return False

    def add(self, kind: str, text: str, project: str = "") -> str:
        """The box on Today (a to-do) or Notes (a note). Returns "" or why it wasn't added."""
        from personal_os.interfaces import island_data

        fn = {"todo": island_data.add_todo, "note": island_data.add_note}.get(str(kind))
        if fn is None:
            return "Can't add that here."
        try:
            return str(self._write(fn, str(text)[:5000], str(project)[:80]))
        except Exception as exc:
            return f"Couldn't save it ({type(exc).__name__})."

    def media(self, action: str) -> dict[str, Any]:
        """The Music page's buttons (and "open"): your music source only. {"ok", "text"}."""
        from personal_os.interfaces import island_data

        return island_data.music_action(self._island.cfg, str(action))

    def open_full(self, page: str) -> str:
        """The full dashboard window, on the page that matches this card."""
        if not runtime.daemon_info(self._cfg.runtime_dir).running:
            return "Capture isn't running, so the dashboard isn't either."
        from personal_os.interfaces import app

        path = {"today": "/", "projects": "/projects", "donna": "/donna"}.get(str(page), "/")
        app.open_app(self._cfg, path)
        self._island.collapse()
        return ""

    def focus(self) -> None:
        """You clicked in it: take the keyboard (it opens without it), and put it in the
        page, so typing goes to the box."""
        super().focus()
        self._island.focused = True
        win = self._window
        with contextlib.suppress(Exception):
            from System import Action  # type: ignore[import-not-found]

            form = win.native
            form.Invoke(Action(lambda: (form.Activate(), form.webview.Focus())))


# --- the controller -------------------------------------------------------------------------


class Island:
    """Decides (island_logic) and drives the shape and the page, ten times a second."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.api = IslandApi(cfg, self)
        self.window: Any = None
        self.shape: Shape | None = None
        self.hwnd = 0
        self.page = "today"
        self.opened_by: str | None = None
        self.hover_since: float | None = None
        self.left_at: float | None = None
        self.request: str | None = None
        self.request_at = 0.0
        self.typing = False
        self.focused = False
        self.voice_h = L.VOICE_MIN_H
        self.monitor = (0, (0, 0, 1280, 800), 1.0)
        self.fullscreen = False
        self.music = False
        self.turn_up = False  # a turn is on screen (from its start until it's read)
        self.collapse_asked = False
        self.view: dict[str, Any] = {}
        self.target = L.Target("hidden", "today", "start")  # nothing shown until wanted
        self.ghost = False  # a pill over an app: see-through, clicks go to the app (ADR-083)
        self.pointer_on = False
        self._pushed = ""
        self._poke = threading.Event()

    def poke(self) -> None:
        self._poke.set()

    # page calls (on pywebview's threads)
    def on_hover(self, on: bool) -> None:
        now = time.monotonic()
        if on:
            self.hover_since = self.hover_since or now
            self.left_at = None
        else:
            self.hover_since = None
            self.left_at = now
        self.poke()

    def set_page(self, name: str) -> str:
        if name in L.PAGES:
            self.page = name
            if self.request:
                self.request = name
            self.poke()
        return self.page

    def turn_over(self) -> None:
        self.collapse_asked = True
        self.turn_up = False
        self.poke()

    def collapse(self) -> None:
        """Esc, ✕, the turn read: back to the pill (unless the mouse is on it)."""
        self.collapse_asked = True
        self.turn_up = False
        self.request = None
        self.poke()

    # the loop
    def run(self) -> None:
        last_slow = 0.0
        last_view = 0.0
        failures = 0
        while True:
            try:
                now = time.monotonic()
                if now - last_slow >= 1.0:
                    last_slow = now
                    self._slow_checks()
                fast = self.target.mode == "open" or self.turn_up
                if now - last_view >= (0.1 if fast else 0.3):
                    last_view = now
                    self._read_voice()
                page = take_page(self.cfg.runtime_dir)
                if page == "hide":
                    self.collapse()
                elif page:
                    self.request, self.request_at, self.page = page, now, page
                    self.monitor = cursor_monitor()
                self.poll_pointer()
                self._decide(now)
                failures = 0
                if DEBUG and int(now) % 5 == 0 and now - int(now) < 0.06:
                    print(f"island: alive, {self.target}", flush=True)
            except Exception as exc:  # one bad step never stops the island
                failures += 1
                if failures in (1, 50):
                    import traceback

                    traceback.print_exc()
                    popup.record_error(self.cfg.runtime_dir, f"island: {type(exc).__name__}: {exc}")
                time.sleep(0.5)
            self._poke.wait(0.05)
            self._poke.clear()

    def _rect(self) -> tuple[int, int, int, int] | None:
        """Where the island is on screen now (left, top, right, bottom), or None if hidden."""
        if self.shape is None or not self.shape.visible or self.shape.last is None:
            return None
        x, y, w, h, _r = self.shape.last
        return x, y, x + w, y + h

    def _app_under(self, rect: tuple[int, int, int, int]) -> bool:
        """The app in front covers the island's spot (a browser's tabs, a title bar)."""
        fg = user32.GetForegroundWindow()
        if not fg or fg == self.hwnd:
            return False
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(fg, cls, 64)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return False  # the desktop: nothing under the pill to click
        r = wintypes.RECT()
        user32.GetWindowRect(fg, ctypes.byref(r))
        return L.overlaps((r.left, r.top, r.right, r.bottom), rect)

    def poll_pointer(self) -> None:
        """Hover, read from the pointer's position (a ghost lets clicks through, so its page
        never sees the mouse). As a ghost, only the pointer held against the top edge on it
        counts as hovering; anywhere on it fades it further so you see what's under it."""
        rect = self._rect()
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        inside = rect is not None and rect[0] <= pt.x < rect[2] and rect[1] <= pt.y < rect[3]
        self.ghost = rect is not None and L.ghosted(self.target, self._app_under(rect))
        edge = round(L.EDGE_PX * self.monitor[2])
        hovering = inside and (not self.ghost or (rect is not None and pt.y <= rect[1] + edge))
        if hovering != (self.hover_since is not None):
            self.on_hover(hovering)
        self.pointer_on = inside
        if self.shape is not None:
            self.shape.ghost(L.alpha(self.ghost, inside), self.ghost)

    def _read_voice(self) -> None:
        self.collapse_asked = False
        v = self.api.view()  # also: auto-close (calls turn_over), the "seen" handshake
        first = not self.view
        # The turn on screen when the island starts is an old one: not a reason to open.
        if (v.get("new_turn") and not first) or v.get("state") in ACTIVE:
            if not self.turn_up:
                self.monitor = cursor_monitor()  # open where you are
            self.turn_up = not self.collapse_asked
        self.view = v

    def _slow_checks(self) -> None:
        """Once a second: the screen with the mouse, full-screen apps, music, config."""
        if self.target.mode != "open":
            mon = cursor_monitor()
            if mon[0] != self.monitor[0]:
                self.monitor = mon
                self._place(jump=True)
        self.fullscreen = fullscreen_on(self.monitor[0], self.monitor[1], self.hwnd)
        if self.cfg.music.source == "any":  # any app making sound, as before
            voice_pid = int(self.view.get("pid") or 0) if isinstance(self.view, dict) else 0
            self.music = music_playing({os.getpid(), voice_pid})

    def _watch_music(self) -> None:
        """Every few seconds, on its own thread (it asks the browser and Windows): is your
        music source playing? Only that lights the pill's bars (ADR-084)."""
        from personal_os.actions import music

        while True:
            src = music.chosen(self.cfg.music.source)
            if src is not None:
                try:
                    b = music.LiveBackend(self.cfg.runtime_dir, self.cfg.actions.browser)
                    self.music = music.playing_now(src, b)
                except Exception:
                    self.music = False
            time.sleep(3)

    def inputs(self, now: float) -> L.Inputs:
        v = self.view
        return L.Inputs(
            turn_active=self.turn_up,
            alerts=len(v.get("alerts") or []),
            hovered_s=(now - self.hover_since) if self.hover_since else None,
            left_s=(now - self.left_at) if self.left_at else None,
            request=self.request,
            request_age_s=now - self.request_at,
            pinned=bool(v.get("pinned")),
            typing=self.typing,
            fullscreen=self.fullscreen,
            music=self.music,
            voice_running=bool(v.get("voice_running")),
            idle_pill=self.cfg.voice.island_idle == "pill",
            ghost=self.ghost,
        )

    def _decide(self, now: float) -> None:
        i = self.inputs(now)
        target, by = L.decide(i, self.page, self.opened_by)
        if target.mode != "open" and self.request:
            self.request = None  # its time is up
        opening = target.mode == "open" and self.target.mode != "open"
        if opening and by == "hover":
            self.monitor = cursor_monitor()
        if target.mode != "open" and self.focused:
            self._drop_focus()
        if DEBUG and (target.mode, target.page) != (self.target.mode, self.target.page):
            print(f"island: {self.target.mode}/{self.target.page} -> {target}", flush=True)
        self.page, self.opened_by, self.target = target.page, by, target
        self._place()
        self._push(i)

    def _place(self, *, jump: bool = False) -> None:
        if self.shape is None:
            return
        i = self.inputs(time.monotonic())
        _mon, bounds, scale = self.monitor
        # Hidden is a sliver tucked into the top edge: it shrinks into it before vanishing,
        # and comes out of it when shown, like the Mac's island.
        sx, sy, sw, sh = L.place(bounds, scale, L.PILL[0] * 3 // 4, L.TUCKED_H)
        tucked = (sx + sw / 2, sy, sw, sh, sh / 2)
        shape = self.shape
        if self.target.mode == "hidden":
            if shape.visible and not jump:
                shape.hide_when_settled = True
                shape.go(*tucked)
            else:
                shape.go(*tucked, jump=True)
                shape.show(False)
            return
        if not shape.visible:
            shape.go(*tucked, jump=True)  # start from the edge...
            shape.show(True)
        shape.hide_when_settled = False
        w, h = L.size(self.target, i, self.voice_h)
        x, y, pw, ph = L.place(bounds, scale, w, h)
        r = L.radius(self.target, h) * scale
        shape.go(x + pw / 2, y, pw, ph, r, jump=jump)  # ...and spring open

    def _push(self, i: L.Inputs) -> None:
        state = {
            "mode": self.target.mode, "page": self.page, "view": self.view,
            "music": i.music, "opened_by": self.opened_by,
        }  # fmt: skip
        data = json.dumps(state, ensure_ascii=False, default=str)
        if data == self._pushed or self.window is None:
            return
        self._pushed = data
        with contextlib.suppress(Exception):
            self.window.evaluate_js(f"window.island && window.island.update({data})")

    def _drop_focus(self) -> None:
        """Back to a pill: give the keyboard back and never take it again unasked."""
        self.focused = False
        with contextlib.suppress(Exception):
            style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE)

    # start-up, once the window exists
    def started(self) -> None:
        self.hwnd = int(self.window.native.Handle.ToInt32())
        dwm = ctypes.windll.dwmapi
        off, square = ctypes.c_int(1), ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(self.hwnd, 2, ctypes.byref(off), 4)  # no frame shadow
        dwm.DwmSetWindowAttribute(self.hwnd, 33, ctypes.byref(square), 4)  # no OS rounding
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        style = (style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED) & ~WS_EX_APPWINDOW
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style)  # no taskbar button, no Alt+Tab
        user32.SetLayeredWindowAttributes(self.hwnd, 0, 255, LWA_ALPHA)  # solid until a ghost
        self.monitor = cursor_monitor()
        self.shape = Shape(self.hwnd)
        self._place(jump=True)
        threading.Thread(target=self.shape.run, name="pos-island-shape", daemon=True).start()
        threading.Thread(target=self.run, name="pos-island", daemon=True).start()
        threading.Thread(target=self._watch_config, name="pos-island-cfg", daemon=True).start()
        threading.Thread(target=self._watch_music, name="pos-island-music", daemon=True).start()

    def _watch_config(self) -> None:
        """Gone when voice is turned off, or the popup style changes back to the corner."""
        from personal_os.config.loader import load_config

        while True:
            time.sleep(10)
            try:
                cfg = load_config()
            except Exception:
                continue
            if not wanted(cfg) or runtime.stop_requested(cfg.runtime_dir):
                os._exit(0)
            self.api._auto.seconds = cfg.voice.popup_autoclose_s
            self.cfg = cfg  # island_idle, the browser, ... as you change them


def page_html() -> str:
    from importlib.resources import files

    return (files("personal_os.interfaces") / "island.html").read_text("utf-8")


def main(argv: list[str] | None = None) -> int:
    from personal_os.config.loader import load_config

    cfg = load_config()
    lock = runtime.InstanceLock(cfg.runtime_dir / LOCK)
    try:
        lock.acquire()
    except runtime.AlreadyRunningError:
        return 0
    try:
        return _run(cfg)
    except Exception as exc:
        popup.record_error(cfg.runtime_dir, f"island: {type(exc).__name__}: {exc}")
        return 0
    finally:
        lock.release()


def _run(cfg: Config) -> int:
    reason = popup.unavailable_reason()
    if reason:
        popup.record_error(cfg.runtime_dir, reason)
        return 0
    with contextlib.suppress(Exception):  # sizes in real pixels on every screen
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    # Where the page hasn't painted yet while the island grows, black, like the island
    # (WebView2's own default is a dark grey that shows as a band at the edge).
    os.environ["WEBVIEW2_DEFAULT_BACKGROUND_COLOR"] = "FF000000"
    import webview

    island = Island(cfg)
    w, h = L.PILL
    window = webview.create_window(
        TITLE, html=page_html(), js_api=island.api, width=w, height=h, x=0, y=0,
        resizable=True, frameless=True, easy_drag=False, on_top=True, focus=False,
        min_size=(8, 8), shadow=False, background_color="#000000",
    )  # fmt: skip
    if window is None:
        return 0
    island.window = window
    island.api._window = window  # the popup's focus() needs it (it did nothing without)
    window.events.shown += lambda: threading.Timer(0.05, island.started).start()
    webview.start(gui="edgechromium", private_mode=True, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
