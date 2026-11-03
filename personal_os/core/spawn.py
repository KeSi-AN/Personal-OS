"""Start Personal OS background processes with no console window (see personal_os/boot.py)."""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

SITE_ENV = "PERSONAL_OS_SITE"
BOOT = Path(__file__).resolve().parents[1] / "boot.py"


def site_packages() -> str:
    """This project's site-packages. Inherited through the environment, because a process
    started via boot.py runs under the base interpreter, whose own purelib is different."""
    return os.environ.get(SITE_ENV) or sysconfig.get_paths()["purelib"]


def windowless_python() -> Path:
    base = Path(getattr(sys, "_base_executable", None) or sys.executable)
    for candidate in (base.with_name("pythonw.exe"), Path(sys.executable).with_name("pythonw.exe")):
        if candidate.exists():
            return candidate
    return base  # non-Windows: there is no console to avoid


def module_command(module: str, *args: str) -> list[str]:
    return [str(windowless_python()), "-I", str(BOOT), site_packages(), module, *args]


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env[SITE_ENV] = site_packages()
    env.update(extra or {})
    return env


def creation_flags(detached: bool) -> int:
    if sys.platform != "win32":
        return 0
    flags = subprocess.CREATE_NO_WINDOW
    if detached:
        flags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    return flags
