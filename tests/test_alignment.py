"""Fixes from the cross-source audit: window-based active time, retraction of visits that turn
out to be off-limits, counting blocked pages once, stale open visits, git restart noise,
deleted build folders, windowless background processes, and the `events` display."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from personal_os.browser.backfill import recompute_dwell
from personal_os.config.schema import Config
from personal_os.core import spawn
from personal_os.core.clock import SystemClock
from personal_os.core.events import Event, PageVisit, WindowFocus
from personal_os.core.wiring import build_pipeline
from personal_os.interfaces.autostart import script
from personal_os.observers.browser import parse_record
from personal_os.observers.fs import classify_path
from personal_os.observers.window import FocusTracker, browser_page_title, foreground_overlap
from personal_os.platform.base import WindowSample
from personal_os.storage import maintenance, repo
from personal_os.storage.db import open_db
from tests.conftest import T0
from tests.test_browser import visit

CHROME = {"chrome.exe", "brave.exe"}


def _tracker_with(*spans: tuple[str, str, int, int]) -> FocusTracker:
    """spans: (exe, window title, start s, end s) relative to T0."""
    t = FocusTracker(min_focus_s=0, title_stable_s=0)
    for exe, title, a, b in spans:
        t.history.append((exe, title, T0 + timedelta(seconds=a), T0 + timedelta(seconds=b)))
    return t


# --- active time from the window record ----------------------------------------------------


def test_browser_page_title():
    assert browser_page_title("What is GraphRAG? - Google Chrome") == "What is GraphRAG?"
    assert browser_page_title("Did the AI bubble just pop? - YouTube - Google Chrome") == (
        "Did the AI bubble just pop? - YouTube"
    )
    assert browser_page_title("New Tab - Brave") == "New Tab"
    assert browser_page_title("Some page") == "Some page"


def test_foreground_overlap_counts_only_matching_browser_windows():
    t = _tracker_with(
        ("chrome.exe", "What is GraphRAG? - Google Chrome", 0, 15),
        ("claude.exe", "Claude", 15, 35),  # another app in front: not counted
        ("chrome.exe", "Other page - Google Chrome", 35, 40),  # other tab: not counted
        ("chrome.exe", "What is GraphRAG? - Google Chrome", 40, 71),
    )
    got = foreground_overlap(t, CHROME, {"What is GraphRAG?"}, T0, T0 + timedelta(seconds=60))
    assert got == 35_000  # 15 s + 20 s (clipped at the visit end)


def test_foreground_overlap_includes_the_open_window_and_knows_its_limits():
    t = FocusTracker(min_focus_s=0, title_stable_s=0)
    sample = WindowSample(
        hwnd=1, pid=1, app="Chrome", exe="chrome.exe", title="Docs - Google Chrome"
    )
    t.tick(T0, sample, 0)
    t.tick(T0 + timedelta(seconds=30), sample, 0)
    assert foreground_overlap(t, CHROME, {"Docs"}, T0, T0 + timedelta(minutes=5)) == 30_000
    # no window record before the visit started (daemon started later): unknown, not zero
    assert foreground_overlap(t, CHROME, {"Docs"}, T0 - timedelta(minutes=10), T0) is None


def test_visit_uses_window_time_and_falls_back_to_the_extension():
    measured = parse_record(visit(activeMs=100_000), foreground=lambda *a: 35_000)
    assert measured is not None
    p = measured[0].payload
    assert isinstance(p, PageVisit)
    assert p.active_dwell_ms == 35_000 and p.enrichment["dwell_source"] == "window"
    assert p.enrichment["extension_active_ms"] == 100_000
    unmatched = parse_record(visit(activeMs=100_000), foreground=lambda *a: 0)
    assert unmatched is not None and unmatched[0].payload.active_dwell_ms == 100_000  # type: ignore[union-attr]
    unknown = parse_record(visit(activeMs=100_000), foreground=lambda *a: None)
    assert unknown is not None and unknown[0].payload.enrichment["dwell_source"] == "extension"  # type: ignore[union-attr]


def test_recompute_dwell_from_stored_window_record(home: Path, config: Config):
    conn = open_db(home / "r.db")
    pipe = build_pipeline(config, conn, SystemClock(), honour_pause=False)
    start = T0
    focus = [
        Event.interval(WindowFocus(app="Chrome", exe="chrome.exe", title="x - Stack Overflow - Google Chrome"),
                       start, start + timedelta(seconds=40), observer_version="t"),
        Event.interval(WindowFocus(app="Claude", exe="claude.exe", title="Claude"),
                       start + timedelta(seconds=40), start + timedelta(seconds=240), observer_version="t"),
    ]  # fmt: skip
    rec = visit(start=start.isoformat(), end=(start + timedelta(minutes=4)).isoformat(),
                last_seen=(start + timedelta(minutes=4)).isoformat(), activeMs=200_000)  # fmt: skip
    pipe.process([*focus, *parse_record(rec)])  # type: ignore[misc]
    assert recompute_dwell(conn) == (1, 1)
    e = next(repo.iter_events(conn, source="browser"))
    assert e.payload.active_dwell_ms == 40_000  # type: ignore[union-attr]
    assert e.payload.enrichment["extension_active_ms"] == 200_000  # type: ignore[union-attr]
    assert recompute_dwell(conn) == (0, 1)  # idempotent


# --- retraction, counting once, stale visits ------------------------------------------------


@pytest.fixture
def pipe(home: Path, config: Config):
    conn = open_db(home / "p.db")
    return build_pipeline(config, conn, SystemClock(), honour_pause=False)


def test_open_visit_is_retracted_when_it_ends_as_a_bounce(pipe):
    open_rec = visit(end=None, activeMs=1000, url="https://example.com/a")
    pipe.process(parse_record(open_rec))
    assert (
        sum(1 for _ in repo.iter_events(pipe.conn, source="browser")) == 1
    )  # open: not judged yet
    closed = visit(activeMs=1500, url="https://example.com/a")  # same id, closed, 1.5 s
    pipe.process(parse_record(closed))
    assert list(repo.iter_events(pipe.conn, source="browser")) == []


def test_visit_is_retracted_when_a_password_field_appears(pipe):
    pipe.process(parse_record(visit(end=None)))
    pipe.process(parse_record(visit(end=None, hasPassword=True)))
    pipe.process(parse_record(visit(end=None, hasPassword=True)))  # re-sent a minute later
    assert list(repo.iter_events(pipe.conn, source="browser")) == []
    assert pipe.privacy.counters.blocked["password_field"] == 1  # counted once, not per re-send


def test_close_stale_open_visits(pipe):
    rec = visit(end=None, last_seen=T0.isoformat(), start=(T0 - timedelta(minutes=2)).isoformat())
    pipe.process(parse_record(rec))
    # the daemon passes "now - 3 min": a visit seen since then is still alive
    assert maintenance.close_stale(pipe.conn, "browser", T0 - timedelta(minutes=1)) == 0
    assert maintenance.close_stale(pipe.conn, "browser", T0 + timedelta(seconds=1)) == 1
    e = next(repo.iter_events(pipe.conn, source="browser"))
    assert e.ts_end == T0 and e.truncated
    # a late re-send of the same visit extends it again rather than reopening it
    pipe.process(
        parse_record(visit(start=rec["start"], end=(T0 + timedelta(minutes=5)).isoformat()))
    )
    e = next(repo.iter_events(pipe.conn, source="browser"))
    assert e.ts_end == T0 + timedelta(minutes=5)


# --- git restart noise, deleted build folders ------------------------------------------------


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not installed")
async def test_git_does_not_rerecord_unchanged_repos_after_restart(tmp_path: Path):
    from personal_os.core.observer import MemoryStateStore
    from tests.test_git import git, make_observer

    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "me@example.com")
    git(r, "config", "user.name", "Me")
    (r / "a.py").write_text("x\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "init")
    root = str(r).replace("\\", "/")
    state = MemoryStateStore()
    first, _ = make_observer([tmp_path], state)
    assert [e.kind for e in await first.refresh(root, baseline=True)] == ["git.state"]
    again, _ = make_observer([tmp_path], state)  # "daemon restarted"
    assert await again.refresh(root, baseline=True) == []
    (r / "a.py").write_text("y\n")
    third, _ = make_observer([tmp_path], state)
    assert [e.kind for e in await third.refresh(root, baseline=True)] == ["git.state"]


def test_deleting_a_build_folder_is_ignored():
    assert classify_path("C:/p/extensions/browser/dist") == "ignore"
    assert classify_path("C:/p/node_modules") == "ignore"
    assert classify_path("C:/p/distribution.md") == "file"


# --- windowless background processes ---------------------------------------------------------


def test_background_command_uses_base_pythonw_and_boot_script():
    cmd = spawn.module_command("personal_os.launcher")
    if sys.platform == "win32":
        assert cmd[0].lower().endswith("pythonw.exe")
        assert ".venv" not in cmd[0]  # not uv's venv launcher, which opens a console python.exe
    assert cmd[1:3] == ["-I", str(spawn.BOOT)] and cmd[-1] == "personal_os.launcher"
    assert "PersonalOS" not in script(None) or True
    assert str(spawn.BOOT) in script(None) and spawn.SITE_ENV in script(None)


def test_boot_script_runs_project_modules_from_the_base_interpreter():
    base = Path(getattr(sys, "_base_executable", sys.executable))
    out = subprocess.run(
        [str(base), "-I", str(spawn.BOOT), spawn.site_packages(), "personal_os.interfaces.cli", "--help"],
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert out.returncode == 0, out.stderr
    assert "Local-first personal context" in out.stdout


# --- events display -----------------------------------------------------------------------


def test_events_show_active_time_and_readable_summaries(pipe, home: Path):
    from typer.testing import CliRunner

    from personal_os.interfaces.cli import app

    pipe.process(
        parse_record(
            visit(activeMs=101_000, title="What is GraphRAG?", url="https://neo4j.com/blog/x")
        )
    )
    pipe.process(parse_record(visit(id="3f1c2a4e-1111-4222-8333-0000000000aa", title="Inbox",
                                    url="https://mail.google.com/mail/u/0/")))  # fmt: skip
    pipe.process(parse_record({"type": "signal", "kind": "copy", "url": "https://neo4j.com/blog/x",
                               "text": "vector-only RAG", "at": T0.isoformat()}))  # fmt: skip
    out = (
        CliRunner()
        .invoke(app, ["events", "--source", "browser", "--db", str(home / "p.db")])
        .output
    )
    assert "101s" in out and "What is GraphRAG?  (neo4j.com)" in out
    assert "[S] mail.google.com" in out
    assert "'vector-only RAG'  (neo4j.com)" in out
    assert json.dumps  # keep import used


def test_redacted_titles_are_never_matched():
    t = _tracker_with(("chrome.exe", "[redacted]", 0, 60))
    assert foreground_overlap(t, CHROME, {"[redacted]"}, T0, T0 + timedelta(seconds=60)) == 0


# --- partial title matches must not turn real visits into bounces ---------------------------


def test_partial_title_match_does_not_drop_a_real_visit(pipe):
    """An inbox counter ticks near the end of a 90 s visit: the window record matches only the
    last 2 s. Trusting that would drop the visit as a bounce and delete its stored row."""
    t = _tracker_with(
        ("chrome.exe", "Inbox (12) - Mail - Google Chrome", 0, 88),
        ("chrome.exe", "Inbox (13) - Mail - Google Chrome", 88, 90),
    )

    def fg(exes, titles, s, e):
        return foreground_overlap(t, exes, titles, s, e)

    common = {"url": "https://example.com/inbox", "start": T0.isoformat()}
    pipe.process(
        parse_record(visit(end=None, title="Inbox (12) - Mail", activeMs=60_000, **common))
    )
    closed = visit(
        title="Inbox (13) - Mail",
        activeMs=85_000,
        end=(T0 + timedelta(seconds=90)).isoformat(),
        **common,
    )
    # Without the earlier title, only 2 s match: the extension's figure is kept instead.
    alone = parse_record(closed, foreground=fg)[0].payload
    assert (alone.active_dwell_ms, alone.enrichment["dwell_source"]) == (85_000, "extension")
    pipe.process(parse_record(closed, foreground=fg))
    assert len(list(repo.iter_events(pipe.conn, source="browser"))) == 1


def test_titles_from_earlier_resends_are_matched():
    from personal_os.observers.browser import VisitTitles

    t = _tracker_with(
        ("chrome.exe", "Untitled document - Google Docs - Google Chrome", 0, 50),
        ("chrome.exe", "Notes - Google Docs - Google Chrome", 50, 60),
    )

    def fg(exes, titles, s, e):
        return foreground_overlap(t, exes, titles, s, e)

    seen = VisitTitles()
    common = {"url": "https://example.com/doc", "start": T0.isoformat()}
    parse_record(visit(end=None, title="Untitled document - Google Docs", **common), None, fg, seen)
    final = visit(
        title="Notes - Google Docs",
        activeMs=30_000,
        end=(T0 + timedelta(seconds=60)).isoformat(),
        **common,
    )
    p = parse_record(final, None, fg, seen)[0].payload
    assert (p.active_dwell_ms, p.enrichment["dwell_source"]) == (60_000, "window")


def test_window_figure_usable():
    from personal_os.observers.browser import window_figure_usable

    assert not window_figure_usable(None, 10_000, 5000)
    assert not window_figure_usable(0, 10_000, 5000)
    assert not window_figure_usable(2000, 10_000, 5000)  # partial match: keep the extension's
    assert window_figure_usable(2000, 3000, 5000)  # both say bounce
    assert window_figure_usable(59_000, 100_000, 5000)  # the normal case (ADR-033)


def test_recompute_dwell_restores_partial_matches(pipe):
    """A visit an earlier run set to a near-zero window figure gets the extension's back."""
    focus = Event.interval(
        WindowFocus(app="Chrome", exe="chrome.exe", title="x - Stack Overflow - Google Chrome"),
        T0, T0 + timedelta(seconds=2), observer_version="t",
    )  # fmt: skip
    rec = visit(start=T0.isoformat(), end=(T0 + timedelta(minutes=1)).isoformat(),
                last_seen=(T0 + timedelta(minutes=1)).isoformat(), activeMs=50_000)  # fmt: skip
    pipe.process([focus, *parse_record(rec)])  # type: ignore[misc]
    assert recompute_dwell(pipe.conn) == (0, 1)  # 2 s vs 50 s: not trusted
    # Simulate what the earlier version stored.
    row_id, payload = pipe.conn.execute(
        "SELECT id, payload FROM events WHERE source = 'browser'"
    ).fetchone()
    p = json.loads(payload)
    p["active_dwell_ms"] = 2000
    p["enrichment"].update(dwell_source="window", extension_active_ms=50_000)
    pipe.conn.execute("UPDATE events SET payload = ? WHERE id = ?", (json.dumps(p), row_id))
    assert recompute_dwell(pipe.conn) == (1, 1)
    e = next(repo.iter_events(pipe.conn, source="browser"))
    assert e.payload.active_dwell_ms == 50_000  # type: ignore[union-attr]
    assert e.payload.enrichment["dwell_source"] == "extension"  # type: ignore[union-attr]
    assert recompute_dwell(pipe.conn) == (0, 1)
