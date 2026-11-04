"""`do "open X"` and `do "open X in Y"` without the model.

Opening something is the most common request and the easiest to get wrong with a small model
(no call planned, a runaway reply cut off mid-JSON). The request is parsed by rules instead:

- X is an app installed on this PC ("open calculator", "open claude"), a project ("open
  personal-os"), or a file or folder found by name inside your allowed folders ("open no.pdf",
  "open typewriter folder").
- Y, if given, is the app to open it in ("in vscode", "with file explorer").

Found files open with their default app: PDFs, images, videos and documents alike. Anything the
rules can't settle goes to the model as before. The executor still checks every call, so this
path can't open more than the model could.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from personal_os.actions.base import ActionError
from personal_os.actions.paths import PathPolicy
from personal_os.actions.tools.desktop import known_app
from personal_os.observers.gitutil import IGNORED_DIR_NAMES
from personal_os.projects.registry import NONE_PROJECT, load_projects

_OPEN = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would|will) you\s+(?:please\s+)?)?"
    r"(?:open|launch|start|show|run)(?:\s+up)?\s+(?:me\s+)?(?P<rest>.+?)\s*[.!?]*\s*$",
    re.I,
)
_IN_APP = re.compile(
    r"^(?P<target>.+?)\s+(?:in|with|using|on)\s+(?:the\s+|my\s+)?(?P<app>[\w .+&-]+?)"
    r"(?:\s+app)?$",
    re.I,
)
_LEAD = re.compile(r"^(?:the|my|a|an|this|that)\s+", re.I)
_KIND = re.compile(
    r"\s+(folder|directory|dir|file|project|repo|document|doc|pdf|image|picture|photo|video)$",
    re.I,
)
_KIND_FIRST = re.compile(
    r"^(folder|directory|file|project|repo|document|pdf|image|picture|photo|video)\s+", re.I
)
_URLISH = re.compile(r"^(https?://|www\.)|\.(com|org|net|io|ai|dev|in|co)(/|$)", re.I)
FILE_KINDS = {"file", "document", "doc", "pdf", "image", "picture", "photo", "video"}
DIR_KINDS = {"folder", "directory", "dir", "project", "repo"}

# Never searched: huge or system trees where nothing you'd ask to open lives.
SKIP_DIRS = frozenset(
    {
        *IGNORED_DIR_NAMES,
        "$recycle.bin",
        "system volume information",
        "config.msi",
        "windows",
        "program files",
        "program files (x86)",
        "programdata",
        "appdata",
        "anaconda3",
        "miniconda3",
        "xampp",
        "blobs",
        "manifests",
    }
)
PROGRAM_EXTS = (".exe", ".bat", ".cmd", ".ps1", ".vbs", ".msi", ".lnk", ".js", ".com", ".scr")
SEARCH_DEPTH = 4
SEARCH_ENTRIES = 40_000
SEARCH_SECONDS = 2.5


@dataclass
class Quick:
    """What the rules decided: calls to run, or a message to say instead."""

    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    message: str = ""
    # Nothing was found: let the model try, and say `message` only if it can't do it either.
    fallback: bool = False


def _key(name: str) -> str:
    """Name compared without case, spaces or punctuation: "No PDF" == "no.pdf"."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_path(
    policy: PathPolicy, name: str, want: str | None = None, within: Path | None = None
) -> Path | None:
    """A file or folder called `name` inside the allowed folders (or `within` one of them),
    nearest first.

    Matches the full name ("no.pdf"), the name without its extension ("no"), or either with
    spaces and punctuation ignored ("no pdf"). Several equally near matches raise ActionError
    listing them, so a guess never opens the wrong thing.
    """
    raw = name.strip().strip("'\"")
    if not raw:
        return None
    given = Path(os.path.expandvars(raw)).expanduser()
    if given.is_absolute() or given.drive:
        return given if given.exists() else None
    if within is None:
        try:
            direct = Path(policy.locate(raw))
        except ActionError:
            direct = None
        if direct is not None and direct.is_absolute() and direct.exists() and _fits(direct, want):
            return direct
    wanted = _key(raw)
    if not wanted:
        return None
    best: list[tuple[int, Path]] = []
    seen = 0
    deadline = time.monotonic() + SEARCH_SECONDS
    roots = [within] if within is not None else _distinct(policy.roots)
    queue: deque[tuple[Path, int]] = deque((root, 0) for root in roots)
    while queue and seen < SEARCH_ENTRIES and time.monotonic() < deadline:
        folder, depth = queue.popleft()
        if best and depth > best[0][0]:
            break  # breadth-first: nothing nearer is left
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            seen += 1
            lower = entry.name.lower()
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            stem = lower.rsplit(".", 1)[0] if not is_dir and "." in lower else lower
            if not is_dir and lower.endswith(PROGRAM_EXTS):
                continue  # never opened as a file: "open powershell" means the app
            if wanted in (_key(lower), _key(stem)) and _fits(Path(entry.path), want, is_dir):
                best.append((depth, Path(entry.path)))
            if is_dir and depth + 1 < SEARCH_DEPTH and not _skip(lower):
                queue.append((Path(entry.path), depth + 1))
    if not best:
        return None
    nearest = [p for d, p in best if d == best[0][0]]
    if len(nearest) > 1:
        shown = ", ".join(str(p) for p in nearest[:5])
        where = nearest[-1].parent.name or str(nearest[-1].parent)
        raise ActionError(
            f"there's more than one '{raw}' ({shown}); say which folder, "
            f"e.g. 'open {raw} in {where}'"
        )
    return nearest[0]


def _distinct(roots: list[Path]) -> list[Path]:
    """Roots without ones inside another (E:/ already covers E:/Personal-OS)."""
    out: list[Path] = []
    for r in sorted(set(roots), key=lambda p: len(p.parts)):
        if not any(r == o or o in r.parents for o in out):
            out.append(r)
    return out


def _skip(lower: str) -> bool:
    return lower in SKIP_DIRS or lower.startswith((".", "$", "~"))


def _fits(path: Path, want: str | None, is_dir: bool | None = None) -> bool:
    if want is None:
        return True
    d = path.is_dir() if is_dir is None else is_dir
    return d if want == "dir" else not d


def _split_kind(target: str) -> tuple[str, str | None]:
    """ "typewriter folder" -> ("typewriter", "dir"); "the pdf no.pdf" -> ("no.pdf", "file")."""
    t = _LEAD.sub("", target.strip())
    kind = None
    m = _KIND.search(t) or _KIND_FIRST.search(t)
    if m:
        word = m[1].lower()
        kind = "dir" if word in DIR_KINDS else "file"
        t = (t[: m.start()] if m.re is _KIND else t[m.end() :]).strip()
        t = _LEAD.sub("", t)
    return t, kind


def _project_root(conn: sqlite3.Connection, name: str) -> Path | None:
    k = _key(name)
    for p in load_projects(conn):
        if p.id != NONE_PROJECT and p.roots and k in (_key(p.id), _key(p.name)):
            root = Path(p.roots[0])
            return root if root.exists() else None
    return None


def seen_app(conn: sqlite3.Connection, name: str) -> tuple[str, str] | None:
    """(app name, program path) of an app you've used that isn't in your list, for a hint."""
    k = _key(name)
    if len(k) < 3:
        return None
    rows = conn.execute(
        "SELECT DISTINCT json_extract(payload, '$.app'), json_extract(payload, '$.exe_path') "
        "FROM events WHERE source = 'window' AND type = 'focus'"
    ).fetchall()
    for app, exe in rows:
        if app and exe and (k in _key(app) or k == _key(Path(exe).stem)):
            return str(app), str(exe)
    return None


def plan_open(request: str, policy: PathPolicy, conn: sqlite3.Connection) -> Quick | None:
    """Calls for an "open ..." request, a message when it can't be done, or None when the rules
    don't cover it (the model plans it instead)."""
    m = _OPEN.match(request)
    if not m:
        return None
    rest = m["rest"].strip()
    app_key: str | None = None
    within: Path | None = None
    target = rest
    split = _IN_APP.match(rest)
    if split:
        try:
            app_key = known_app(split["app"], policy.config)
        except ActionError as exc:
            return Quick(message=f"I didn't open anything: {exc}.")
        if app_key is not None:
            target = split["target"]
        elif (folder := _folder_named(policy, conn, split["app"])) is not None:
            target, within = split["target"], folder  # "open no.pdf in downloads"
        elif not _URLISH.search(split["app"]) and (seen := seen_app(conn, split["app"])):
            return _add_app_hint(*seen, split["app"])
    cfg = policy.config
    try:
        whole = _LEAD.sub("", target.strip())
        whole_app = known_app(whole, cfg, fuzzy=False) if app_key is None and not within else None
        if whole_app is not None:
            name, kind = whole, None  # "file explorer" is an app, not a file called "explorer"
        else:
            name, kind = _split_kind(target)
        if not name or _URLISH.search(name):
            return None  # nothing left, or a web address: the model plans open_url
        own_app = whole_app or (
            known_app(name, cfg, fuzzy=False) if app_key is None and kind is None else None
        )
        if own_app is not None and within is None:
            # "open antigravity" is the app even with an E:\Antigravity folder; say "antigravity
            # folder" for the folder.
            return Quick([("open_app", {"app": own_app})])
        path = _project_root(conn, name) if kind in (None, "dir") and within is None else None
        path = path or find_path(policy, name, kind, within)
        if path is None and app_key is None and kind is None and within is None:
            loose = known_app(name, cfg)  # "open powershell" -> Windows PowerShell
            if loose is not None:
                return Quick([("open_app", {"app": loose})])
    except ActionError as exc:
        return Quick(message=f"I didn't open anything: {exc}.")
    if path is not None:
        if app_key is not None:
            return Quick([("open_app", {"app": app_key, "target": str(path)})])
        return Quick([("open_path", {"path": str(path)})])
    if app_key is not None:
        return Quick(message=_not_found(name, policy), fallback=True)
    if _seen_site(conn, name) or len(name.split()) > 4:
        return None  # a website ("open youtube") or a description: the model plans it
    return Quick(message=_not_found(name, policy), fallback=True)


def _folder_named(policy: PathPolicy, conn: sqlite3.Connection, name: str) -> Path | None:
    """An allowed folder called `name`: a root ("downloads", "desktop"), a project, or a
    folder found inside the roots."""
    k = _key(_LEAD.sub("", name.strip()))
    if not k:
        return None
    for root in policy.roots:
        if k in (_key(root.name), _key(root.drive) if root.drive and not root.name else ""):
            return root  # "downloads", or "e" / "E:" for a drive
    found = _project_root(conn, name)
    if found is not None:
        return found
    try:
        return find_path(policy, name, "dir")
    except ActionError:
        return None


def _seen_site(conn: sqlite3.Connection, name: str) -> bool:
    k = _key(name)
    if len(k) < 3:
        return False
    for (domain,) in conn.execute(
        "SELECT DISTINCT json_extract(payload, '$.domain') FROM events "
        "WHERE source = 'browser' AND type = 'page_visit'"
    ):
        labels = [_key(x) for x in str(domain or "").split(".")]
        if k in labels or k == "".join(labels[-2:-1]):
            return True
    return False


def _add_app_hint(app: str, exe: str, said: str) -> Quick:
    path = exe.replace("\\", "/")
    if "/windowsapps/" in path.lower():
        # A Store app: its program path changes with every update and can't be started directly.
        return Quick(
            message=(
                f"{app} is a Microsoft Store app, which I can only start by its app ID. Find it "
                f"with PowerShell: Get-StartApps {said}, then: personal-os config set "
                f'actions.apps.{_key(said)} \'["explorer.exe", "shell:AppsFolder\\\\<AppID>"]\''
            )
        )
    return Quick(
        message=(
            f"{app} isn't in your app list yet, so I can't start it. Add it once with: "
            f"personal-os config set actions.apps.{_key(said)} '[\"{path}\"]'"
        )
    )


def _not_found(name: str, policy: PathPolicy) -> str:
    folders = ", ".join(
        str(r)
        for r in _distinct(policy.roots)
        if r not in (policy.notes_dir, policy.screenshots_dir)
    )
    return (
        f"I couldn't find '{name}' as an app, project, file or folder in your folders "
        f"({folders}). Give its full path, or add the folder to actions.allowed_paths."
    )
