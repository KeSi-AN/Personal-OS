"""Runtime files shared by the daemon, launcher and CLI: lock, health snapshot, stop request."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import psutil

HEALTH_FILE = "health.json"
STOP_FILE = "stop-request"
DAEMON_LOCK = "daemon.lock"
LAUNCHER_LOCK = "launcher.lock"


class AlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    """An exclusive OS lock on a file, held for the process lifetime. Released if we crash."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO[str] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+", encoding="utf-8")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise AlreadyRunningError(f"another instance holds {self.path.name}") from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._fh.close()
            self._fh = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, default=str, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_health(runtime_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((runtime_dir / HEALTH_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class DaemonInfo:
    running: bool
    pid: int | None
    health: dict[str, Any] | None


def daemon_info(runtime_dir: Path) -> DaemonInfo:
    """Is a daemon running for this data dir? Checks the pid in the health file is alive."""
    health = read_health(runtime_dir)
    pid = health.get("pid") if health else None
    running = False
    if isinstance(pid, int) and psutil.pid_exists(pid):
        try:
            proc = psutil.Process(pid)
            running = abs(proc.create_time() - float(health.get("process_started", 0))) < 2  # type: ignore[union-attr]
        except psutil.Error:
            running = False
    return DaemonInfo(running, pid if running else None, health)


def request_stop(runtime_dir: Path) -> None:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / STOP_FILE).write_text("stop", encoding="utf-8")


def stop_requested(runtime_dir: Path) -> bool:
    return (runtime_dir / STOP_FILE).exists()


def clear_stop(runtime_dir: Path) -> None:
    (runtime_dir / STOP_FILE).unlink(missing_ok=True)
