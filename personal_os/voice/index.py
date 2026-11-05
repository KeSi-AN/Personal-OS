"""What can be opened by voice, indexed for fuzzy matching (spec §6).

- Apps: your list (actions.apps) and every Start menu app, with program-name aliases
  ("chrome" for Google Chrome, "code" for Visual Studio Code).
- Projects: their names, opening their folder.
- Folders and files: everything inside the folders actions may touch (on this machine: E:\\),
  at any depth, inside code repos too. Only dependency and build trees (node_modules, .venv,
  dist ...) and binary files (.dll, fonts, caches) are left out.

Nothing is dropped for being deep or in a repo; it ranks a little lower instead (`penalty`), so
the folder or document you mean wins over a same-named file in a project's assets. Two things
with the same name are both kept; the question then names their folders.

Built in the background and refreshed every few minutes (Windows has no inotify; the file
watcher's recent changes rank recently used files higher). Building reads names only, never
contents.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from personal_os.actions import apps as apps_mod
from personal_os.actions.paths import PathPolicy
from personal_os.actions.quick import PROGRAM_EXTS, SKIP_DIRS, _distinct
from personal_os.config.schema import Config

Kind = Literal["app", "project", "folder", "file"]
# Documents, media and the like: what people ask to open.
FILE_EXTS = (
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".csv", ".txt", ".md", ".rtf",
    ".odt", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".svg",
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".mp3", ".wav", ".m4a", ".flac", ".ogg",
)  # fmt: skip
# Never worth opening by name: binaries, fonts, caches, build output.
BINARY_EXTS = (
    ".dll", ".pyd", ".pyc", ".so", ".dylib", ".pak", ".bin", ".dat", ".woff", ".woff2", ".ttf",
    ".otf", ".eot", ".ico", ".lock", ".map", ".db", ".sqlite", ".sqlite3", ".log", ".tmp",
    ".cache", ".obj", ".o", ".class", ".jar", ".asar", ".node", ".wasm", ".ldb", ".blob",
)  # fmt: skip
# Files that are usually a project's parts (icons, configs, code), not what you mean.
PART_EXTS = (
    ".svg", ".json", ".js", ".ts", ".tsx", ".jsx", ".css", ".scss", ".html", ".xml", ".yml",
    ".yaml", ".toml", ".ini", ".cfg", ".py", ".java", ".c", ".h", ".cpp",
)  # fmt: skip
MAX_DEPTH = 16
MAX_ENTRIES = 120_000
BUILD_SECONDS = 30.0
REFRESH_S = 300
DEPTH_PENALTY = 0.012  # per folder level
REPO_PENALTY = 0.04  # inside a code repo (the repo folder itself isn't)
PART_PENALTY = 0.08  # a project part (.svg, .json, .py ...): "my resume" is the PDF
RECENT_BONUS = 0.03  # changed recently


@dataclass(frozen=True)
class Entry:
    kind: Kind
    name: str  # what it's called out loud: "Brave", "typewriter", "resume"
    target: str  # open_app's app, or the path
    key: str = ""  # normalized name
    penalty: float = 0.0  # ranks it below an equally good match (deep, in a repo, a part)
    where: str = ""  # the folder it's in, to tell same-named things apart

    @property
    def is_app(self) -> bool:
        return self.kind == "app"


def norm(text: str) -> str:
    """Lower case, words only: "Antigravity IDE.exe" -> "antigravity ide exe"."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)  # camelCase -> camel case
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _entry(kind: Kind, name: str, target: str, penalty: float = 0.0, where: str = "") -> Entry:
    return Entry(kind, name, target, norm(name), penalty, where)


@dataclass
class Index:
    entries: list[Entry] = field(default_factory=list)
    built_at: float = 0.0

    def of(self, *kinds: Kind) -> list[Entry]:
        return [e for e in self.entries if e.kind in kinds]

    def names_for_hints(self, limit: int = 60) -> list[str]:
        """Names worth biasing speech recognition towards: projects and the top folders
        first (your own names, the ones recognition gets wrong), then apps."""
        out: list[str] = []
        folders = sorted(self.of("project", "folder"), key=lambda e: e.penalty)
        for e in [*folders[:30], *self.of("app")]:
            if e.name not in out and 2 < len(e.name) <= 24:
                out.append(e.name)
        return out[:limit]


def build(config: Config, conn: sqlite3.Connection | None = None) -> Index:
    entries: list[Entry] = []
    seen: set[tuple[str, str, str]] = set()
    recent = _recent_paths(conn) if conn is not None else set()

    def add(kind: Kind, name: str, target: str, penalty: float = 0.0, where: str = "") -> None:
        if kind in ("file", "folder") and target.lower() in recent:
            penalty -= RECENT_BONUS
        e = _entry(kind, name, target, penalty, where)
        # Same name in two places: both kept (the question names their folders).
        if e.key and (e.key, kind, target) not in seen:
            seen.add((e.key, kind, target))
            entries.append(e)

    installed = apps_mod.installed(config.runtime_dir)
    proper = {norm(a.name): a.name for a in installed}
    for key in config.actions.apps:
        spoken = key.replace("_", " ")
        add("app", proper.get(norm(spoken), spoken), key)  # said as "Brave", runs your entry
    for app in installed:
        add("app", app.name, app.name)
        if app.exe:
            stem = Path(app.exe).stem
            if norm(stem) != norm(app.name) and len(norm(stem)) >= 3:
                add("app", stem, app.name)  # "chrome" for Google Chrome
    policy = PathPolicy(config)
    if conn is not None:
        _projects(conn, policy, add)
    _walk(policy, add)
    return Index(entries, time.time())


_HASHLIKE = re.compile(r"^[0-9a-f]{8,}$|^[0-9a-f-]{32,}$")
MACHINE_DIRS = frozenset({"win-unpacked", "mac-unpacked", "linux-unpacked", "_", "out"})


def _machine_made(lower: str) -> bool:
    """Folder names nobody says: content hashes, unpacked builds."""
    return lower in MACHINE_DIRS or bool(_HASHLIKE.match(lower))


def _app_install(folder: Path) -> bool:
    """An installed program: a program file next to its runtime (resources, DLLs)."""
    try:
        names = [n.lower() for n in os.listdir(folder)]
    except OSError:
        return False
    if not any(n.endswith(".exe") for n in names):
        return False
    return "resources" in names or sum(n.endswith(".dll") for n in names) >= 2


def _recent_paths(conn: sqlite3.Connection) -> set[str]:
    """Files (and their folders) the file watcher saw change recently, lower-cased."""
    out: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT payload FROM events WHERE source = 'fs' ORDER BY ts_start DESC LIMIT 2000"
        ).fetchall()
    except sqlite3.Error:
        return out
    for (payload,) in rows:
        path = str(json.loads(payload).get("path") or "")
        if path:
            p = Path(path)
            out.add(str(p).lower())
            out.add(str(p.parent).lower())
    return out


def _projects(conn: sqlite3.Connection, policy: PathPolicy, add: object) -> None:
    from personal_os.actions.base import ActionError
    from personal_os.projects.registry import NONE_PROJECT, load_projects

    for p in load_projects(conn):
        if p.id == NONE_PROJECT or not p.roots:
            continue
        try:
            root = policy.check(p.roots[0], must_exist=True)
        except ActionError:
            continue
        for name in {p.id, p.name}:
            add("project", name, str(root))  # type: ignore[operator]


def _walk(policy: PathPolicy, add: object) -> None:
    roots = [
        r for r in _distinct(policy.roots) if r not in (policy.notes_dir, policy.screenshots_dir)
    ]
    # (folder, depth, inside a repo?)
    queue: deque[tuple[Path, int, bool]] = deque((r, 0, False) for r in roots)
    count = 0
    deadline = time.monotonic() + BUILD_SECONDS
    while queue and count < MAX_ENTRIES and time.monotonic() < deadline:
        folder, depth, in_repo = queue.popleft()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        where = folder.name or str(folder)
        base = DEPTH_PENALTY * depth + (REPO_PENALTY if in_repo else 0.0)
        for e in entries:
            count += 1
            lower = e.name.lower()
            try:
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if lower in SKIP_DIRS or lower.startswith((".", "$", "~")) or _machine_made(lower):
                    continue
                add("folder", e.name, e.path, base, where)  # type: ignore[operator]
                # An installed program (VS Code, Macro Deck ...) opens as a folder; its own
                # files (extensions, locales, icons) aren't yours and would crowd out what is.
                if depth + 1 < MAX_DEPTH and not _app_install(Path(e.path)):
                    repo = in_repo or (Path(e.path) / ".git").exists()
                    queue.append((Path(e.path), depth + 1, repo))
                continue
            if lower.endswith(BINARY_EXTS) or lower.endswith(PROGRAM_EXTS):
                continue
            if policy._secret(Path(e.path)):
                continue
            penalty = base + (PART_PENALTY if lower.endswith(PART_EXTS) else 0.0)
            stem = Path(e.name).stem
            if stem and stem != e.name:
                add("file", stem, e.path, penalty, where)  # type: ignore[operator]
            add("file", e.name, e.path, penalty, where)  # type: ignore[operator]


class LiveIndex:
    """An index kept fresh in a background thread; `current()` never blocks on a rebuild."""

    def __init__(self, config: Config, db_path: Path | None = None) -> None:
        self.config = config
        self.db_path = db_path
        self._index = Index()
        self._lock = threading.Lock()
        self._building = False

    def current(self) -> Index:
        with self._lock:
            index = self._index
        if time.time() - index.built_at > REFRESH_S:
            self.refresh()
        return index

    def refresh(self, *, wait: bool = False) -> None:
        with self._lock:
            if self._building:
                return
            self._building = True
        thread = threading.Thread(target=self._build, name="pos-index", daemon=True)
        thread.start()
        if wait:
            thread.join()

    def _build(self) -> None:
        conn = None
        try:
            if self.db_path is not None:
                from personal_os.storage.db import connect

                conn = connect(self.db_path)
            try:
                index = build(self.config, conn)
            except Exception as exc:  # keep the last index; try again at the next refresh
                from personal_os.core.logging import get_logger

                get_logger("voice.index").warning(
                    "index build failed", extra={"fields": {"exc_type": type(exc).__name__}}
                )
                with self._lock:
                    self._index = Index(self._index.entries, time.time())
                return
            with self._lock:
                self._index = index
        finally:
            if conn is not None:
                conn.close()
            with self._lock:
                self._building = False
