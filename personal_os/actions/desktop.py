"""The desktop operations tools need, behind one interface: Windows (ctypes, no new
dependencies) and a fake that records calls for tests."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

MEDIA_KEYS = {
    "play_pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
    "mute": 0xAD,
    "volume_down": 0xAE,
    "volume_up": 0xAF,
}
WINDOW_ACTIONS = ("focus", "minimize", "maximize", "restore")


@dataclass
class WindowInfo:
    handle: int
    title: str
    state: str  # "minimized" | "maximized" | "normal"


class Desktop(Protocol):
    def open_path(self, path: Path) -> None: ...
    def open_url(self, url: str) -> None: ...
    def launch(self, argv: list[str]) -> int: ...
    def media_key(self, action: str) -> None: ...
    def get_clipboard(self) -> str | None: ...
    def set_clipboard(self, text: str) -> None: ...
    def screenshot(self, path: Path) -> None: ...
    def windows(self, title_part: str) -> list[WindowInfo]: ...
    def set_window(self, handle: int, action: str) -> None: ...
    def recycle(self, path: Path) -> None: ...
    def get_brightness(self) -> int | None: ...
    def set_brightness(self, percent: int) -> None: ...
    def get_volume(self) -> int | None: ...
    def set_volume(self, percent: int) -> None: ...
    def lock_screen(self) -> None: ...
    def reveal(self, path: Path) -> None: ...


@dataclass
class FakeDesktop:
    """Records what would have happened; clipboard and windows are simulated."""

    calls: list[tuple[str, object]] = field(default_factory=list)
    clipboard: str | None = "before"
    open_windows: list[WindowInfo] = field(default_factory=list)
    brightness: int | None = 50
    volume: int | None = 30

    def get_volume(self) -> int | None:
        return self.volume

    def set_volume(self, percent: int) -> None:
        self.calls.append(("set_volume", percent))
        self.volume = percent

    def lock_screen(self) -> None:
        self.calls.append(("lock_screen", None))

    def reveal(self, path: Path) -> None:
        self.calls.append(("reveal", str(path)))

    def get_brightness(self) -> int | None:
        return self.brightness

    def set_brightness(self, percent: int) -> None:
        self.calls.append(("set_brightness", percent))
        self.brightness = percent

    def open_path(self, path: Path) -> None:
        self.calls.append(("open_path", str(path)))

    def open_url(self, url: str) -> None:
        self.calls.append(("open_url", url))

    def launch(self, argv: list[str]) -> int:
        self.calls.append(("launch", list(argv)))
        return 4242

    def media_key(self, action: str) -> None:
        self.calls.append(("media_key", action))

    def get_clipboard(self) -> str | None:
        return self.clipboard

    def set_clipboard(self, text: str) -> None:
        self.calls.append(("set_clipboard", text))
        self.clipboard = text

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG fake")
        self.calls.append(("screenshot", str(path)))

    def windows(self, title_part: str) -> list[WindowInfo]:
        return [w for w in self.open_windows if title_part.lower() in w.title.lower()]

    def set_window(self, handle: int, action: str) -> None:
        self.calls.append(("set_window", (handle, action)))
        for w in self.open_windows:
            if w.handle == handle:
                w.state = {"minimize": "minimized", "maximize": "maximized"}.get(action, "normal")

    def recycle(self, path: Path) -> None:
        self.calls.append(("recycle", str(path)))
        if path.is_dir():
            import shutil

            shutil.rmtree(path)
        else:
            path.unlink()


class WindowsDesktop:  # pragma: no cover - exercised by the live tests and the demo
    def open_path(self, path: Path) -> None:
        os.startfile(str(path))

    def open_url(self, url: str) -> None:
        os.startfile(url)

    def launch(self, argv: list[str]) -> int:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        proc = subprocess.Popen(
            argv,
            creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return proc.pid

    def media_key(self, action: str) -> None:
        from personal_os.platform import windows as w

        w.press_key(MEDIA_KEYS[action])

    def get_clipboard(self) -> str | None:
        from personal_os.platform import windows as w

        return w.get_clipboard_text()

    def set_clipboard(self, text: str) -> None:
        from personal_os.platform import windows as w

        w.set_clipboard_text(text)

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        script = (
            "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
            "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
            "$i=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
            "$g=[System.Drawing.Graphics]::FromImage($i);"
            "$g.CopyFromScreen($b.Left,$b.Top,0,0,$i.Size);"
            "$i.Save($env:POS_SHOT,[System.Drawing.Imaging.ImageFormat]::Png)"
        )
        env = dict(os.environ, POS_SHOT=str(path))  # the path never goes through the command line
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env=env,
            check=True,
            timeout=30,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def windows(self, title_part: str) -> list[WindowInfo]:
        from personal_os.platform import windows as w

        return [
            WindowInfo(h, t, s)
            for h, t, s in w.top_level_windows()
            if title_part.lower() in t.lower()
        ]

    def set_window(self, handle: int, action: str) -> None:
        from personal_os.platform import windows as w

        w.set_window(handle, action)

    def recycle(self, path: Path) -> None:
        from personal_os.platform import windows as w

        w.recycle(path)

    def _powershell(self, script: str) -> str:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if out.returncode != 0:
            from personal_os.actions.base import ActionError

            raise ActionError("this screen's brightness can't be set from Windows")
        return out.stdout.strip()

    def get_brightness(self) -> int | None:
        try:
            value = self._powershell(
                "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness)"
                ".CurrentBrightness"
            )
            return int(value.splitlines()[0]) if value else None
        except Exception:
            return None

    def set_brightness(self, percent: int) -> None:
        self._powershell(
            "Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | "
            "Invoke-CimMethod -MethodName WmiSetBrightness "
            f"-Arguments @{{Timeout=1; Brightness={int(percent)}}} | Out-Null"
        )

    def _endpoint(self) -> Any:
        import comtypes
        from pycaw.pycaw import AudioUtilities

        comtypes.CoInitialize()  # each thread that uses COM must
        return AudioUtilities.GetSpeakers().EndpointVolume

    def get_volume(self) -> int | None:
        try:
            return round(float(self._endpoint().GetMasterVolumeLevelScalar()) * 100)
        except Exception:
            return None

    def set_volume(self, percent: int) -> None:
        ep = self._endpoint()
        ep.SetMute(0, None)
        ep.SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100.0, None)

    def lock_screen(self) -> None:
        import ctypes

        ctypes.WinDLL("user32").LockWorkStation()

    def reveal(self, path: Path) -> None:
        # Explorer with the item selected; the path is one argument, never a shell string.
        subprocess.Popen(["explorer.exe", f"/select,{path}"], close_fds=True)


def default_desktop() -> Desktop:
    if sys.platform == "win32":
        return WindowsDesktop()
    raise NotImplementedError("desktop actions are Windows-only for now (DESIGN.md §6.3)")
