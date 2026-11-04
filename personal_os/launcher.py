"""Keeps the daemon running: restarts it with backoff if it exits abnormally (ADR-024).

Started in the background by `personal-os daemon start` (and at logon by the Startup-folder
entry from `personal-os autostart enable`). A clean daemon exit or a stop request ends it.
"""

from __future__ import annotations

import subprocess
import sys
import time

from personal_os.config.loader import ensure_dirs, load_config
from personal_os.core import runtime, spawn
from personal_os.core.logging import get_logger, setup_logging

EXIT_ALREADY_RUNNING = 3
EXIT_RESTART = 75  # the daemon asks to be started again (see daemon.request_restart)
_log = get_logger("launcher")


def main() -> int:
    config = load_config()
    ensure_dirs(config)
    setup_logging(config.logs_dir)
    lock = runtime.InstanceLock(config.runtime_dir / runtime.LAUNCHER_LOCK)
    try:
        lock.acquire()
    except runtime.AlreadyRunningError:
        return EXIT_ALREADY_RUNNING
    backoff = 1.0
    try:
        while True:
            started = time.monotonic()
            child = subprocess.run(
                spawn.module_command("personal_os.daemon"),
                env=spawn.child_env(),
                creationflags=spawn.creation_flags(detached=False),
                check=False,
            )
            code = child.returncode
            if code == EXIT_RESTART:  # asked for (settings changed): start again right away
                _log.info("daemon restarting on request")
                backoff = 1.0
                continue
            if code in (0, EXIT_ALREADY_RUNNING) or runtime.stop_requested(config.runtime_dir):
                runtime.clear_stop(config.runtime_dir)
                return 0
            if time.monotonic() - started > 600:
                backoff = 1.0  # it had been healthy for a while
            _log.warning(
                "daemon exited; restarting", extra={"fields": {"code": code, "in_s": backoff}}
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
