"""git helpers: repo discovery, lock-free CLI runner, and porcelain parsers (ADR-005).

All git commands run with --no-optional-locks and GIT_OPTIONAL_LOCKS=0, so we never take
index.lock and never get in the way of the user's own git commands.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from personal_os.core.events import GitFileStat

IGNORED_DIR_NAMES = frozenset(
    {
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
        ".cache",
        ".gradle",
        ".idea",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".terraform",
        "bower_components",
        ".parcel-cache",
        ".turbo",
        ".svelte-kit",
        "site-packages",
        ".hypothesis",
        "coverage",
        ".angular",
    }
)


def norm(path: str | Path) -> str:
    """Forward slashes, no trailing slash. Case is preserved (only compared case-insensitively)."""
    return str(path).replace("\\", "/").rstrip("/")


@lru_cache(maxsize=4096)
def _find_repo_root_cached(directory: str) -> str | None:
    d = Path(directory)
    for candidate in (d, *d.parents):
        if (candidate / ".git").exists():
            return norm(candidate)
    return None


def find_repo_root(path: str | Path) -> str | None:
    """Nearest ancestor containing .git (dir, or file for worktrees/submodules). Cached per dir."""
    p = Path(path)
    directory = p if p.is_dir() else p.parent
    return _find_repo_root_cached(str(directory))


def git_dir(repo_root: str | Path) -> Path:
    """The real .git directory (follows `gitdir:` files used by worktrees and submodules)."""
    dot_git = Path(repo_root) / ".git"
    if dot_git.is_file():
        text = dot_git.read_text("utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            target = Path(text.split(":", 1)[1].strip())
            return target if target.is_absolute() else (Path(repo_root) / target).resolve()
    return dot_git


def read_head_branch(repo_root: str | Path) -> str | None:
    """Current branch by reading HEAD directly (no subprocess). None when detached."""
    try:
        head = (git_dir(repo_root) / "HEAD").read_text("utf-8", errors="replace").strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    return head[len(prefix) :] if head.startswith(prefix) else None


def detect_op(repo_root: str | Path) -> str:
    g = git_dir(repo_root)
    if (g / "rebase-merge").exists() or (g / "rebase-apply").exists():
        return "rebase"
    if (g / "MERGE_HEAD").exists():
        return "merge"
    if (g / "CHERRY_PICK_HEAD").exists():
        return "cherry_pick"
    if (g / "REVERT_HEAD").exists():
        return "revert"
    if (g / "BISECT_LOG").exists():
        return "bisect"
    return "none"


def discover_repos(roots: list[Path], max_depth: int = 4) -> list[str]:
    """Walk watched roots (skipping dependency/build dirs) and return repo roots found."""
    found: list[str] = []

    def walk(d: Path, depth: int) -> None:
        if (d / ".git").exists():
            found.append(norm(d))
            return  # nested repos (submodules) are found via their own events later
        if depth >= max_depth:
            return
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            name = entry.name
            if name in IGNORED_DIR_NAMES or name.startswith(".") or name.startswith("$"):
                continue
            walk(Path(entry.path), depth + 1)

    for root in roots:
        if root.is_dir():
            walk(root, 0)
    return sorted(set(found))


def normalize_remote(url: str) -> str | None:
    """'git@github.com:Owner/Repo.git' or 'https://user:tok@github.com/Owner/Repo' ->
    'github.com/owner/repo'. Credentials are dropped; None if it isn't a host/path remote."""
    url = url.strip()
    m = re.match(r"^(?:[\w.+-]+@)?([\w.-]+):(?!//)(.+)$", url)  # scp-like: git@host:path
    if m and "://" not in url:
        host, path = m.group(1), m.group(2)
        if len(host) == 1:
            return None  # "C:/path": a Windows drive letter, i.e. a local remote
    else:
        m = re.match(r"^[a-z][a-z0-9+.-]*://(?:[^@/]*@)?([^/:]+)(?::\d+)?/(.+)$", url, re.I)
        if not m:
            return None
        host, path = m.group(1), m.group(2)
    path = path.strip("/").removesuffix(".git").strip("/")
    return f"{host.lower()}/{path.lower()}" if path else None


# --- runner --------------------------------------------------------------------------------


class GitError(RuntimeError):
    pass


@dataclass
class GitRunner:
    executable: str = "git"
    timeout_s: float = 10.0
    env: dict[str, str] = field(default_factory=dict)

    async def run(self, repo: str, *args: str, check: bool = True) -> bytes:
        env = {
            **os.environ,
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            **self.env,
        }
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = await asyncio.create_subprocess_exec(
            self.executable,
            "--no-optional-locks",
            "-c",
            "core.quotepath=off",
            *args,
            cwd=repo,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=flags,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), self.timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise GitError(f"git {args[0]} timed out after {self.timeout_s}s") from None
        if check and proc.returncode != 0:
            msg = err.decode("utf-8", "replace").strip().splitlines()
            raise GitError(f"git {args[0]} failed: {msg[-1] if msg else proc.returncode}")
        return out


# --- parsers -------------------------------------------------------------------------------


@dataclass
class StatusInfo:
    head: str | None = None
    branch: str | None = None
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    stash_count: int = 0
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    files: list[GitFileStat] = field(default_factory=list)


def parse_status_v2(raw: bytes, max_files: int = 50) -> StatusInfo:
    """Parse `git status --porcelain=v2 --branch --show-stash -z`."""
    info = StatusInfo()
    fields = raw.decode("utf-8", "replace").split("\0")
    i = 0
    while i < len(fields):
        rec = fields[i]
        i += 1
        if not rec:
            continue
        if rec.startswith("# "):
            key, _, value = rec[2:].partition(" ")
            if key == "branch.oid":
                info.head = None if value == "(initial)" else value
            elif key == "branch.head":
                info.branch = None if value == "(detached)" else value
            elif key == "branch.upstream":
                info.upstream = value
            elif key == "branch.ab":
                a, _, b = value.partition(" ")
                info.ahead, info.behind = abs(int(a)), abs(int(b))
            elif key == "stash":
                info.stash_count = int(value)
            continue
        kind = rec[0]
        if kind == "?":
            info.untracked += 1
            _add_file(info, rec[2:], "??", max_files)
        elif kind in "12u":
            parts = rec.split(" ")
            xy = parts[1]
            n_fields = {"1": 8, "2": 9, "u": 10}[kind]
            path = " ".join(parts[n_fields:])
            if kind == "2":
                i += 1  # original path follows as its own NUL-separated field
            if xy[0] != "." or kind == "u":
                info.staged += 1
            if xy[1] != "." or kind == "u":
                info.unstaged += 1
            _add_file(info, path, xy.replace(".", " ").strip() or "M", max_files)
    return info


def _add_file(info: StatusInfo, path: str, status: str, max_files: int) -> None:
    if len(info.files) < max_files:
        info.files.append(GitFileStat(path=path, status=status))


def parse_numstat_z(raw: bytes) -> dict[str, tuple[int | None, int | None]]:
    """Parse `git diff --numstat -z`. Binary files give (None, None)."""
    out: dict[str, tuple[int | None, int | None]] = {}
    fields = raw.decode("utf-8", "replace").split("\0")
    i = 0
    while i < len(fields):
        rec = fields[i]
        i += 1
        if not rec:
            continue
        parts = rec.split("\t")
        if len(parts) < 3:
            continue
        added = None if parts[0] == "-" else int(parts[0])
        removed = None if parts[1] == "-" else int(parts[1])
        path = parts[2]
        if path == "" and i + 1 < len(fields):  # rename: "\0old\0new"
            path = fields[i + 1]
            i += 2
        out[path] = (added, removed)
    return out


@dataclass
class CommitInfo:
    sha: str
    parents: int
    author_email: str
    message: str


def parse_log(raw: bytes) -> list[CommitInfo]:
    """Parse `git log --format=%H%x1f%P%x1f%ae%x1f%B%x1e`."""
    commits: list[CommitInfo] = []
    for rec in raw.decode("utf-8", "replace").split("\x1e"):
        rec = rec.strip("\n")
        if not rec:
            continue
        parts = rec.split("\x1f")
        if len(parts) < 4:
            continue
        sha, parents, email, message = parts[0], parts[1], parts[2], parts[3]
        commits.append(
            CommitInfo(sha.strip(), len(parents.split()), email.strip(), message.strip())
        )
    return commits
