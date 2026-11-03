from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest

from personal_os.config.schema import FsObserverConfig
from personal_os.core.bus import EventBus
from personal_os.core.clock import SystemClock
from personal_os.core.events import FsBulkChange, FsChange
from personal_os.core.logging import get_logger
from personal_os.core.observer import ObserverContext
from personal_os.observers.fs import (
    Debouncer,
    FsObserver,
    GitIgnoreCache,
    classify_path,
    diff_symbols,
    python_symbols,
)
from personal_os.observers.gitutil import _find_repo_root_cached
from personal_os.privacy.rules import PrivacyRules

NOW = datetime(2026, 11, 2, 9, 0).astimezone()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("C:/p/src/a.py", "file"),
        ("C:/p/node_modules/x/index.js", "ignore"),
        ("C:/p/.venv/Lib/site.py", "ignore"),
        ("C:\\p\\__pycache__\\a.pyc", "ignore"),
        ("C:/p/.git/objects/ab/cdef", "ignore"),
        ("C:/p/.git/index.lock", "ignore"),
        ("C:/p/.git/HEAD", "git"),
        ("C:/p/.git/refs/heads/main", "git"),
        ("C:/p/.git/index", "git"),
        ("C:/p/notes.md~", "ignore"),
        ("C:/p/~$report.docx", "ignore"),
        ("C:/p/file.tmp", "ignore"),
    ],
)
def test_classify_path(path, expected):
    assert classify_path(path) == expected


def test_debounce_editor_save_patterns():
    d = Debouncer(["C:/root"], quiet_s=2)
    # atomic save: write temp, rename over the original
    d.add("created", "C:/root/p/a.py.tmp123", 0.0, NOW)
    d.add("modified", "C:/root/p/a.py.tmp123", 0.1, NOW)
    d.add("moved", "C:/root/p/a.py.tmp123", 0.2, NOW, dest="C:/root/p/a.py")
    # delete + recreate (another editor style)
    d.add("deleted", "C:/root/p/b.py", 0.0, NOW)
    d.add("created", "C:/root/p/b.py", 0.1, NOW)
    # short-lived temp file
    d.add("created", "C:/root/p/scratch.txt", 0.0, NOW)
    d.add("deleted", "C:/root/p/scratch.txt", 0.5, NOW)
    # repeated saves
    for i in range(5):
        d.add("modified", "C:/root/p/c.py", i * 0.3, NOW)
    assert d.flush(1.0) == ([], [])  # nothing quiet yet
    ready, bulks = d.flush(5.0)
    assert {(p.path, p.kind) for p in ready} == {
        ("C:/root/p/a.py", "modified"),
        ("C:/root/p/b.py", "modified"),
        ("C:/root/p/c.py", "modified"),
    }
    assert bulks == []


def test_burst_becomes_one_bulk_change():
    d = Debouncer(["C:/root"], quiet_s=2, burst_per_s=50)
    for i in range(500):
        d.add("created", f"C:/root/proj/build-out/f{i}.js", i * 0.001, NOW)
    d.add("modified", "C:/root/other/readme.md", 0.2, NOW)
    ready, bulks = d.flush(10.0)
    assert [p.path for p in ready] == ["C:/root/other/readme.md"]
    assert len(bulks) == 1 and bulks[0].count == 500 and bulks[0].key == "C:/root/proj/build-out"
    assert len(bulks[0].samples) == 5


def test_python_symbols_names_and_changes():
    v1 = "class A:\n    def f(self):\n        return 1\n\ndef g():\n    pass\n"
    v2 = "class A:\n    def f(self):\n        return 2\n\ndef h():\n    pass\n"
    s1, s2 = python_symbols(v1), python_symbols(v2)
    assert set(s1) == {"A", "A.f", "g"}
    added, removed, changed = diff_symbols(s1, s2)
    assert added == ["h"] and removed == ["g"] and changed == ["A.f"]
    assert python_symbols("def broken(:\n") == {}


def test_gitignore_cache_nested(tmp_path: Path):
    (tmp_path / ".git" / "info").mkdir(parents=True)
    (tmp_path / ".gitignore").write_text("*.log\nbuild/\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".gitignore").write_text("secret_*.txt\n")
    (tmp_path / ".git" / "info" / "exclude").write_text("local.ini\n")
    gi = GitIgnoreCache()
    root = str(tmp_path)
    assert gi.is_ignored(root, str(tmp_path / "x.log"))
    assert gi.is_ignored(root, str(tmp_path / "build" / "a.js"))
    assert gi.is_ignored(root, str(tmp_path / "sub" / "secret_1.txt"))
    assert gi.is_ignored(root, str(tmp_path / "local.ini"))
    assert not gi.is_ignored(root, str(tmp_path / "secret_1.txt"))
    assert not gi.is_ignored(root, str(tmp_path / "src" / "a.py"))


# --- observer, fed directly (no watcher thread) -------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def observer(tmp_path: Path, config):
    rules = PrivacyRules.from_config(config.privacy, protected_dirs=[tmp_path / "data"])
    repos: list[str] = []
    obs = FsObserver([tmp_path], FsObserverConfig(enabled=True, debounce_s=0.0), rules,
                     on_repo_activity=repos.append)  # fmt: skip
    bus = EventBus()
    obs._ctx = ObserverContext(bus=bus, clock=SystemClock(), config=None, log=get_logger("t"))
    obs.repos_seen = repos  # type: ignore[attr-defined]
    _find_repo_root_cached.cache_clear()
    return obs, bus


async def test_change_outside_repo_line_delta_and_symbols(tmp_path: Path, observer):
    obs, bus = observer
    f = tmp_path / "notes" / "tool.py"
    f.parent.mkdir()
    f.write_text("def a():\n    pass\n")
    obs.on_raw("created", str(f), None)
    first = await obs.pump(time.monotonic() + 10)
    assert first[0].payload.change == "created" and first[0].payload.file_type == "code"
    f.write_text("def a():\n    return 1\n\ndef b():\n    pass\n")
    obs.on_raw("modified", str(f), None)
    second = (await obs.pump(time.monotonic() + 10))[0].payload
    assert isinstance(second, FsChange)
    assert second.lines_added == 3 and second.lines_removed == 0
    assert second.symbols_added == ["b"] and second.symbols_changed == ["a"]
    assert len(bus) == 2


async def test_change_inside_repo_uses_git_numstat_and_notifies(tmp_path: Path, observer):
    obs, _ = observer
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n")
    (repo / ".gitignore").write_text("*.out\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    (repo / "a.py").write_text("x = 1\ny = 2\nz = 3\n")
    (repo / "run.out").write_text("ignored")
    obs.on_raw("modified", str(repo / "a.py"), None)
    obs.on_raw("modified", str(repo / "run.out"), None)
    obs.on_raw("modified", str(repo / ".git" / "index"), None)
    events = await obs.pump(time.monotonic() + 10)
    assert len(events) == 1  # gitignored file dropped
    p = events[0].payload
    assert isinstance(p, FsChange) and p.lines_added == 2 and p.lines_removed == 0
    assert p.repo_root and p.repo_root.endswith("/proj")
    assert any(r.endswith("proj") for r in obs.repos_seen)  # type: ignore[attr-defined]


async def test_changes_made_by_git_operations_are_marked(tmp_path: Path, observer):
    obs, _ = observer
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "checkout", "-qb", "other")
    (repo / "a.py").write_text("x = 2\n")
    _git(repo, "commit", "-qam", "change")

    # your own edit, then a commit: commits don't count as worktree operations
    (repo / "a.py").write_text("x = 3\n")
    obs.on_raw("modified", str(repo / "a.py"), None)
    obs.on_raw("modified", str(repo / ".git" / "refs" / "heads" / "other"), None)
    obs.on_raw("modified", str(repo / ".git" / "logs" / "HEAD"), None)
    mine = await obs.pump(time.monotonic() + 3)
    assert mine and mine[0].payload.via_git is False

    # a checkout rewrites the worktree and HEAD
    _git(repo, "checkout", "-q", "--", "a.py")
    _git(repo, "checkout", "-q", "main")
    obs.on_raw("modified", str(repo / ".git" / "HEAD"), None)
    obs.on_raw("modified", str(repo / "a.py"), None)
    theirs = await obs.pump(time.monotonic() + 3)
    assert theirs and theirs[0].payload.via_git is True


async def test_secret_files_are_never_read(tmp_path: Path, observer, monkeypatch):
    obs, _ = observer
    secret = tmp_path / ".env"
    secret.write_text("API_KEY=abc\n")
    reads: list[Path] = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: reads.append(self) or real(self))
    obs.on_raw("modified", str(secret), None)
    events = await obs.pump(time.monotonic() + 10)
    p = events[0].payload
    assert isinstance(p, FsChange) and p.sensitive and p.lines_added is None
    assert secret not in reads


async def test_protected_dir_and_paused_events_dropped(tmp_path: Path, observer):
    obs, _ = observer
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "personal_os.db").write_text("x")
    obs.on_raw("modified", str(tmp_path / "data" / "personal_os.db"), None)
    obs._ctx.paused.set()
    (tmp_path / "x.txt").write_text("x")
    obs.on_raw("modified", str(tmp_path / "x.txt"), None)
    assert await obs.pump(time.monotonic() + 10) == []


async def test_metadata_only_modification_is_ignored(tmp_path: Path, observer):
    import os

    obs, _ = observer
    f = tmp_path / "old.md"
    f.write_text("unchanged\n")
    old = time.time() - 3600
    os.utime(f, (old, old))  # last edited an hour ago; now something merely reads it
    obs.on_raw("modified", str(f), None)
    assert await obs.pump(time.monotonic() + 10) == []
    f.write_text("edited\n")  # a real edit
    obs.on_raw("modified", str(f), None)
    assert len(await obs.pump(time.monotonic() + 10)) == 1


async def test_bulk_event_emitted(tmp_path: Path, observer):
    obs, _ = observer
    obs.debouncer.burst_per_s = 5
    d = tmp_path / "proj" / "out"
    d.mkdir(parents=True)
    for i in range(30):
        (d / f"f{i}.txt").write_text("x")
        obs.on_raw("created", str(d / f"f{i}.txt"), None)
    events = await obs.pump(time.monotonic() + 10)
    bulk = [e for e in events if isinstance(e.payload, FsBulkChange)]
    assert len(bulk) == 1 and bulk[0].payload.count >= 25


@pytest.mark.live
async def test_watchdog_end_to_end(tmp_path: Path, config):
    rules = PrivacyRules.from_config(config.privacy)
    obs = FsObserver([tmp_path], FsObserverConfig(enabled=True, debounce_s=0.5), rules,
                     flush_every_s=0.2)  # fmt: skip
    bus = EventBus()
    ctx = ObserverContext(bus=bus, clock=SystemClock(), config=None, log=get_logger("t"))
    task = asyncio.create_task(obs.start(ctx))
    await asyncio.sleep(0.5)
    (tmp_path / "hello.md").write_text("# hi\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("x")
    for _ in range(40):
        await asyncio.sleep(0.1)
        if len(bus):
            break
    await asyncio.sleep(1.0)
    task.cancel()
    events = bus.drain()
    paths = [e.payload.path for e in events]  # type: ignore[union-attr]
    assert any(p.endswith("hello.md") for p in paths)
    assert not any("node_modules" in p for p in paths)
