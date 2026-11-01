from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from personal_os.config.schema import GitObserverConfig
from personal_os.core.bus import EventBus
from personal_os.core.clock import SystemClock
from personal_os.core.events import GitBranchSwitch, GitCommit, GitOp, GitState
from personal_os.core.logging import get_logger
from personal_os.core.observer import MemoryStateStore, ObserverContext
from personal_os.observers.git import HEADS_KEY, GitObserver
from personal_os.observers.gitutil import (
    discover_repos,
    git_dir,
    parse_log,
    parse_numstat_z,
    parse_status_v2,
    read_head_branch,
)

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="git not installed")


def test_parse_status_v2():
    raw = "\0".join([
        "# branch.oid 1234567890abcdef1234567890abcdef12345678",
        "# branch.head feature/oauth",
        "# branch.upstream origin/feature/oauth",
        "# branch.ab +4 -1",
        "# stash 2",
        "1 .M N... 100644 100644 100644 aaaa bbbb src/auth/session.py",
        "1 A. N... 000000 100644 100644 0000 cccc tests/test new.py",
        "2 R. N... 100644 100644 100644 dddd eeee R100 docs/new.md",
        "docs/old.md",
        "u UU N... 100644 100644 100644 100644 a b c conflict.txt",
        "? notes.txt",
        "",
    ]).encode()  # fmt: skip
    s = parse_status_v2(raw)
    assert s.head and s.head.startswith("12345") and s.branch == "feature/oauth"
    assert (s.ahead, s.behind, s.stash_count) == (4, 1, 2)
    assert (s.staged, s.unstaged, s.untracked) == (3, 2, 1)
    assert [f.path for f in s.files] == [
        "src/auth/session.py", "tests/test new.py", "docs/new.md", "conflict.txt", "notes.txt",
    ]  # fmt: skip


def test_parse_status_initial_and_detached():
    s = parse_status_v2(b"# branch.oid (initial)\0# branch.head (detached)\0")
    assert s.head is None and s.branch is None


def test_parse_numstat_with_rename_and_binary():
    raw = b"3\t1\tsrc/a.py\0-\t-\timg.png\0" + b"5\t0\t\0old.md\0new.md\0"
    assert parse_numstat_z(raw) == {"src/a.py": (3, 1), "img.png": (None, None), "new.md": (5, 0)}


def test_parse_log():
    raw = b"abc\x1fp1 p2\x1fme@x.com\x1fMerge branch\n\nbody\n\x1e\ndef\x1fp1\x1fyou@x.com\x1fFix\n\x1e\n"
    commits = parse_log(raw)
    assert [(c.sha, c.parents, c.author_email) for c in commits] == [
        ("abc", 2, "me@x.com"), ("def", 1, "you@x.com"),
    ]  # fmt: skip
    assert commits[0].message == "Merge branch\n\nbody"


# --- against a real repository -------------------------------------------------------------


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "Documents" / "proj"
    r.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "me@example.com")
    git(r, "config", "user.name", "Me")
    (r / "a.py").write_text("x = 1\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "initial")
    return r


def make_observer(roots: list[Path], state=None) -> tuple[GitObserver, EventBus]:
    obs = GitObserver(roots, GitObserverConfig())
    bus = EventBus()
    obs._ctx = ObserverContext(bus=bus, clock=SystemClock(), config=None, log=get_logger("t"),
                               state=state or MemoryStateStore())  # fmt: skip
    return obs, bus


def test_discover_skips_dependency_dirs(tmp_path: Path, repo: Path):
    (tmp_path / "Documents" / "node_modules" / "pkg" / ".git").mkdir(parents=True)
    nested = repo / "vendor" / "lib"
    nested.mkdir(parents=True)
    (nested / ".git").mkdir()
    found = discover_repos([tmp_path / "Documents"])
    assert [Path(f).name for f in found] == ["proj"]  # stops at a repo root; skips node_modules


def test_head_and_gitdir_helpers(repo: Path, tmp_path: Path):
    assert read_head_branch(repo) == "main"
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    assert git_dir(wt).is_dir() and read_head_branch(wt) == "side"


async def test_state_commits_branch_switch_and_op(repo: Path):
    obs, _ = make_observer([repo.parent])
    root = str(repo).replace("\\", "/")
    first = await obs.refresh(root, baseline=True)
    assert [e.kind for e in first] == ["git.state"]
    assert await obs.refresh(root) == []  # unchanged → nothing emitted

    (repo / "a.py").write_text("x = 1\ny = 2\n")
    (repo / "new.txt").write_text("n\n")
    dirty = await obs.refresh(root)
    st = dirty[0].payload
    assert isinstance(st, GitState) and st.unstaged == 1 and st.untracked == 1
    assert {f.path: f.added for f in st.files}["a.py"] == 1

    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Add y\n\nwith a body")
    git(repo, "checkout", "-qb", "feature/x")
    (repo / "b.py").write_text("b\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Add b")
    events = await obs.refresh(root)
    kinds = [e.kind for e in events]
    assert kinds.count("git.commit") == 2 and "git.branch_switch" in kinds and "git.state" in kinds
    commits = [e.payload for e in events if isinstance(e.payload, GitCommit)]
    assert [c.message.splitlines()[0] for c in commits] == ["Add y", "Add b"]
    assert (
        commits[0].author_is_me
        and commits[0].insertions == 2
        and commits[0].files == ["a.py", "new.txt"]
    )
    switch = next(e.payload for e in events if isinstance(e.payload, GitBranchSwitch))
    assert (switch.from_branch, switch.to_branch) == ("main", "feature/x")
    assert all(e.project_hint == root for e in events)

    # a merge conflict puts the repo in an operation
    git(repo, "checkout", "-q", "main")
    (repo / "b.py").write_text("main version\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "b on main")
    subprocess.run(["git", "merge", "feature/x"], cwd=repo, capture_output=True)
    events = await obs.refresh(root)
    ops = [e.payload for e in events if isinstance(e.payload, GitOp)]
    assert ops and ops[0].op == "merge" and ops[0].state == "started"
    git(repo, "merge", "--abort")
    events = await obs.refresh(root)
    ops = [e.payload for e in events if isinstance(e.payload, GitOp)]
    assert ops and ops[0].state == "finished"


async def test_commits_made_while_daemon_was_off_are_found(repo: Path):
    state = MemoryStateStore()
    obs, _ = make_observer([repo.parent], state)
    root = str(repo).replace("\\", "/")
    await obs.refresh(root, baseline=True)
    assert state.get(HEADS_KEY)[root]
    # "daemon stops"; user commits; a fresh observer starts with the persisted HEADs
    (repo / "c.py").write_text("c\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "offline work")
    obs2, _ = make_observer([repo.parent], state)
    events = await obs2.refresh(root, baseline=True)
    assert [e.payload.message for e in events if isinstance(e.payload, GitCommit)] == [
        "offline work"
    ]


async def test_never_takes_index_lock(repo: Path):
    obs, _ = make_observer([repo.parent])
    root = str(repo).replace("\\", "/")
    lock = repo / ".git" / "index.lock"
    lock.write_text("")  # simulate the user's git holding the index
    try:
        events = await obs.refresh(root, baseline=True)  # must not fail or wait
        assert events and events[0].kind == "git.state"
    finally:
        lock.unlink()


async def test_notify_debounces(repo: Path):
    obs, _ = make_observer([repo.parent])
    root = str(repo).replace("\\", "/")
    for _ in range(10):
        obs.notify(root)
    assert list(obs._pending.due) == [root]
