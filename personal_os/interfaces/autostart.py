"""Start at logon via the per-user Startup folder (no admin rights, easy to see and remove).

Task Scheduler logon triggers need administrator rights, so the Startup folder is used
instead (ADR-024). The entry is a tiny .vbs that starts the launcher with no console window.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENTRY_NAME = "PersonalOS.vbs"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def entry_path() -> Path:
    return startup_dir() / ENTRY_NAME


def pythonw() -> Path:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def script(home: Path | None) -> str:
    env = ""
    if home is not None:
        env = f'sh.Environment("PROCESS")("PERSONAL_OS_HOME") = "{home}"\n'
    return (
        "' Personal OS: start the capture daemon at logon. Remove with "
        "`personal-os autostart disable`.\n"
        'Set sh = CreateObject("WScript.Shell")\n'
        f"{env}"
        f'sh.Run """{pythonw()}"" -m personal_os.launcher", 0, False\n'
    )


def enable(home: Path | None = None) -> Path:
    path = entry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script(home), encoding="utf-8")
    return path


def disable() -> bool:
    path = entry_path()
    if path.exists():
        path.unlink()
        return True
    return False


def is_enabled() -> bool:
    return entry_path().exists()
