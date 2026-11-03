"""Filesystem observer (DESIGN.md §4.6).

watchdog (ReadDirectoryChangesW on Windows) → cheap ignore filter in the watcher thread →
thread-safe queue → `Debouncer` on the event loop (pure, unit-tested) → enrichment (size,
type, repo, diff summary, Python symbols) → fs.change / fs.bulk_change events.

File contents are read only to count lines and to list Python symbol names, never stored.
Secret files, cloud-only (OneDrive) placeholders, binaries and files over 2 MB are not read.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import os
import queue
import stat as stat_mod
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

import pathspec

from personal_os.config.schema import FsObserverConfig
from personal_os.core.events import Event, FsBulkChange, FsChange
from personal_os.core.observer import BaseObserver, ObserverContext
from personal_os.observers.gitutil import IGNORED_DIR_NAMES, GitRunner, find_repo_root, norm
from personal_os.privacy.rules import PrivacyRules

VERSION = "1.0.0"
MAX_READ_BYTES = 2_000_000
# A real edit sets mtime to about "now"; a "modified" event whose file mtime is older than the
# event by more than this was a metadata-only change (e.g. last-access time after a read).
MTIME_SLACK_S = 10.0


def _narrow_windows_notify_flags() -> None:
    """watchdog asks Windows for every change type, including LAST_ACCESS, ATTRIBUTES and
    SECURITY, so merely *reading* a file (git status, antivirus, indexer) shows up as a
    modification. Keep only name, size and last-write changes. The mtime check in _enrich
    remains as a backstop if a watchdog upgrade renames this constant."""
    if sys.platform != "win32":
        return
    try:
        from watchdog.observers import winapi

        winapi.WATCHDOG_FILE_NOTIFY_FLAGS = (
            winapi.FILE_NOTIFY_CHANGE_FILE_NAME
            | winapi.FILE_NOTIFY_CHANGE_DIR_NAME
            | winapi.FILE_NOTIFY_CHANGE_SIZE
            | winapi.FILE_NOTIFY_CHANGE_LAST_WRITE
        )
    except (ImportError, AttributeError):
        pass


Kind = Literal["created", "modified", "deleted", "moved"]

# Git internals that mean "repo state may have changed".
_GIT_TRIGGERS = (
    "HEAD",
    "index",
    "ORIG_HEAD",
    "MERGE_HEAD",
    "FETCH_HEAD",
    "packed-refs",
    "refs/",
    "logs/HEAD",
    "rebase-merge",
    "rebase-apply",
    "CHERRY_PICK_HEAD",
)
_IGNORED_SUFFIXES = (
    ".tmp",
    ".temp",
    ".swp",
    ".swo",
    ".swx",
    "~",
    ".crdownload",
    ".part",
    ".pyc",
    ".pyo",
    ".lock",
    ".log",
)
_IGNORED_PREFIXES = ("~$", ".#", "#")
_CODE_EXTS = {
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".rs",
    ".go",
    ".java",
    ".kt",
    ".cs",
    ".cpp",
    ".cc",
    ".c",
    ".h",
    ".hpp",
    ".rb",
    ".php",
    ".swift",
    ".sql",
    ".sh",
    ".ps1",
    ".psm1",
    ".lua",
    ".dart",
    ".scala",
    ".vue",
    ".svelte",
    ".r",
}
_TEXT_EXTS = {
    ".md",
    ".txt",
    ".rst",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".ini",
    ".cfg",
    ".csv",
    ".xml",
    ".html",
    ".css",
    ".scss",
    ".tex",
}
_DOC_EXTS = {".docx", ".doc", ".pdf", ".pptx", ".xlsx", ".odt", ".ods"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}
_ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz"}
# Windows attributes for files that are not really on disk (OneDrive "online-only").
_CLOUD_ATTRS = (
    0x00400000 | 0x00040000 | 0x00001000
)  # RECALL_ON_DATA_ACCESS | RECALL_ON_OPEN | OFFLINE


def file_type(path: str) -> str:
    ext = PurePosixPath(norm(path)).suffix.lower()
    for kind, exts in (
        ("code", _CODE_EXTS),
        ("text", _TEXT_EXTS),
        ("doc", _DOC_EXTS),
        ("image", _IMAGE_EXTS),
        ("archive", _ARCHIVE_EXTS),
    ):
        if ext in exts:
            return kind
    return "other"


# --- fast ignore (runs in the watcher thread) ----------------------------------------------


def classify_path(path: str, extra_ignored: frozenset[str] = frozenset()) -> str:
    """'ignore', 'git' (a .git internal worth a git refresh) or 'file'."""
    p = norm(path)
    parts = p.split("/")
    if ".git" in parts:
        rest = "/".join(parts[parts.index(".git") + 1 :])
        if rest.startswith("objects") or rest.startswith("lfs") or rest.endswith(".lock"):
            return "ignore"
        return "git" if rest.startswith(_GIT_TRIGGERS) else "ignore"
    names = IGNORED_DIR_NAMES | extra_ignored
    if any(part in names for part in parts[:-1]):
        return "ignore"
    name = parts[-1]
    if name.startswith(_IGNORED_PREFIXES) or name.lower().endswith(_IGNORED_SUFFIXES):
        return "ignore"
    return "file"


# .git entries that git writes when an operation rewrites the worktree. A commit doesn't touch
# these (it updates refs/ and logs/), so your own edits right before a commit aren't affected.
_WORKTREE_OPS = ("HEAD", "ORIG_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD",
                 "rebase-merge", "rebase-apply")  # fmt: skip
GIT_OP_WINDOW_S = 10.0


def git_internal_name(path: str) -> str:
    """'C:/r/.git/refs/heads/x' -> 'refs/heads/x'."""
    p = norm(path)
    return p.split("/.git/", 1)[1] if "/.git/" in p else ""


def git_root_of_internal(path: str) -> str:
    p = norm(path)
    return p[: p.index("/.git/")] if "/.git/" in p else p


# --- debouncer (pure) ----------------------------------------------------------------------


@dataclass
class Pending:
    path: str
    kind: Kind
    first_wall: datetime
    last_t: float
    dest: str | None = None
    count: int = 1


@dataclass
class Bulk:
    key: str
    first_wall: datetime
    last_t: float
    count: int = 0
    kinds: Counter[str] = field(default_factory=Counter)
    samples: list[str] = field(default_factory=list)


class Debouncer:
    """Coalesces editor save patterns and collapses bursts (git checkout, unzip, builds).

    Rules per path within the quiet window: created+modified → created, created+deleted →
    nothing (temp file), deleted+created → modified (atomic replace), temp→real rename →
    modified. More than `burst_per_s` events in one second under the same burst key (the first
    two folders below a watched root) become one bulk change until that key goes quiet.
    """

    def __init__(self, roots: list[str], quiet_s: float = 2.0, burst_per_s: int = 50) -> None:
        self.roots = sorted((norm(r) for r in roots), key=len, reverse=True)
        self.quiet_s = quiet_s
        self.burst_per_s = burst_per_s
        self.pending: dict[str, Pending] = {}
        self.bulks: dict[str, Bulk] = {}
        self._rate: dict[str, tuple[float, int]] = {}

    def burst_key(self, path: str) -> str:
        p = norm(path)
        for root in self.roots:
            if p.lower().startswith(root.lower() + "/"):
                rel = p[len(root) + 1 :].split("/")
                return "/".join([root, *rel[: min(2, len(rel) - 1)]])
        return str(PurePosixPath(p).parent)

    def add(self, kind: Kind, path: str, t: float, wall: datetime, dest: str | None = None) -> None:
        path = norm(path)
        dest = norm(dest) if dest else None
        key = self.burst_key(dest or path)
        start, n = self._rate.get(key, (t, 0))
        if t - start >= 1.0:
            start, n = t, 0
        self._rate[key] = (start, n + 1)
        if key in self.bulks or n + 1 > self.burst_per_s:
            self._to_bulk(key, kind, dest or path, t, wall)
            return
        if kind == "moved" and dest:
            src = self.pending.pop(path, None)
            if src is not None and src.kind == "created":
                self._merge(dest, "modified", t, wall)  # editor wrote a temp file then renamed it
            else:
                self.pending[path] = Pending(path, "moved", wall, t, dest)
            return
        self._merge(path, kind, t, wall)

    def _merge(self, path: str, kind: Kind, t: float, wall: datetime) -> None:
        cur = self.pending.get(path)
        if cur is None:
            self.pending[path] = Pending(path, kind, wall, t)
            return
        cur.count += 1
        cur.last_t = t
        if cur.kind == "created" and kind == "deleted":
            del self.pending[path]  # short-lived temp file
        elif cur.kind == "created":
            pass  # created then modified: still a creation
        elif cur.kind == "deleted" and kind == "created":
            cur.kind = "modified"  # atomic replace
        elif kind == "deleted":
            cur.kind = "deleted"
        elif cur.kind != "moved":
            cur.kind = "modified"

    def _to_bulk(self, key: str, kind: str, path: str, t: float, wall: datetime) -> None:
        bulk = self.bulks.get(key)
        if bulk is None:
            bulk = self.bulks[key] = Bulk(key, wall, t)
            # absorb anything already pending under this key
            for p in [p for p in self.pending if self.burst_key(p) == key]:
                pend = self.pending.pop(p)
                bulk.count += pend.count
                bulk.kinds[pend.kind] += pend.count
                if len(bulk.samples) < 5:
                    bulk.samples.append(p)
        bulk.count += 1
        bulk.kinds[kind] += 1
        bulk.last_t = t
        if len(bulk.samples) < 5 and path not in bulk.samples:
            bulk.samples.append(path)

    def flush(self, t: float) -> tuple[list[Pending], list[Bulk]]:
        ready = [p for p in self.pending.values() if t - p.last_t >= self.quiet_s]
        for p in ready:
            del self.pending[p.path]
        bulks = [b for b in self.bulks.values() if t - b.last_t >= self.quiet_s]
        for b in bulks:
            del self.bulks[b.key]
        return sorted(ready, key=lambda p: p.last_t), bulks


# --- enrichment ----------------------------------------------------------------------------


def python_symbols(source: str) -> dict[str, str]:
    """{qualified name: short hash of its AST} for functions and classes. Names only leave here."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    out: dict[str, str] = {}

    def visit(nodes: list[ast.stmt], prefix: str) -> None:
        for node in nodes:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{prefix}{node.name}"
                out[name] = hashlib.blake2s(ast.dump(node).encode(), digest_size=6).hexdigest()
                if isinstance(node, ast.ClassDef):
                    visit(node.body, f"{name}.")

    visit(tree.body, "")
    return out


def diff_symbols(
    old: dict[str, str], new: dict[str, str]
) -> tuple[list[str], list[str], list[str]]:
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(k for k in set(old) & set(new) if old[k] != new[k])
    # A changed method also changes its class's hash; report the method, not the class.
    changed = [c for c in changed if not any(o.startswith(c + ".") for o in changed)]
    return added, removed, changed


def _is_cloud_placeholder(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _CLOUD_ATTRS)


def _read_text(path: Path, st: os.stat_result) -> str | None:
    if st.st_size > MAX_READ_BYTES or _is_cloud_placeholder(st):
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None  # binary
    return data.decode("utf-8", errors="replace")


class GitIgnoreCache:
    """.gitignore matching per repo, including nested .gitignore files and .git/info/exclude."""

    def __init__(self) -> None:
        self._specs: dict[str, tuple[float, pathspec.GitIgnoreSpec | None]] = {}

    def _spec(self, directory: str, ignore_file: Path) -> pathspec.GitIgnoreSpec | None:
        try:
            mtime = ignore_file.stat().st_mtime
        except OSError:
            self._specs[directory] = (0.0, None)
            return None
        cached = self._specs.get(directory)
        if cached and cached[0] == mtime:
            return cached[1]
        lines = ignore_file.read_text("utf-8", errors="replace").splitlines()
        spec = pathspec.GitIgnoreSpec.from_lines(lines)
        self._specs[directory] = (mtime, spec)
        return spec

    def is_ignored(self, repo_root: str, path: str) -> bool:
        root, p = norm(repo_root), norm(path)
        if not p.lower().startswith(root.lower() + "/"):
            return False
        rel_parts = p[len(root) + 1 :].split("/")
        exclude = self._spec(root + "#exclude", Path(root) / ".git" / "info" / "exclude")
        if exclude and exclude.match_file("/".join(rel_parts)):
            return True
        for depth in range(len(rel_parts)):
            directory = "/".join([root, *rel_parts[:depth]])
            spec = self._spec(directory, Path(directory) / ".gitignore")
            if spec is not None and spec.match_file("/".join(rel_parts[depth:])):
                return True
        return False


# --- observer ------------------------------------------------------------------------------


class FsObserver(BaseObserver):
    name = "fs"
    version = VERSION

    def __init__(
        self,
        roots: list[Path],
        cfg: FsObserverConfig,
        rules: PrivacyRules,
        *,
        on_repo_activity: Callable[[str], None] | None = None,
        runner: GitRunner | None = None,
        flush_every_s: float = 0.5,
    ) -> None:
        super().__init__()
        self.roots = [r for r in roots if r.is_dir()]
        self.cfg = cfg
        self.rules = rules
        self.on_repo_activity = on_repo_activity
        self.runner = runner or GitRunner()
        self.flush_every_s = flush_every_s
        self.debouncer = Debouncer(
            [str(r) for r in self.roots], cfg.debounce_s, cfg.burst_events_per_s
        )
        self.gitignore = GitIgnoreCache()
        self.raw: queue.SimpleQueue[tuple[str, str, str | None, float, datetime]] = (
            queue.SimpleQueue()
        )
        self._symbols: dict[str, dict[str, str]] = {}  # path -> {symbol: hash}; names only
        self._lines: dict[str, int] = {}
        self._git_ops: dict[str, float] = {}  # repo (lower-case) -> monotonic time of last op
        self._watcher: object | None = None
        self.dropped_raw = 0

    # Called from the watchdog thread: must be cheap and never raise.
    def on_raw(self, kind: str, src: str, dest: str | None) -> None:
        try:
            if self._ctx is None or self._ctx.paused.is_set():
                return
            target = dest or src
            cls = classify_path(target)
            if cls == "git":
                self.raw.put(
                    (
                        "git",
                        git_root_of_internal(target),
                        git_internal_name(target),
                        time.monotonic(),
                        datetime.now().astimezone(),
                    )
                )
            elif cls == "file" and not self.rules.is_protected_path(target):
                self.raw.put((kind, src, dest, time.monotonic(), datetime.now().astimezone()))
        except Exception:
            self.dropped_raw += 1

    async def run(self, ctx: ObserverContext) -> None:
        if not self.roots:
            raise RuntimeError("no watched roots exist")
        self._start_watcher()
        try:
            while True:
                await asyncio.sleep(self.flush_every_s)
                await self.pump(time.monotonic())
        finally:
            self._stop_watcher()

    async def pump(self, now_t: float) -> list[Event]:
        """Drain the raw queue, flush quiet changes, emit events. Separated out for tests."""
        repos_touched: set[str] = set()
        while True:
            try:
                kind, src, dest, t, wall = self.raw.get_nowait()
            except queue.Empty:
                break
            if kind == "git":
                repos_touched.add(src)
                if dest and dest.startswith(_WORKTREE_OPS):
                    self._git_ops[norm(src).lower()] = t
                continue
            self.debouncer.add(kind, src, t, wall, dest)  # type: ignore[arg-type]
        ready, bulks = self.debouncer.flush(now_t)
        events: list[Event] = []
        for pend in ready:
            event = await self._enrich(pend)
            if event is not None:
                events.append(event)
                if isinstance(event.payload, FsChange) and event.payload.repo_root:
                    repos_touched.add(event.payload.repo_root)
        for bulk in bulks:
            events.append(
                Event.point(
                    FsBulkChange(
                        dir=bulk.key,
                        count=bulk.count,
                        kinds=dict(bulk.kinds),
                        sample_paths=bulk.samples,
                    ),
                    bulk.first_wall,
                    observer_version=VERSION,
                )
            )
            repo = find_repo_root(bulk.key) if Path(bulk.key).exists() else None
            if repo:
                repos_touched.add(repo)
        for e in events:
            self.emit(e)
        if self.on_repo_activity is not None:
            for repo in repos_touched:
                self.on_repo_activity(repo)
        return events

    async def _enrich(self, pend: Pending) -> Event | None:
        path = pend.dest if pend.kind == "moved" and pend.dest else pend.path
        p = Path(path)
        repo_root = find_repo_root(p.parent) if p.parent.exists() else None
        if repo_root and self.gitignore.is_ignored(repo_root, path):
            return None
        secret = self.rules.is_secret_path(path) or self.rules.is_secret_path(pend.path)
        size: int | None = None
        added = removed = None
        sym_added: list[str] = []
        sym_removed: list[str] = []
        sym_changed: list[str] = []
        if pend.kind != "deleted":
            try:
                st = p.stat()
            except OSError:
                return None  # vanished before we looked: a temp file after all
            if stat_mod.S_ISDIR(st.st_mode):
                return None
            stale = st.st_mtime < pend.first_wall.timestamp() - MTIME_SLACK_S
            if pend.kind == "modified" and stale:
                return None  # only metadata changed (a read, attributes, ACL): not an edit
            size = st.st_size
            if not secret and file_type(path) in ("code", "text"):
                added, removed, sym_added, sym_removed, sym_changed = await self._diff_summary(
                    p, st, repo_root
                )
        else:
            self._symbols.pop(norm(path), None)
            self._lines.pop(norm(path), None)
        payload = FsChange(
            via_git=self._via_git(repo_root, pend.last_t),
            path=norm(pend.path),
            root=self._root_of(path),
            change=pend.kind,
            dest_path=norm(pend.dest) if pend.dest else None,
            size=size,
            ext=PurePosixPath(norm(path)).suffix.lower() or None,
            file_type=file_type(path),
            repo_root=repo_root,
            lines_added=added,
            lines_removed=removed,
            symbols_added=sym_added,
            symbols_removed=sym_removed,
            symbols_changed=sym_changed,
            sensitive=secret,
        )
        return Event.point(payload, pend.first_wall, observer_version=VERSION)

    async def _diff_summary(
        self, p: Path, st: os.stat_result, repo_root: str | None
    ) -> tuple[int | None, int | None, list[str], list[str], list[str]]:
        """Lines added/removed (vs HEAD in a repo, else vs the last save we saw) and Python
        symbols added/removed/changed since the last save we saw."""
        key = norm(p)
        text = await asyncio.to_thread(_read_text, p, st)
        added = removed = None
        if repo_root:
            rel = key[len(repo_root) + 1 :]
            try:
                out = await self.runner.run(
                    repo_root, "diff", "HEAD", "--numstat", "-z", "--", rel, check=False
                )
            except Exception:
                out = b""
            parts = out.decode("utf-8", "replace").split("\t")
            if len(parts) >= 2 and parts[0].isdigit():
                added, removed = int(parts[0]), int(parts[1])
            elif text is not None:
                added, removed = text.count("\n"), 0  # untracked or brand new repo
        elif text is not None:
            lines = text.count("\n")
            prev = self._lines.get(key)
            self._lines[key] = lines
            if prev is not None:
                added, removed = max(0, lines - prev), max(0, prev - lines)
        sym_added: list[str] = []
        sym_removed: list[str] = []
        sym_changed: list[str] = []
        if text is not None and p.suffix.lower() in (".py", ".pyi"):
            new = await asyncio.to_thread(python_symbols, text)
            old = self._symbols.get(key)
            if new or old:
                self._symbols[key] = new
            if old is not None:
                sym_added, sym_removed, sym_changed = diff_symbols(old, new)
        return added, removed, sym_added[:50], sym_removed[:50], sym_changed[:50]

    def _via_git(self, repo_root: str | None, t: float) -> bool:
        """Did a git operation that rewrites the worktree (pull, merge, checkout, rebase) run
        in this repo around the time of the change? Then git made the change, not you."""
        if not repo_root:
            return False
        op_t = self._git_ops.get(norm(repo_root).lower())
        return op_t is not None and abs(t - op_t) <= GIT_OP_WINDOW_S

    def _root_of(self, path: str) -> str | None:
        p = norm(path).lower()
        for r in self.roots:
            if p.startswith(norm(r).lower() + "/"):
                return norm(r)
        return None

    def _start_watcher(self) -> None:
        from watchdog.events import FileSystemEvent, FileSystemEventHandler
        from watchdog.observers import Observer

        owner = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event: FileSystemEvent) -> None:
                if event.is_directory or event.event_type not in (
                    "created",
                    "modified",
                    "deleted",
                    "moved",
                ):
                    return
                src = os.fsdecode(event.src_path)
                dest = os.fsdecode(event.dest_path) if event.event_type == "moved" else None
                owner.on_raw(event.event_type, src, dest)

        _narrow_windows_notify_flags()
        watcher = Observer()
        for root in self.roots:
            watcher.schedule(Handler(), str(root), recursive=True)
        watcher.daemon = True
        watcher.start()
        self._watcher = watcher

    def _stop_watcher(self) -> None:
        watcher = self._watcher
        if watcher is not None:
            watcher.stop()  # type: ignore[attr-defined]
            watcher.join(5)  # type: ignore[attr-defined]
            self._watcher = None

    async def stop(self) -> None:
        self._stop_watcher()
