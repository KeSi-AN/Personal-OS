"""What's happening on this laptop right now: windows, browser tabs, idle, reminders.

History says what you did; this says what's open. Windows are read straight from Windows (always
current); browser tabs come from the extension's latest list (runtime/now/tabs-<browser>.json,
replaced on every change, never stored in the database). Everything passes the same privacy
rules as capture: blocked apps and domains are left out, sensitive titles and URLs redacted,
incognito never sent. The result is captured content: it taints what follows (ADR-050).
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from personal_os.config.schema import Config
from personal_os.privacy.redact import TextRedactor
from personal_os.privacy.rules import NON_WEB_SCHEMES, PrivacyRules, domain_of, scheme_of

TABS_STALE_S = 180  # the extension refreshes every minute while the browser runs
MAX_WINDOWS = 40
# Windows' own surfaces, not apps you'd call "open".
_SHELL = frozenset(
    {
        *("textinputhost.exe", "shellexperiencehost.exe", "searchhost.exe", "lockapp.exe"),
        *("startmenuexperiencehost.exe", "applicationframehost.exe", "systemsettings.exe"),
        *("nvidia share.exe", "widgets.exe", "pythonw.exe"),
    }
)


@dataclass
class Window:
    app: str
    title: str
    front: bool = False
    exe: str = ""
    exe_path: str = ""


@dataclass
class Tab:
    browser: str
    title: str
    url: str
    active: bool = False
    audible: bool = False


@dataclass
class Now:
    at: datetime
    idle_s: int = 0
    windows: list[Window] = field(default_factory=list)
    tabs: list[Tab] = field(default_factory=list)
    tabs_known: dict[str, bool] = field(default_factory=dict)  # browser -> list is fresh
    paused: bool = False
    killed: bool = False
    reminders: list[str] = field(default_factory=list)

    def render(self) -> str:
        """For the model: short lines, front window first."""
        out = [f"time: {self.at.astimezone():%A %d %B, %H:%M}"]
        out.append(f"you: {'idle for ' + _mins(self.idle_s) if self.idle_s >= 120 else 'active'}")
        if self.paused:
            out.append("capture: paused")
        if self.killed:
            out.append("actions: off (kill switch)")
        front = [w for w in self.windows if w.front]
        if front:
            out.append(f"in front: {front[0].app} - {front[0].title}")
        others = [w for w in self.windows if not w.front]
        if others:
            out.append("other windows: " + "; ".join(f"{w.app} - {w.title}" for w in others))
        by_browser: dict[str, list[Tab]] = {}
        for t in self.tabs:
            by_browser.setdefault(t.browser, []).append(t)
        for browser, tabs in by_browser.items():
            items = "; ".join(_tab_line(t) for t in tabs)
            out.append(f"{browser} tabs ({len(tabs)}): {items}")
        for browser, fresh in self.tabs_known.items():
            if not fresh:
                out.append(f"{browser} tabs: not known (the extension hasn't reported recently)")
        if not self.tabs_known:
            out.append("browser tabs: not known (no browser extension has reported)")
        if self.reminders:
            out.append("next reminders: " + "; ".join(self.reminders))
        return "\n".join(out)


def _tab_line(t: Tab) -> str:
    marks = (" (current)" if t.active else "") + (" (playing sound)" if t.audible else "")
    return f"{t.title or t.url}{marks}"


def _mins(s: int) -> str:
    return f"{s // 60} min" if s < 3600 else f"{s // 3600} h {s % 3600 // 60} min"


def idle_seconds() -> int:
    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes

    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LastInput()
    info.cbSize = ctypes.sizeof(info)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):  # type: ignore[attr-defined,unused-ignore]
        return 0
    ticks = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF  # type: ignore[attr-defined,unused-ignore]
    return int(max(0, (ticks - info.dwTime) & 0xFFFFFFFF) // 1000)


def windows(rules: PrivacyRules, redactor: TextRedactor) -> list[Window]:
    """Visible app windows (not tool windows, not hidden "cloaked" ones), front one first."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    import psutil

    user32 = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
    dwm = ctypes.windll.dwmapi  # type: ignore[attr-defined,unused-ignore]
    front = user32.GetForegroundWindow()
    found: list[Window] = []
    names: dict[int, str] = {}
    paths: dict[int, str] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)  # type: ignore[untyped-decorator,misc,unused-ignore]
    def visit(hwnd: int, _lp: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # GW_OWNER
            return True
        if user32.GetWindowLongW(hwnd, -20) & 0x80:  # WS_EX_TOOLWINDOW
            return True
        cloaked = wintypes.DWORD()
        dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in names:
            try:
                proc = psutil.Process(pid.value)
                names[pid.value] = proc.name()
                with contextlib.suppress(psutil.Error):
                    paths[pid.value] = proc.exe()
            except psutil.Error:
                names[pid.value] = ""
        exe = names[pid.value]
        if exe.lower() in _SHELL or rules.is_blocked_app(exe, exe.removesuffix(".exe")):
            return True
        title = buf.value
        if any(r.search(title) for r in rules.redact_title_res):
            title = "(title hidden)"
        else:
            title, _ = redactor.redact(title[:200], "title")
        found.append(
            Window(exe.removesuffix(".exe"), title, front=hwnd == front, exe=exe,
                   exe_path=paths.get(pid.value, ""))
        )  # fmt: skip
        return True

    user32.EnumWindows(visit, 0)
    found.sort(key=lambda w: not w.front)
    return found[:MAX_WINDOWS]


def tabs(
    now_dir: Path, rules: PrivacyRules, redactor: TextRedactor, now_s: float | None = None
) -> tuple[list[Tab], dict[str, bool]]:
    """Each browser's latest tab list (filtered), and whether each list is fresh."""
    out: list[Tab] = []
    known: dict[str, bool] = {}
    if not now_dir.is_dir():
        return out, known
    now_s = time.time() if now_s is None else now_s
    for f in sorted(now_dir.glob("tabs-*.json")):
        try:
            data = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        browser = str(data.get("browser", f.stem.removeprefix("tabs-")))
        fresh = now_s - f.stat().st_mtime <= TABS_STALE_S
        known[browser] = fresh
        if not fresh:
            continue
        for t in data.get("tabs") or []:
            url = str(t.get("url", ""))
            if scheme_of(url) in NON_WEB_SCHEMES or rules.is_blocked_domain(domain_of(url)):
                continue
            title = str(t.get("title", ""))[:200]
            if any(r.search(title) for r in rules.redact_title_res):
                title = "(title hidden)"
            else:
                title, _ = redactor.redact(title, "title")
            out.append(
                Tab(
                    browser,
                    title,
                    redactor.redact_url(url),
                    bool(t.get("active")),
                    bool(t.get("audible")),
                )
            )
    return out, known


def snapshot(cfg: Config, conn: Any = None) -> Now:
    from personal_os.actions import killswitch, reminders
    from personal_os.core.clock import SystemClock

    rules = PrivacyRules.from_config(cfg.privacy, protected_dirs=[cfg.data_dir])
    redactor = TextRedactor()
    now = SystemClock().now()
    tab_list, known = tabs(cfg.runtime_dir / "now", rules, redactor)
    upcoming: list[str] = []
    if conn is not None:
        upcoming = [f'"{r.text}" {reminders.when(r.due, now)}' for r in reminders.pending(conn)[:3]]
    return Now(
        at=now,
        idle_s=idle_seconds(),
        windows=windows(rules, redactor),
        tabs=tab_list,
        tabs_known=known,
        paused=(cfg.runtime_dir / "paused").exists(),
        killed=killswitch.engaged(cfg.runtime_dir),
        reminders=upcoming,
    )
