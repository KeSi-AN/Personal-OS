"""Start at logon via the per-user Startup folder (no admin rights, easy to see and remove).

Task Scheduler logon triggers need administrator rights, so the Startup folder is used
instead (ADR-024). The entry is a tiny .vbs that starts the launcher with no window at all:
it runs the base interpreter's pythonw.exe through personal_os/boot.py (see core/spawn.py).
"""

from __future__ import annotations

import os
from pathlib import Path

from personal_os.core import spawn

ENTRY_NAME = "PersonalOS.vbs"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("APPDATA is not set")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def entry_path() -> Path:
    return startup_dir() / ENTRY_NAME


def _vbs_quote(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def script(home: Path | None) -> str:
    command = " ".join(f'"{part}"' for part in spawn.module_command("personal_os.launcher"))
    lines = [
        "' Personal OS: start the capture daemon at logon. "
        "Remove with `personal-os autostart disable`.",
        'Set sh = CreateObject("WScript.Shell")',
        f'sh.Environment("PROCESS")("{spawn.SITE_ENV}") = {_vbs_quote(spawn.site_packages())}',
    ]
    if home is not None:
        lines.append(f'sh.Environment("PROCESS")("PERSONAL_OS_HOME") = {_vbs_quote(str(home))}')
    lines.append(f"sh.Run {_vbs_quote(command)}, 0, False")
    return "\n".join(lines) + "\n"


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
