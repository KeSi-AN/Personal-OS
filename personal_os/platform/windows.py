"""Windows probes via ctypes (ADR-006): foreground window, idle time, session/power events.

Only window metadata is read: handle, owning process, title and class. Never window
contents, keystrokes or the mouse position.
"""

from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

import psutil

from personal_os.core.logging import get_logger
from personal_os.platform.base import NotSupportedError, SessionSignal, WindowSample

if sys.platform != "win32":  # pragma: no cover
    raise NotSupportedError("personal_os.platform.windows imported on a non-Windows OS")

_log = get_logger("platform.windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
version_dll = ctypes.WinDLL("version", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
kernel32.GetTickCount64.restype = ctypes.c_ulonglong
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
wtsapi32.WTSRegisterSessionNotification.argtypes = [wintypes.HWND, wintypes.DWORD]
wtsapi32.WTSUnRegisterSessionNotification.argtypes = [wintypes.HWND]
version_dll.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
version_dll.GetFileVersionInfoW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
]
version_dll.VerQueryValueW.argtypes = [
    wintypes.LPCVOID,
    wintypes.LPCWSTR,
    ctypes.POINTER(wintypes.LPVOID),
    ctypes.POINTER(wintypes.UINT),
]

WM_QUIT = 0x0012
WM_HOTKEY = 0x0312
WM_POWERBROADCAST = 0x0218
WM_WTSSESSION_CHANGE = 0x02B1
PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
WTS_SESSION_LOGON, WTS_SESSION_LOGOFF, WTS_SESSION_LOCK, WTS_SESSION_UNLOCK = 5, 6, 7, 8
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
NOTIFY_FOR_THIS_SESSION = 0

_FRAME_HOST = "applicationframehost.exe"


# --- foreground window ---------------------------------------------------------------------


def _window_text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(min(n, 4096) + 1)
    user32.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, len(buf))
    return buf.value


def _window_pid(hwnd: int) -> int | None:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value or None


def _uwp_child_pid(hwnd: int, frame_pid: int) -> int | None:
    """UWP apps are hosted by ApplicationFrameHost; the real app owns a child window."""
    found: list[int] = []

    def cb(child: int, _lparam: int) -> bool:
        pid = _window_pid(child)
        if pid and pid != frame_pid:
            found.append(pid)
            return False
        return True

    user32.EnumChildWindows(hwnd, WNDENUMPROC(cb), 0)
    return found[0] if found else None


def file_description(path: str) -> str | None:
    """FileDescription from the exe's version resource ("Visual Studio Code" for Code.exe)."""
    try:
        size = version_dll.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(path, 0, size, data):
            return None
        ptr, length = wintypes.LPVOID(), wintypes.UINT()
        if (
            not version_dll.VerQueryValueW(
                data, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(length)
            )
            or not length.value
        ):
            return None
        lang, codepage = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD * 2)).contents
        key = f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription"
        if not version_dll.VerQueryValueW(data, key, ctypes.byref(ptr), ctypes.byref(length)):
            return None
        text = ctypes.wstring_at(ptr, length.value).rstrip("\x00").strip()
        return text or None
    except OSError:
        return None


@dataclass
class _ProcInfo:
    create_time: float
    exe: str | None
    exe_path: str | None
    app: str
    elevated_unreadable: bool
    cwd: str | None = None
    cwd_at: float = 0.0


class WindowsWindowProbe:
    """GetForegroundWindow + psutil, with a per-process cache (process info rarely changes)."""

    CWD_TTL_S = 10.0

    def __init__(self) -> None:
        self._procs: dict[int, _ProcInfo] = {}
        self._descriptions: dict[str, str | None] = {}

    def sample(self) -> WindowSample | None:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = _window_pid(hwnd)
        title = _window_text(hwnd)
        cls = _class_name(hwnd)
        info = self._proc(pid) if pid else None
        is_uwp = False
        if info and info.exe and info.exe.lower() == _FRAME_HOST and pid:
            child = _uwp_child_pid(hwnd, pid)
            if child:
                pid, info, is_uwp = child, self._proc(child), True
        if info is None:
            return WindowSample(
                hwnd=hwnd,
                pid=pid,
                app=title or "unknown",
                title=title,
                window_class=cls,
                elevated_unreadable=True,
            )
        return WindowSample(
            hwnd=hwnd,
            pid=pid,
            app=info.app,
            exe=info.exe,
            exe_path=info.exe_path,
            title=title,
            window_class=cls,
            cwd=self._cwd(pid, info) if pid else None,
            is_uwp=is_uwp,
            elevated_unreadable=info.elevated_unreadable,
        )

    def idle_seconds(self) -> float:
        lii = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
        if not user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        # dwTime is a 32-bit tick count: compare modulo 2^32 so the 49.7-day wrap is harmless.
        now32 = kernel32.GetTickCount64() & 0xFFFFFFFF
        return float((now32 - lii.dwTime) & 0xFFFFFFFF) / 1000.0

    def _proc(self, pid: int | None) -> _ProcInfo | None:
        if not pid:
            return None
        try:
            p = psutil.Process(pid)
            created = p.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        cached = self._procs.get(pid)
        if cached and cached.create_time == created:
            return cached
        exe_path: str | None
        elevated = False
        try:
            exe_path = p.exe() or None
        except (psutil.AccessDenied, psutil.ZombieProcess):
            exe_path, elevated = None, True
        except psutil.NoSuchProcess:
            return None
        exe: str | None
        try:
            exe = p.name()
        except psutil.Error:
            exe = Path(exe_path).name if exe_path else None
        app = self._app_name(exe, exe_path)
        info = _ProcInfo(created, exe, exe_path, app, elevated)
        if len(self._procs) > 512:
            self._procs.clear()
        self._procs[pid] = info
        return info

    def _app_name(self, exe: str | None, exe_path: str | None) -> str:
        if exe_path:
            if exe_path not in self._descriptions:
                self._descriptions[exe_path] = file_description(exe_path)
            desc = self._descriptions[exe_path]
            if desc:
                return desc
        return Path(exe).stem if exe else "unknown"

    def _cwd(self, pid: int, info: _ProcInfo) -> str | None:
        now = time.monotonic()
        if now - info.cwd_at >= self.CWD_TTL_S:
            info.cwd_at = now
            try:
                info.cwd = psutil.Process(pid).cwd() or None
            except psutil.Error:
                info.cwd = None
        return info.cwd


# --- session / power / hotkey --------------------------------------------------------------


class WindowsSessionWatcher:
    """A hidden top-level window on its own thread, receiving WTS session changes, power
    broadcasts (which message-only windows don't get) and the global pause hotkey."""

    HOTKEY_ID = 0x5053  # "PS"

    def __init__(
        self, *, hotkey: tuple[int, int] | None = (MOD_CONTROL | MOD_ALT | MOD_SHIFT, 0x50)
    ):
        self._hotkey = hotkey
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._on_signal: Callable[[SessionSignal], None] | None = None
        self._wndproc = WNDPROC(self._proc)  # keep a reference for the window's lifetime
        self.hotkey_registered = False
        self.error: str | None = None

    def start(self, on_signal: Callable[[SessionSignal], None]) -> None:
        self._on_signal = on_signal
        self._thread = threading.Thread(target=self._run, name="pos-session-watcher", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        if self.error:
            raise OSError(self.error)

    def stop(self) -> None:
        if self._thread_id is not None:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(5)

    def _emit(self, signal: SessionSignal) -> None:
        if self._on_signal is not None:
            try:
                self._on_signal(signal)
            except Exception:  # never let a callback kill the message loop
                _log.warning("session callback failed", exc_info=True)

    def _proc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_WTSSESSION_CHANGE:
            signal = {
                WTS_SESSION_LOCK: "lock",
                WTS_SESSION_UNLOCK: "unlock",
                WTS_SESSION_LOGON: "logon",
                WTS_SESSION_LOGOFF: "logoff",
            }.get(wparam)
            if signal:
                self._emit(signal)  # type: ignore[arg-type]
            return 0
        if msg == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                self._emit("sleep")
            elif wparam in (PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC):
                self._emit("wake")
            return 1
        if msg == WM_HOTKEY and wparam == self.HOTKEY_ID:
            self._emit("hotkey")
            return 0
        return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam))

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        hinst = kernel32.GetModuleHandleW(None)
        class_name = f"PersonalOSSessionWatcher{os.getpid()}"
        wc = WNDCLASSEXW(
            cbSize=ctypes.sizeof(WNDCLASSEXW),
            lpfnWndProc=self._wndproc,
            hInstance=hinst,
            lpszClassName=class_name,
        )
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            self.error = f"RegisterClassExW failed ({ctypes.get_last_error()})"
            self._ready.set()
            return
        # A hidden top-level window (never shown): power broadcasts only reach top-level windows.
        hwnd = user32.CreateWindowExW(
            0, class_name, "Personal OS", 0, 0, 0, 0, 0, None, None, hinst, None
        )
        if not hwnd:
            self.error = f"CreateWindowExW failed ({ctypes.get_last_error()})"
            user32.UnregisterClassW(class_name, hinst)
            self._ready.set()
            return
        wtsapi32.WTSRegisterSessionNotification(hwnd, NOTIFY_FOR_THIS_SESSION)
        if self._hotkey:
            mods, vk = self._hotkey
            self.hotkey_registered = bool(
                user32.RegisterHotKey(hwnd, self.HOTKEY_ID, mods | MOD_NOREPEAT, vk)
            )
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self.hotkey_registered:
                user32.UnregisterHotKey(hwnd, self.HOTKEY_ID)
            wtsapi32.WTSUnRegisterSessionNotification(hwnd)
            user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(class_name, hinst)
