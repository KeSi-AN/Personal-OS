"""The capture daemon (DESIGN.md §3.1): observers → bus → privacy filter → SQLite.

Runs in the user's login session. `python -m personal_os.daemon` runs it in the foreground;
`personal-os daemon start` runs it in the background under the launcher, which restarts it
if it crashes.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import psutil

from personal_os.config.loader import ensure_dirs, load_config
from personal_os.config.schema import Config
from personal_os.core import pause as pause_mod
from personal_os.core import runtime
from personal_os.core.bus import EventBus
from personal_os.core.clock import Clock, SystemClock
from personal_os.core.events import Event, SystemEvent
from personal_os.core.logging import get_logger, setup_logging
from personal_os.core.observer import BaseObserver, ObserverContext, Supervisor
from personal_os.core.wiring import build_pipeline
from personal_os.observers.browser import BrowserObserver
from personal_os.observers.fs import FsObserver
from personal_os.observers.git import GitObserver
from personal_os.observers.session import SessionObserver, SessionState
from personal_os.observers.terminal import TerminalObserver
from personal_os.observers.window import WindowObserver
from personal_os.platform.base import SessionWatcher, WindowProbe
from personal_os.privacy.rules import PrivacyRules
from personal_os.projects.registry import ensure_repo_projects
from personal_os.projects.resolver import Resolver
from personal_os.projects.sessions import rebuild_sessions
from personal_os.storage import maintenance, repo
from personal_os.storage.db import connect, open_db

VERSION = "1.0.0"
EXIT_ALREADY_RUNNING = 3
HEALTH_EVERY_S = 10.0
RESOLVE_EVERY_S = 30.0
SESSIONS_EVERY_S = 300.0
RETENTION_EVERY_S = 6 * 3600.0
_log = get_logger("daemon")


class KvStateStore:
    """Observer state in kv_settings (namespaced). Used from the event loop thread only."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock) -> None:
        self.conn = conn
        self.clock = clock

    def get(self, key: str, default: Any = None) -> Any:
        return repo.get_setting(self.conn, f"observer.{key}", default)

    def set(self, key: str, value: Any) -> None:
        repo.set_setting(self.conn, f"observer.{key}", value, self.clock)


def _platform_probes() -> tuple[WindowProbe | None, SessionWatcher | None, str | None]:
    if sys.platform != "win32":
        return None, None, f"no window/session probes for {sys.platform} yet (DESIGN.md §4.4)"
    from personal_os.platform.windows import WindowsSessionWatcher, WindowsWindowProbe

    return WindowsWindowProbe(), WindowsSessionWatcher(), None


class Daemon:
    def __init__(
        self,
        config: Config,
        *,
        clock: Clock | None = None,
        window_probe: WindowProbe | None = None,
        session_watcher: SessionWatcher | None = None,
        use_platform: bool = True,
        control_every_s: float = 1.0,
        health_every_s: float = HEALTH_EVERY_S,
    ) -> None:
        self.config = config
        self.clock = clock or SystemClock()
        self.control_every_s = control_every_s
        self.health_every_s = health_every_s
        probe, watcher, reason = (
            (None, None, "platform probes disabled") if not use_platform else _platform_probes()
        )
        self.window_probe = window_probe or probe
        self.session_watcher = session_watcher or watcher
        self.platform_reason = None if (self.window_probe and self.session_watcher) else reason
        self.bus = EventBus()
        self.session = SessionState()
        self.paused = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.paused_at = self.clock.now()
        self.started_at = self.clock.now()
        self._proc = psutil.Process()
        self._proc.cpu_percent(None)
        self._lock = runtime.InstanceLock(config.runtime_dir / runtime.DAEMON_LOCK)

    # --- lifecycle -------------------------------------------------------------------------

    async def run(self) -> None:
        cfg = self.config
        ensure_dirs(cfg)
        self._lock.acquire()
        runtime.clear_stop(cfg.runtime_dir)
        self.writer_conn = open_db(cfg.db_path)
        self.control_conn = connect(cfg.db_path)
        self.jobs_conn = connect(cfg.db_path)  # projects/sessions jobs run in a worker thread
        recovered = maintenance.close_open_intervals(self.control_conn)
        self.pipeline = build_pipeline(
            cfg, self.writer_conn, self.clock, bus=self.bus, pause_gate=self._pause_gate
        )
        self.supervisor = self._build_supervisor()
        self._forget_rowid = maintenance.max_rowid(self.control_conn)
        if pause_mod.is_paused(self.control_conn, self.clock):
            self.paused_at = self.clock.now()
            self.paused.set()

        self._publish_system("system.daemon_start")
        pipeline_stop = asyncio.Event()
        pipeline_task = asyncio.create_task(
            self.pipeline.run(self.bus, pipeline_stop), name="pipeline"
        )
        self.supervisor.start()
        _log.info(
            "daemon started", extra={"fields": {"pid": os.getpid(), "recovered_open": recovered}}
        )
        try:
            await self._control_loop()
        finally:
            await self.supervisor.stop()
            self._publish_system("system.daemon_stop")
            pipeline_stop.set()
            await asyncio.wait_for(pipeline_task, 15)
            self._write_health(stopping=True)
            self.writer_conn.close()
            self.control_conn.close()
            self.jobs_conn.close()
            self._lock.release()
            _log.info("daemon stopped")

    def request_stop(self) -> None:
        self.stop_event.set()

    # --- wiring ----------------------------------------------------------------------------

    def _build_supervisor(self) -> Supervisor:
        cfg = self.config
        state_store = KvStateStore(self.control_conn, self.clock)

        def make_context(obs: BaseObserver) -> ObserverContext:
            return ObserverContext(
                bus=self.bus,
                clock=self.clock,
                config=None,
                log=get_logger(f"observers.{obs.name}"),
                paused=self.paused,
                state=state_store,
            )

        sup = Supervisor(make_context, self.clock)
        oc = cfg.observers
        window: WindowObserver | None = None
        if self.window_probe and self.session_watcher:
            sup.add(
                SessionObserver(self.session_watcher, self.session, on_hotkey=self.toggle_pause),
                enabled=oc.window.enabled,
            )
            window = WindowObserver(self.window_probe, self.session, oc.window, oc.idle)
            sup.add(
                window,
                enabled=oc.window.enabled,
                reason=None if oc.window.enabled else "disabled in config",
            )
        else:
            reason = self.platform_reason or "unavailable"
            sup.add(_Placeholder("window"), enabled=False, reason=reason)
            sup.add(_Placeholder("session"), enabled=False, reason=reason)
        git = GitObserver(cfg.watched_roots, oc.git)
        sup.add(
            git, enabled=oc.git.enabled, reason=None if oc.git.enabled else "disabled in config"
        )
        rules = PrivacyRules.from_config(cfg.privacy, protected_dirs=[cfg.data_dir])
        fs = FsObserver(
            cfg.watched_roots, oc.fs, rules, on_repo_activity=git.notify if oc.git.enabled else None
        )
        sup.add(
            fs,
            enabled=oc.fs.enabled,
            reason=None
            if oc.fs.enabled
            else "off by default; enable with `personal-os config set observers.fs.enabled true`",
        )
        sup.add(
            TerminalObserver(cfg.spool_dir / "terminal"),
            enabled=oc.terminal.enabled,
            reason=None if oc.terminal.enabled else "disabled in config",
        )
        sup.add(
            BrowserObserver(
                cfg.spool_dir / "browser",
                foreground=window.foreground_ms if window and oc.window.enabled else None,
            ),
            enabled=oc.browser.enabled,
            reason=None
            if oc.browser.enabled
            else "off until the extension is set up: `personal-os browser install`",
        )
        return sup

    # --- control loop ----------------------------------------------------------------------

    async def _control_loop(self) -> None:
        last_health = last_retention = last_stale = last_resolve = last_sessions = -1e18
        while not self.stop_event.is_set():
            if runtime.stop_requested(self.config.runtime_dir):
                runtime.clear_stop(self.config.runtime_dir)
                break
            await self._sync_pause()
            await self._sync_forget()
            mono = self.clock.monotonic()
            if mono - last_health >= self.health_every_s:
                last_health = mono
                self._write_health()
            if mono - last_resolve >= RESOLVE_EVERY_S and not self.paused.is_set():
                last_resolve = mono
                rebuild = mono - last_sessions >= SESSIONS_EVERY_S
                if rebuild:
                    last_sessions = mono
                await asyncio.to_thread(self._project_jobs, rebuild)
            if mono - last_stale >= 60:
                last_stale = mono
                maintenance.close_stale(
                    self.control_conn, "browser", self.clock.now() - timedelta(minutes=3)
                )
            if mono - last_retention >= RETENTION_EVERY_S:
                last_retention = mono
                await asyncio.to_thread(self._retention)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), self.control_every_s)

    def _pause_gate(self, event: Event) -> bool:
        """Filter-side pause check: drop what *began* during the pause. Intervals closed at the
        pause time (by on_pause) started earlier, so they are still stored."""
        return self.paused.is_set() and event.ts_start >= self.paused_at

    async def _sync_pause(self) -> None:
        present, _ = pause_mod.paused_until(self.control_conn)
        paused = pause_mod.is_paused(self.control_conn, self.clock)
        if present and not paused:  # timed pause expired
            self.bus.publish(
                pause_mod.resume(self.control_conn, self.clock, runtime_dir=self.config.runtime_dir)
            )
        if paused and not self.paused.is_set():
            # Order matters: observers close their intervals at `at` while emit still works,
            # then capture stops, the queue drains, and anything still open is closed in the DB.
            at = self.clock.now()
            self.supervisor.notify_pause(at)
            self.paused_at = at
            self.paused.set()
            await self._drain()
            maintenance.close_open_at(self.control_conn, at)
            _log.info("capture paused")
        elif not paused and self.paused.is_set():
            self.paused.clear()
            _log.info("capture resumed")

    async def _drain(self, timeout_s: float = 5.0) -> None:
        """Wait until everything queued so far has been written."""
        deadline = self.clock.monotonic() + timeout_s
        while (len(self.bus) or self.pipeline.busy) and self.clock.monotonic() < deadline:
            await asyncio.sleep(0.02)

    def toggle_pause(self) -> None:
        """Global hotkey (Ctrl+Alt+Shift+P)."""
        if pause_mod.is_paused(self.control_conn, self.clock):
            marker = pause_mod.resume(
                self.control_conn, self.clock, runtime_dir=self.config.runtime_dir
            )
        else:
            marker = pause_mod.pause(
                self.control_conn, self.clock, runtime_dir=self.config.runtime_dir
            )
        self.bus.publish(marker)
        # The control loop applies the change within a tick; beep for the new state now.
        if sys.platform == "win32":
            import winsound

            now_paused = marker.type == "pause"
            winsound.MessageBeep(winsound.MB_OK if now_paused else winsound.MB_ICONASTERISK)

    async def _sync_forget(self) -> None:
        for rowid, start, end in maintenance.forget_markers_since(
            self.control_conn, self._forget_rowid
        ):
            self._forget_rowid = rowid

            def in_window(e: Event, s: datetime = start, t: datetime = end) -> bool:
                return _overlaps(e, s, t)

            purged = self.bus.purge(in_window)
            self.supervisor.notify_forget(start, end)
            self.pipeline.add_forgotten(start, end)
            # A batch already past the guard may still be writing (e.g. a checkpoint written
            # between the CLI's delete and now): let it finish, then delete once more.
            await self._drain()
            maintenance.forget(self.control_conn, start, end, self.clock, marker=False)
            _log.info("forget applied", extra={"fields": {"queued_purged": purged}})

    def _project_jobs(self, rebuild_sessions_too: bool) -> None:
        """Assign projects to new events; every few minutes rebuild the last day's sessions.
        A failure here is logged and retried next time; it never stops capture."""
        try:
            ensure_repo_projects(self.jobs_conn, self.clock)
            Resolver(self.jobs_conn, self.clock).resolve_pending()
            if rebuild_sessions_too:
                now = self.clock.now()
                rebuild_sessions(self.jobs_conn, self.clock, now - timedelta(days=1), now)
        except Exception as exc:
            _log.warning("project jobs failed", extra={"fields": {"exc_type": type(exc).__name__}})

    def _retention(self) -> None:
        conn = connect(self.config.db_path)
        try:
            maintenance.purge_expired(conn, self.clock.now())
        finally:
            conn.close()

    # --- health ----------------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        ncpu = psutil.cpu_count() or 1
        try:
            cpu = self._proc.cpu_percent(None) / ncpu
            rss = self._proc.memory_info().rss
        except psutil.Error:
            cpu, rss = None, None
        observers = {
            name: h.model_dump(mode="json") for name, h in self.supervisor.health().items()
        }
        for obs in self.supervisor.observers:
            describe: Callable[[], dict[str, Any]] | None = getattr(obs, "describe", None)
            if describe is not None:
                observers[obs.name]["detail"] = describe()
        c = self.pipeline.privacy.counters
        return {
            "version": VERSION,
            "pid": os.getpid(),
            "process_started": self._proc.create_time(),
            "started_at": self.started_at.isoformat(),
            "heartbeat": self.clock.now().isoformat(),
            "paused": self.paused.is_set(),
            "cpu_percent": cpu,
            "rss_bytes": rss,
            "db_path": str(self.config.db_path),
            "observers": observers,
            "bus": {
                "queued": len(self.bus),
                "high_water": self.bus.stats.high_water,
                "dropped": dict(self.bus.stats.dropped),
            },
            "pipeline": {
                "received": self.pipeline.stats.received,
                "stored": self.pipeline.stats.stored,
                "forget_dropped": self.pipeline.forget_dropped,
            },
            "filter": {
                "blocked": dict(c.blocked),
                "errors": {str(k): v for k, v in c.errors.items()},
                "paused": c.paused,
            },
        }

    def _write_health(self, *, stopping: bool = False) -> None:
        data = self.health()
        data["stopping"] = stopping
        try:
            runtime.write_json_atomic(self.config.runtime_dir / runtime.HEALTH_FILE, data)
        except OSError:
            _log.warning("could not write health file")

    def _publish_system(self, kind: str) -> None:
        self.bus.publish(
            Event.point(
                SystemEvent(kind=kind),
                self.clock.now().astimezone(),
                observer_version=VERSION,
            )
        )


def _overlaps(e: Event, start: datetime, end: datetime) -> bool:
    last = e.ts_end or e.last_seen or e.ts_start
    return e.source != "meta" and e.ts_start < end and last >= start


class _Placeholder(BaseObserver):
    """Stands in for an observer this OS can't run, so doctor can list it with a reason."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name  # type: ignore[misc]

    async def run(self, ctx: ObserverContext) -> None:  # pragma: no cover - never started
        return None


def main() -> int:
    config = load_config()
    ensure_dirs(config)
    setup_logging(config.logs_dir)
    daemon = Daemon(config)
    try:
        asyncio.run(daemon.run())
    except runtime.AlreadyRunningError:
        print("Personal OS daemon is already running.", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
