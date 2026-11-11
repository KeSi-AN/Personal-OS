"""The capture daemon (DESIGN.md §3.1): observers → bus → privacy filter → SQLite.

Runs in the user's login session. `python -m personal_os.daemon` runs it in the foreground;
`personal-os daemon start` runs it in the background under the launcher, which restarts it
if it crashes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import sys
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import psutil

from personal_os.actions import killswitch
from personal_os.config.loader import ensure_dirs, load_config
from personal_os.config.schema import Config
from personal_os.context.gate import JobGate
from personal_os.context.jobs import JobReport, run_llm_jobs
from personal_os.core import modes, runtime
from personal_os.core import pause as pause_mod
from personal_os.core.bus import EventBus
from personal_os.core.clock import Clock, SystemClock
from personal_os.core.events import Event, SystemEvent
from personal_os.core.logging import get_logger, setup_logging
from personal_os.core.observer import BaseObserver, ObserverContext, Supervisor
from personal_os.core.wiring import build_pipeline
from personal_os.interfaces.tray import Tray, TrayActions, spawn_module, spawn_overlay
from personal_os.llm import LLMProvider, LLMUnavailable, make_provider
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
EXIT_RESTART = 75  # the launcher starts the daemon again at once
HEALTH_EVERY_S = 10.0
RESOLVE_EVERY_S = 30.0
SESSIONS_EVERY_S = 300.0
RETENTION_EVERY_S = 6 * 3600.0
LLM_EVERY_S = 60.0
LLM_BACKOFF_S = 600.0  # Ollama not running: look again in 10 minutes
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
        llm_provider: LLMProvider | None = None,
        llm_every_s: float = LLM_EVERY_S,
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
        self.llm_every_s = llm_every_s
        self.llm = llm_provider or (make_provider(config.llm) if config.llm.enabled else None)
        probe_ = self.window_probe
        self.gate = JobGate(
            config.llm,
            idle_seconds=probe_.idle_seconds if probe_ else None,
            fullscreen=getattr(probe_, "fullscreen", None),
            paused=self.paused.is_set,
            mode_allows=lambda: modes.effects(config.runtime_dir).background_jobs,
        )
        self.llm_status: dict[str, Any] = {"gate": "not checked yet"}
        self.last_nudge: datetime | None = None
        self.restart_requested = False
        self.interfaces = use_platform  # web dashboard, tray (never in tests without a desktop)
        self.tray: Tray | None = None
        self._voice_proc: Any = None
        self._voice_retry_at = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- lifecycle -------------------------------------------------------------------------

    async def run(self) -> None:
        cfg = self.config
        ensure_dirs(cfg)
        self._lock.acquire()
        runtime.clear_stop(cfg.runtime_dir)
        self.writer_conn = open_db(cfg.db_path)
        self.control_conn = connect(cfg.db_path)
        self.jobs_conn = connect(cfg.db_path)  # projects/sessions jobs run in a worker thread
        self.llm_conn = connect(cfg.db_path)  # LLM jobs: their own task and thread
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
        llm_task = asyncio.create_task(self._llm_loop(), name="llm-jobs")
        self._loop = asyncio.get_running_loop()
        servers = self._start_interfaces() if self.interfaces else []
        try:
            await self._control_loop()
        finally:
            self.stop_event.set()
            for server, _task in servers:
                server.should_exit = True
            for _server, task in servers:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(task, 10)
            if self.tray is not None:
                self.tray.stop()
            if self._voice_proc is not None and self._voice_proc.poll() is None:
                self._voice_proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(llm_task, 60)
            await self.supervisor.stop()
            self._publish_system("system.daemon_stop")
            pipeline_stop.set()
            await asyncio.wait_for(pipeline_task, 15)
            self._write_health(stopping=True)
            self.writer_conn.close()
            self.control_conn.close()
            self.jobs_conn.close()
            self.llm_conn.close()
            self._lock.release()
            _log.info("daemon stopped")

    def request_stop(self) -> None:
        self.stop_event.set()

    def request_restart(self) -> None:
        """Stop now and have the launcher start again (settings that apply at start changed)."""
        self.restart_requested = True
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
                SessionObserver(
                    self.session_watcher,
                    self.session,
                    on_hotkey=self.toggle_pause,
                    on_kill=self.engage_kill_switch,
                    on_overlay=self.open_overlay,
                ),
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
                now_dir=cfg.runtime_dir / "now",
            ),
            enabled=oc.browser.enabled,
            reason=None
            if oc.browser.enabled
            else "off until the extension is set up: `personal-os browser install`",
        )
        return sup

    # --- control loop ----------------------------------------------------------------------

    def _startup_brief(self) -> None:
        """A moment after capture starts: judge new videos, keep unclaimed research as R&D
        projects, and open the 24-hour brief (at most every [brief] min_gap_h hours)."""
        import time as _time

        from personal_os.interfaces import alerts
        from personal_os.web import brief

        _time.sleep(45)  # let the observers and the browser connection settle
        cfg = self._fresh_config()
        llm = self.llm if modes.effects(cfg.runtime_dir).background_jobs else None
        prepared = brief.prepare(cfg, llm, self.clock)
        made = [k for k in prepared.get("kept", []) if k.get("created")]
        if made:
            text = "; ".join(f"{k['project']} ({k['minutes']} min)" for k in made)
            alerts.show(
                cfg,
                f"rnd-{self.clock.now():%Y%m%d%H%M}",
                "Kept your research",
                text,
                self.clock.now(),
            )
        if not brief.due(cfg, self.clock) or not self.interfaces:
            return
        from personal_os.interfaces import app

        how = app.open_app(cfg, "/brief")
        brief.mark_opened(cfg, self.clock)
        _log.info(
            "brief opened", extra={"fields": {"kept": len(prepared.get("kept", [])), "in": how}}
        )

    async def _control_loop(self) -> None:
        last_health = last_retention = last_stale = last_resolve = last_sessions = -1e18
        last_reminders = -1e18
        if self.interfaces:
            threading.Thread(target=self._startup_brief, name="pos-brief", daemon=True).start()
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
            if mono - last_reminders >= 30:
                last_reminders = mono
                self._fire_reminders()
                self._sync_project_sources()
                self._mode_tick()
                if self.interfaces:
                    self._sync_voice()
                    await asyncio.to_thread(self._briefings)
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

    # --- interfaces (phase 6): dashboard, tray, overlay, reminders -----------------------------

    def _threadsafe(self, fn: Callable[[], None]) -> Callable[[], None]:
        def call() -> None:
            if self._loop is not None:
                self._loop.call_soon_threadsafe(fn)

        return call

    def _start_interfaces(self) -> list[tuple[Any, asyncio.Task[None]]]:
        from personal_os.web.app import WebState
        from personal_os.web.server import lan_addresses, lan_server, local_server

        desktop = None
        if sys.platform == "win32":
            from personal_os.actions.desktop import WindowsDesktop

            desktop = WindowsDesktop
        self.web_state = WebState(
            self.config, self.clock, provider=lambda: self.llm, desktop=desktop,
            restart=self._threadsafe(self.request_restart), lan_addresses=lan_addresses,
        )  # fmt: skip
        servers: list[tuple[Any, asyncio.Task[None]]] = []
        for make in (local_server, lan_server):
            try:
                server = make(self.web_state)
            except Exception as exc:
                _log.warning(
                    "web listener not started", extra={"fields": {"exc_type": type(exc).__name__}}
                )
                continue
            if server is not None:
                task = asyncio.create_task(self._serve(server), name=f"web-{make.__name__}")
                servers.append((server, task))
        if sys.platform == "win32":
            try:
                self.tray = Tray(TrayActions(
                    state=self._tray_state,
                    toggle_pause=self._threadsafe(self.toggle_pause),
                    open_dashboard=self.open_dashboard,
                    open_overlay=self.open_overlay,
                    toggle_kill=self._toggle_kill,
                    restart=self._threadsafe(self.request_restart),
                    stop=self._threadsafe(self._stop_from_tray),
                    voice_state=self._voice_state,
                    toggle_voice=lambda: self._toggle_setting("voice.enabled"),
                    toggle_wake=lambda: self._toggle_setting("voice.wake_word_enabled"),
                ))  # fmt: skip
                self.tray.start()
            except Exception as exc:
                _log.warning("tray not started", extra={"fields": {"exc_type": type(exc).__name__}})
                self.tray = None
        return servers

    async def _serve(self, server: Any) -> None:
        try:
            await server.serve()
        except (OSError, SystemExit) as exc:  # e.g. the port is taken
            _log.warning("web listener stopped", extra={"fields": {"exc_type": type(exc).__name__}})

    def _tray_state(self) -> str:
        if killswitch.engaged(self.config.runtime_dir):
            return "killed"
        if self._voice_listening():
            return "listening"
        return "paused" if self.paused.is_set() else "capturing"

    def _voice_listening(self) -> bool:
        """The voice process has the mic open for a request (not just waiting for the key)."""
        try:
            data = json.loads((self.config.runtime_dir / "voice.json").read_text("utf-8"))
        except (OSError, ValueError):
            return False
        return bool(isinstance(data, dict) and data.get("state") == "listening")

    def _toggle_kill(self) -> None:
        if killswitch.engaged(self.config.runtime_dir):
            killswitch.release(self.config.runtime_dir)
        else:
            killswitch.engage(self.config.runtime_dir, self.clock.now(), "tray")

    def _stop_from_tray(self) -> None:
        runtime.request_stop(self.config.runtime_dir)  # the launcher won't restart it
        self.stop_event.set()

    def open_dashboard(self) -> None:
        """The tray's "Open dashboard": the desktop app (ADR-079)."""
        from personal_os.interfaces import app

        app.open_app(self.config, "/")

    def open_overlay(self) -> None:
        try:
            spawn_overlay()
        except OSError:
            _log.warning("overlay didn't start")

    # --- voice and briefings (phase 7) --------------------------------------------------------

    def _fresh_config(self) -> Config:
        try:
            return load_config()
        except Exception:
            return self.config

    def _voice_state(self) -> tuple[bool, bool]:
        cfg = self._fresh_config()
        return cfg.voice.enabled, cfg.voice.wake_word_enabled

    def _toggle_setting(self, key: str) -> None:
        from personal_os.config.loader import set_value

        section, name = key.split(".")
        current = getattr(getattr(self._fresh_config(), section), name)
        set_value(key, not current)

    def _sync_voice(self) -> None:
        """Keep the voice process running while voice is on (it exits by itself when off)."""
        if sys.platform != "win32" or not self._fresh_config().voice.enabled:
            return
        proc = self._voice_proc
        if proc is not None and proc.poll() is None:
            return
        if self.clock.monotonic() < self._voice_retry_at:
            return
        try:
            self._voice_proc = spawn_module("personal_os.voice.main")
            self._voice_retry_at = self.clock.monotonic() + 60  # at most one restart a minute
        except OSError:
            _log.warning("voice didn't start")

    def _briefings(self) -> None:
        """Once a day each: a morning briefing and an end-of-day summary, while you're here."""
        from personal_os.context import briefing

        cfg = self._fresh_config()
        if not cfg.briefings.enabled or self.window_probe is None:
            return
        if self.window_probe.idle_seconds() > 120:
            return  # you're away: wait until you're back
        conn = connect(self.config.db_path)
        try:
            kind = briefing.due_kind(
                conn, self.clock, cfg.briefings.morning_after, cfg.briefings.evening_after
            )
            if kind is None:
                return
            from personal_os.voice.main import google_client

            b = briefing.build(conn, self.clock, kind, google=google_client(cfg))
            briefing.mark_delivered(conn, self.clock, kind)
            last = {
                "kind": kind,
                "title": b.title,
                "lines": b.lines,
                "at": self.clock.now().isoformat(),
            }
            repo.set_setting(conn, "briefings.last", last, self.clock)
        finally:
            conn.close()
        _log.info("briefing delivered", extra={"fields": {"kind": kind}})
        from personal_os.interfaces import alerts
        from personal_os.interfaces.notify import toast

        # The popup gets only the title (its rule: nothing from captured activity); the toast,
        # in the notification centre, and the dashboard card have the lines.
        alerts.show(
            cfg, f"briefing-{kind}-{self.clock.now():%Y%m%d}", b.title,
            "Ready on the dashboard (Today).", self.clock.now(),
        )  # fmt: skip
        if not toast(b.title, " ".join(b.lines)) and self.tray is not None:
            self.tray.notify(" ".join(b.lines)[:240], b.title)
        if cfg.voice.enabled and cfg.briefings.speak:
            self._speak(cfg, f"{b.title}. " + " ".join(b.lines))

    def _mode_tick(self) -> None:
        """Every 30 s: gaming mode when a game is in front (and back after), and in work mode a
        nudge when chill has taken over."""
        from personal_os.context import activity
        from personal_os.context import now as now_mod

        rt = self.config.runtime_dir
        now = self.clock.now()
        try:
            snap = now_mod.snapshot(self.config)
            front = next((w for w in snap.windows if w.front), None)
            game = (
                front is not None and activity.classify_app(front.exe, front.exe_path) == "gaming"
            )
            if self.config.modes.auto_gaming:
                change = modes.auto_gaming_step(rt, game, now)
                if change == "entered" and self.llm is not None:
                    with contextlib.suppress(Exception):
                        self.llm.unload()  # free the GPU for the game
                if change:
                    _log.info("mode changed", extra={"fields": {"auto": change}})
            fx = modes.effects(rt)
            cache = activity.load_cache(self.config.data_dir)
            if (
                fx.nudge_chill_min
                and self._nudge_due(rt, now)
                and activity.in_front(snap, cache) == "chill"
            ):
                # Only while the chill is still in front: back at work already means no nudge.
                b = activity.breakdown(self.control_conn, now - timedelta(minutes=20), now, cache)
                chill = b.by_category.get("chill", 0)
                if chill >= fx.nudge_chill_min * 60_000:
                    self.last_nudge = now
                    self._nudge(chill // 60_000, b)
        except Exception as exc:  # never let this take the loop down
            _log.warning("mode tick failed", extra={"fields": {"exc_type": type(exc).__name__}})

    def _nudge_due(self, rt: Any, now: datetime) -> bool:
        """Not twice within 30 minutes, and not in the first 10 minutes of work mode."""
        if self.last_nudge is not None and now - self.last_nudge <= timedelta(minutes=30):
            return False
        since = str(modes.state(rt).get("since") or "")
        try:
            return not since or now - datetime.fromisoformat(since) >= timedelta(minutes=10)
        except ValueError:
            return True

    def _nudge(self, minutes: int, b: Any) -> None:
        from personal_os.interfaces import alerts
        from personal_os.projects.registry import get_project

        top = max(b.by_site.items(), key=lambda kv: kv[1])[0] if b.by_site else "videos"
        row = self.control_conn.execute(
            "SELECT project_id FROM sessions WHERE project_id IS NOT NULL AND project_id != 'none' "
            "ORDER BY ts_end DESC LIMIT 1"
        ).fetchone()
        project = get_project(self.control_conn, row[0]) if row else None
        back = f" Back to {project.name}?" if project else ""
        text = (f"{minutes} min of chill ({top}) in work mode.{back} "
                "If it's a break, say 'chill mode'.")  # fmt: skip
        alerts.show(
            self.config, f"nudge-{self.clock.now():%H%M}", "Work mode", text, self.clock.now()
        )
        self._speak(self._fresh_config(), text)

    def _sync_project_sources(self) -> None:
        """Pages added from the browser's "Add to Personal OS project" go to the project inbox;
        tab groups named like a project keep its sources in step (phase 9k). Each item is its own
        write: filing a link also teaches the tracker, which opens a transaction of its own."""
        from personal_os.projects import items
        from personal_os.projects.registry import get_project

        rt = self.config.runtime_dir
        inbox = rt / "browser" / "inbox"
        try:
            items.publish_projects(self.control_conn, self.clock, rt)  # for the right-click menu
            for f in sorted(inbox.glob("*.json")) if inbox.is_dir() else []:
                rec = runtime.read_json(f) or {}
                f.unlink(missing_ok=True)
                url = str(rec.get("url", ""))
                picked = get_project(self.control_conn, str(rec.get("project") or ""))
                if url.startswith(("http://", "https://")):
                    items.add(self.control_conn, self.clock, picked.id if picked else None,
                              "link", url=url, title=str(rec.get("title", "")),
                              source="extension")  # fmt: skip
            for f in (rt / "now").glob("tabs-*.json") if (rt / "now").is_dir() else []:
                tabs = (runtime.read_json(f) or {}).get("tabs") or []
                items.sync_groups(self.control_conn, self.clock, list(tabs))
        except Exception as exc:  # never let this take the loop down
            _log.warning(
                "project sources not synced", extra={"fields": {"exc_type": type(exc).__name__}}
            )

    def _fire_reminders(self) -> None:
        from personal_os.actions import reminders

        due = reminders.take_due(self.control_conn, self.clock)
        if due:
            threading.Thread(target=self._announce_reminders, args=(due,), daemon=True).start()

    def _announce_reminders(self, due: list[Any]) -> None:
        """Each in the popup, which stays on top until dismissed (Windows hides toast banners
        during full-screen apps and Do Not Disturb). A Windows notification if the popup can't
        open, the tray balloon if that fails too. Spoken if voice is on."""
        from personal_os.interfaces import alerts
        from personal_os.interfaces.notify import toast

        cfg = self._fresh_config()
        quiet = not modes.effects(cfg.runtime_dir).popup  # gaming: never a window over the game
        for r in due:
            shown_by = "popup"
            if quiet or not alerts.show(cfg, r.id, "Reminder", r.text, self.clock.now()):
                shown_by = "toast" if toast("Reminder", r.text, reminder=True) else "none"
                if shown_by == "none" and self.tray is not None:
                    self.tray.notify(r.text, "Reminder")
                    shown_by = "tray"
            _log.info("reminder fired", extra={"fields": {"shown": shown_by}})
            if cfg.voice.enabled and modes.effects(cfg.runtime_dir).speak_announcements:
                self._speak(cfg, f"Reminder: {r.text}")

    def _speak(self, cfg: Any, text: str) -> None:
        """Through the voice process when it runs (one voice, never over a conversation, no
        speech model in the daemon); spoken here only when it doesn't."""
        from personal_os.voice.main import queue_speech
        from personal_os.voice.tts import Speaker, TTSError

        if queue_speech(cfg.runtime_dir, text):
            return
        with contextlib.suppress(TTSError):
            Speaker(cfg.voice, cfg.data_dir).say(text)

    def engage_kill_switch(self) -> None:
        """Global hotkey (Ctrl+Alt+Shift+K): actions go observe-only and running ones stop."""
        killswitch.engage(self.config.runtime_dir, self.clock.now(), "hotkey")
        _log.info("kill switch engaged", extra={"fields": {"by": "hotkey"}})
        if sys.platform == "win32":
            import winsound

            winsound.MessageBeep(winsound.MB_ICONHAND)

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
            maintenance.forget(
                self.control_conn, start, end, self.clock, marker=False,
                undo_dir=self.config.data_dir / "undo",
            )  # fmt: skip
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

    async def _llm_loop(self) -> None:
        """Every minute: if the gate is open, summarise and embed in a worker thread. Runs beside
        the control loop, so a long job never delays pause, forget or health."""
        if self.llm is None:
            self.llm_status = {"gate": "llm disabled in config"}
            return
        wait = 5.0
        while not self.stop_event.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.stop_event.wait(), wait)
            if self.stop_event.is_set():
                break
            wait = self.llm_every_s
            gate = self.gate.check()
            self.llm_status["gate"] = gate.reason
            if not gate.ok:
                continue
            try:
                report = await asyncio.to_thread(self._llm_jobs)
            except LLMUnavailable:
                self.llm_status["error"] = "ollama not reachable"
                wait = LLM_BACKOFF_S
                continue
            except Exception as exc:  # never let a model problem stop capture
                _log.warning("llm jobs failed", extra={"fields": {"exc_type": type(exc).__name__}})
                self.llm_status["error"] = type(exc).__name__
                continue
            self.llm_status.pop("error", None)
            if report.summarized or report.failed or report.embedded:
                self.llm_status["last_run"] = self.clock.now().isoformat()
                _log.info(
                    "llm jobs",
                    extra={
                        "fields": {
                            "summarized": report.summarized,
                            "failed": report.failed,
                            "embedded": report.embedded,
                            "stopped_early": report.stopped_early,
                        }
                    },
                )

    def _llm_jobs(self) -> JobReport:
        assert self.llm is not None

        def should_stop() -> bool:
            return self.stop_event.is_set() or not self.gate.check().ok

        return run_llm_jobs(self.llm_conn, self.llm, self.clock, self.config.llm, should_stop)

    def _retention(self) -> None:
        conn = connect(self.config.db_path)
        try:
            maintenance.purge_expired(conn, self.clock.now())
            keep = timedelta(days=self.config.actions.undo_keep_days)
            maintenance.purge_undo(self.config.data_dir / "undo", self.clock.now() - keep)
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
            "llm": dict(self.llm_status),
            "hotkeys": {
                name: getattr(self.session_watcher, attr, None)
                for name, attr in (
                    ("pause Ctrl+Alt+Shift+P", "hotkey_registered"),
                    ("kill Ctrl+Alt+Shift+K", "kill_hotkey_registered"),
                    ("overlay Ctrl+Alt+Shift+Space", "overlay_hotkey_registered"),
                )
            },
            "tray": self.tray is not None,
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
        if daemon.restart_requested:
            return EXIT_RESTART
    except runtime.AlreadyRunningError:
        print("Personal OS daemon is already running.", file=sys.stderr)
        return EXIT_ALREADY_RUNNING
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
