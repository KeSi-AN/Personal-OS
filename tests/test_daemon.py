"""Daemon end to end with fake platform probes: capture → DB, pause, forget, health, stop."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from personal_os.config.loader import load_config, set_value
from personal_os.core import pause as pause_mod
from personal_os.core import runtime
from personal_os.core.clock import SystemClock
from personal_os.daemon import Daemon
from personal_os.platform.base import SessionSignal, WindowSample
from personal_os.storage import maintenance, repo
from personal_os.storage.db import connect, open_db


class FakeProbe:
    def __init__(self) -> None:
        self.current: WindowSample | None = WindowSample(hwnd=1, pid=1, app="Code", exe="Code.exe",
                                                         title="a.py - proj")  # fmt: skip
        self.idle = 0.0

    def sample(self) -> WindowSample | None:
        return self.current

    def idle_seconds(self) -> float:
        return self.idle


class FakeWatcher:
    def __init__(self) -> None:
        self.cb: Callable[[SessionSignal], None] | None = None

    def start(self, on_signal: Callable[[SessionSignal], None]) -> None:
        self.cb = on_signal

    def stop(self) -> None:
        self.cb = None


async def wait_for(cond: Callable[[], bool], timeout: float = 10.0) -> None:
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met in time")


@pytest.fixture
def cfg(home: Path):
    load_config(home)
    set_value("observers.window.poll_interval_s", 0.05, home)
    set_value("observers.window.min_focus_s", 0.0, home)
    set_value("observers.window.title_stable_s", 0.0, home)
    set_value("observers.git.enabled", False, home)
    return set_value("watched_roots", [], home)


def count(db: Path, where: str = "1=1") -> int:
    c = connect(db)
    try:
        return int(c.execute(f"SELECT count(*) FROM events WHERE {where}").fetchone()[0])
    finally:
        c.close()


async def test_daemon_lifecycle(cfg, home: Path):
    probe, watcher = FakeProbe(), FakeWatcher()
    daemon = Daemon(cfg, window_probe=probe, session_watcher=watcher, use_platform=False,
                    control_every_s=0.05, health_every_s=0.2)  # fmt: skip
    task = asyncio.create_task(daemon.run())
    db = cfg.db_path
    await wait_for(lambda: db.exists() and count(db, "source='window'") >= 1)

    # window switch + a spooled terminal command
    probe.current = WindowSample(hwnd=2, pid=2, app="Brave", exe="brave.exe", title="Docs - Brave")
    spool = cfg.spool_dir / "terminal"
    spool.mkdir(parents=True, exist_ok=True)
    now = SystemClock().now()
    (spool / "powershell-1.jsonl").write_text(json.dumps({
        "shell": "powershell", "command": "pytest -q", "start": now.isoformat(),
        "end": (now + timedelta(seconds=2)).isoformat(), "success": True, "exit_code": 0,
    }) + "\n", encoding="utf-8")  # fmt: skip
    await wait_for(
        lambda: count(db, "source='terminal'") == 1 and count(db, "source='window'") >= 2
    )

    # lock via the session watcher → idle(locked)
    assert watcher.cb is not None
    watcher.cb("lock")
    await wait_for(lambda: count(db, "source='system' AND type='lock'") == 1)
    await wait_for(lambda: count(db, "source='idle'") >= 1)
    watcher.cb("unlock")

    # pause from "another process" (the CLI): observers stop, open rows get closed
    ctl = connect(db)
    pause_mod.pause(ctl, SystemClock(), runtime_dir=cfg.runtime_dir)
    await wait_for(lambda: daemon.paused.is_set())
    # the daemon closes intervals at pause time once its queue has drained
    await wait_for(lambda: count(db, "ts_end IS NULL AND source != 'meta'") == 0)
    before = count(db, "source='window'")
    probe.current = WindowSample(hwnd=3, pid=3, app="Secret", title="while paused")
    await asyncio.sleep(0.5)
    assert count(db, "source='window'") == before
    pause_mod.resume(ctl, SystemClock(), runtime_dir=cfg.runtime_dir)
    await wait_for(lambda: not daemon.paused.is_set())

    # the hotkey toggles pause too
    watcher.cb("hotkey")
    await wait_for(lambda: daemon.paused.is_set())
    watcher.cb("hotkey")
    await wait_for(lambda: not daemon.paused.is_set())

    # forget the last minute: rows go, and nothing from inside the window comes back
    probe.current = WindowSample(hwnd=4, pid=4, app="Notes", title="forget me")
    await wait_for(lambda: count(db, "payload LIKE '%forget me%'") == 1)
    probe.current = WindowSample(hwnd=5, pid=5, app="Code", title="still here")
    await wait_for(lambda: count(db, "payload LIKE '%still here%'") == 1)
    clock = SystemClock()
    maintenance.forget(ctl, clock.now() - timedelta(minutes=1), clock.now(), clock)
    for _ in range(10):  # several ticks and checkpoints later
        await asyncio.sleep(0.1)
        assert count(db, "payload LIKE '%forget me%'") == 0
    # the window that is still focused is recorded again from after the forgotten window
    await wait_for(lambda: count(db, "payload LIKE '%still here%'") == 1)
    c = connect(db)
    start = c.execute("SELECT ts_start FROM events WHERE payload LIKE '%still here%'").fetchone()[0]
    marker = c.execute(
        "SELECT ts_start FROM events WHERE source='meta' AND type='forget'"
    ).fetchone()[0]
    c.close()
    assert start >= marker

    # health snapshot
    await asyncio.sleep(0.3)
    info = runtime.daemon_info(cfg.runtime_dir)
    assert info.running and info.health
    assert info.health["observers"]["window"]["state"] in ("running", "paused")
    assert info.health["observers"]["fs"]["state"] == "disabled"

    # stop via the stop-request file (what `personal-os daemon stop` does)
    runtime.request_stop(cfg.runtime_dir)
    await asyncio.wait_for(task, 20)
    assert count(db, "source='system' AND type='daemon_stop'") == 1
    assert count(db, "ts_end IS NULL AND source = 'window'") == 0  # closed on shutdown
    ctl.close()


async def test_second_daemon_refuses_to_start(cfg):
    a = Daemon(cfg, window_probe=FakeProbe(), session_watcher=FakeWatcher(), use_platform=False,
               control_every_s=0.05)  # fmt: skip
    task = asyncio.create_task(a.run())
    await wait_for(lambda: cfg.db_path.exists())
    b = Daemon(cfg, window_probe=FakeProbe(), session_watcher=FakeWatcher(), use_platform=False)
    with pytest.raises(runtime.AlreadyRunningError):
        await b.run()
    a.request_stop()
    await asyncio.wait_for(task, 20)


async def test_open_intervals_from_a_crash_are_closed_on_start(cfg):
    conn = open_db(cfg.db_path)
    from personal_os.core.events import Event, WindowFocus
    from personal_os.storage.writer import EventWriter

    t = SystemClock().now() - timedelta(hours=1)
    EventWriter(conn, cfg.retention, SystemClock()).write(
        [
            Event.interval(
                WindowFocus(app="Code"),
                t,
                None,
                last_seen=t + timedelta(minutes=5),
                observer_version="t",
            )
        ]
    )
    conn.close()
    d = Daemon(cfg, window_probe=FakeProbe(), session_watcher=FakeWatcher(), use_platform=False,
               control_every_s=0.05)  # fmt: skip
    task = asyncio.create_task(d.run())
    await wait_for(lambda: count(cfg.db_path, "truncated = 1") == 1)
    d.request_stop()
    await asyncio.wait_for(task, 20)
    c = connect(cfg.db_path)
    e = next(repo.iter_events(c, source="window"))
    assert e.truncated and e.ts_end is not None
    assert abs(e.ts_end - (t + timedelta(minutes=5))) < timedelta(milliseconds=1)  # stored in ms
    c.close()
