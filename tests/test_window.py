from __future__ import annotations

from datetime import timedelta

from personal_os.core.events import Event, Idle, WindowFocus
from personal_os.observers.window import FocusTracker, normalize_title
from personal_os.platform.base import WindowSample
from tests.conftest import T0

CODE = WindowSample(
    hwnd=1,
    pid=10,
    app="Visual Studio Code",
    exe="Code.exe",
    title="a.py - proj - Visual Studio Code",
)
BRAVE = WindowSample(hwnd=2, pid=20, app="Brave", exe="brave.exe", title="Docs - Brave")
EXPLORER = WindowSample(hwnd=3, pid=30, app="Explorer", exe="explorer.exe", title="Downloads")


def run(tracker: FocusTracker, timeline, *, start=T0, idle=None, locked=None):
    """timeline: list of samples, one per second. Returns all emitted events."""
    out: list[Event] = []
    for i, sample in enumerate(timeline):
        idle_s = idle(i) if idle else 0.0
        out += tracker.tick(
            start + timedelta(seconds=i), sample, idle_s, bool(locked and locked(i))
        )
    return out


def closed(events: list[Event], kind: str = "window.focus") -> list[Event]:
    """Final state of each interval (last emission per id), closed ones only."""
    last: dict = {}
    for e in events:
        if e.kind == kind:
            last[e.id] = e
    return [e for e in last.values() if e.ts_end is not None]


def test_normalize_title():
    assert normalize_title("(3) Inbox - Gmail") == "Inbox - Gmail"
    assert normalize_title("● main.py - proj") == "main.py - proj"
    assert normalize_title("  a   b ") == "a b"


def test_switch_after_min_focus_and_back_dated_start():
    t = FocusTracker(min_focus_s=2, title_stable_s=3)
    events = run(t, [CODE] * 5 + [BRAVE] * 5)
    events += t.close_all(T0 + timedelta(seconds=10))
    focus = closed(events)
    assert [e.payload.app for e in focus] == ["Visual Studio Code", "Brave"]  # type: ignore[union-attr]
    # Brave was first seen at t=5 and confirmed at t=7; the switch is dated at first sight.
    assert focus[0].ts_end == focus[1].ts_start == T0 + timedelta(seconds=5)


def test_alt_tab_flicker_is_merged_and_counted():
    t = FocusTracker(min_focus_s=2)
    events = run(t, [CODE] * 3 + [EXPLORER] + [CODE] * 3 + [BRAVE] + [CODE] * 2)
    events += t.close_all(T0 + timedelta(seconds=10))
    focus = closed(events)
    assert len(focus) == 1
    assert focus[0].payload.transient_switches == 2  # type: ignore[union-attr]


def test_title_change_needs_to_be_stable():
    t = FocusTracker(min_focus_s=2, title_stable_s=3)
    other_file = WindowSample(**{**CODE.__dict__, "title": "b.py - proj - Visual Studio Code"})
    events = run(t, [CODE] * 3 + [other_file] * 2 + [CODE] * 3 + [other_file] * 4)
    events += t.close_all(T0 + timedelta(seconds=12))
    titles = [e.payload.title for e in closed(events)]  # type: ignore[union-attr]
    assert titles == ["a.py - proj - Visual Studio Code", "b.py - proj - Visual Studio Code"]


def test_unread_counter_changes_do_not_split():
    t = FocusTracker()
    a = WindowSample(hwnd=5, pid=50, app="Brave", title="(1) Inbox - Mail")
    b = WindowSample(hwnd=5, pid=50, app="Brave", title="(2) Inbox - Mail")
    events = run(t, [a, b] * 5) + t.close_all(T0 + timedelta(seconds=10))
    assert len(closed(events)) == 1


def test_idle_closes_focus_back_dated_to_last_input_and_resumes():
    t = FocusTracker(idle_threshold_s=300)
    # Active 0..9 s, then no input for 400 s (ticks every second report growing idle time),
    # then input again.
    events: list[Event] = []
    for i in range(10):
        events += t.tick(T0 + timedelta(seconds=i), CODE, 0)
    for k in range(1, 401):
        events += t.tick(T0 + timedelta(seconds=9 + k), CODE, float(k))
    resume_at = T0 + timedelta(seconds=410)
    events += t.tick(resume_at, CODE, 0.0)
    events += t.close_all(resume_at + timedelta(seconds=5))
    focus = closed(events)
    idle = closed(events, "idle.idle")
    assert focus[0].ts_end == T0 + timedelta(seconds=9)  # last input, not when idle was noticed
    assert idle[0].ts_start == T0 + timedelta(seconds=9) and idle[0].ts_end == resume_at
    assert isinstance(idle[0].payload, Idle) and idle[0].payload.reason == "no_input"
    assert focus[1].ts_start == resume_at


def test_lock_is_away_immediately_and_lockapp_counts_as_locked():
    t = FocusTracker()
    events = run(t, [CODE] * 5 + [CODE] * 5, locked=lambda i: i >= 5)
    idle = [e for e in events if e.kind == "idle.idle"]
    assert idle and isinstance(idle[-1].payload, Idle) and idle[-1].payload.reason == "locked"
    t2 = FocusTracker()
    lockapp = WindowSample(hwnd=9, pid=90, app="LockApp", exe="LockApp.exe")
    events2 = run(t2, [CODE] * 3 + [lockapp] * 3)
    assert any(e.kind == "idle.idle" for e in events2)


def test_sleep_gap_closes_at_last_tick_and_records_asleep():
    t = FocusTracker(sleep_gap_s=30)
    events = t.tick(T0, CODE, 0) + t.tick(T0 + timedelta(seconds=1), CODE, 0)
    wake = T0 + timedelta(hours=2)
    events += t.tick(wake, CODE, 0)
    focus = closed(events)
    assert focus[0].ts_end == T0 + timedelta(seconds=1)
    asleep = [e for e in events if e.kind == "idle.idle"]
    assert asleep[0].payload.reason == "asleep"  # type: ignore[union-attr]
    assert asleep[0].ts_start == T0 + timedelta(seconds=1) and asleep[0].ts_end == wake


def test_checkpoints_keep_interval_open_with_rising_last_seen():
    t = FocusTracker(checkpoint_s=60)
    events = run(t, [CODE] * 185)
    focus = [e for e in events if e.kind == "window.focus"]
    assert len({e.id for e in focus}) == 1
    assert all(e.ts_end is None for e in focus)
    assert len(focus) == 4  # open + checkpoints at 60, 120, 180 s
    seen = [e.last_seen for e in focus]
    assert seen == sorted(seen)


def test_reset_drops_open_interval_without_emitting():
    t = FocusTracker()
    run(t, [CODE] * 3)
    t.reset()
    assert t.close_all(T0 + timedelta(seconds=5)) == []
    events = t.tick(T0 + timedelta(seconds=6), CODE, 0)
    assert events and events[0].ts_start == T0 + timedelta(seconds=6)


def test_none_sample_keeps_current_focus():
    t = FocusTracker()
    events = run(t, [CODE, None, None, CODE]) + t.close_all(T0 + timedelta(seconds=4))
    assert len(closed(events)) == 1


def test_payload_carries_no_window_content():
    t = FocusTracker()
    events = run(t, [CODE])
    p = events[0].payload
    assert isinstance(p, WindowFocus)
    assert set(p.model_dump()) <= {
        "kind", "app", "exe", "exe_path", "pid", "title", "window_class", "cwd", "is_uwp",
        "elevated_unreadable", "transient_switches",
    }  # fmt: skip
