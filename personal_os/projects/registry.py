"""Projects: discovered from the git repos seen in events, or added by you (DESIGN.md §6.1)."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from personal_os.core.clock import Clock, to_ms
from personal_os.storage.db import transaction

NONE_PROJECT = "none"


def norm_path(path: str) -> str:
    """Comparison form for Windows paths: forward slashes, no trailing slash, lower case."""
    return path.replace("\\", "/").rstrip("/").lower()


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "project"


@dataclass
class Project:
    id: str
    name: str
    roots: list[str] = field(default_factory=list)
    remotes: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    source: str = "auto"
    archived: bool = False

    @property
    def norm_roots(self) -> list[str]:
        return [norm_path(r) for r in self.roots]

    @property
    def folder_names(self) -> set[str]:
        """Folder names that identify this project in window titles."""
        names = {PurePosixPath(r).name.lower() for r in self.norm_roots}
        names.add(self.name.lower())
        return {n for n in names if len(n) >= 3}


def load_projects(conn: sqlite3.Connection, *, include_archived: bool = False) -> list[Project]:
    rows = conn.execute(
        "SELECT id, name, roots, remotes, keywords, source, archived FROM projects ORDER BY id"
    ).fetchall()
    out = [
        Project(r[0], r[1], json.loads(r[2]), json.loads(r[3]), json.loads(r[4]), r[5], bool(r[6]))
        for r in rows
    ]
    return out if include_archived else [p for p in out if not p.archived]


def _unique_id(conn: sqlite3.Connection, base: str) -> str:
    candidate, n = base, 2
    while conn.execute("SELECT 1 FROM projects WHERE id = ?", (candidate,)).fetchone():
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def ensure_repo_projects(conn: sqlite3.Connection, clock: Clock) -> int:
    """Create an automatic project for every repo root seen in events that no project covers
    yet, and record repo remotes. Returns how many projects were created."""
    roots: dict[str, str] = {}
    for (root,) in conn.execute(
        "SELECT DISTINCT json_extract(payload, '$.repo_root') FROM events "
        "WHERE source IN ('git', 'fs', 'terminal') "
        "AND json_extract(payload, '$.repo_root') IS NOT NULL"
    ):
        roots.setdefault(norm_path(root), root.replace("\\", "/").rstrip("/"))
    remotes: dict[str, str] = {}
    for root, remote in conn.execute(
        "SELECT json_extract(payload, '$.repo_root'), json_extract(payload, '$.remote') "
        "FROM events "
        "WHERE source = 'git' AND type = 'state' AND json_extract(payload, '$.remote') IS NOT NULL"
    ):
        remotes[norm_path(root)] = remote
    projects = load_projects(conn, include_archived=True)
    covered = {r: p for p in projects for r in p.norm_roots}
    created = 0
    with transaction(conn):
        for key, root in sorted(roots.items()):
            project = covered.get(key)
            if project is None:
                pid = _unique_id(conn, slugify(PurePosixPath(root).name))
                conn.execute(
                    "INSERT INTO projects (id, name, roots, remotes, source, created_at) "
                    "VALUES (?, ?, ?, ?, 'auto', ?)",
                    (
                        pid,
                        PurePosixPath(root).name,
                        json.dumps([root]),
                        json.dumps([remotes[key]] if key in remotes else []),
                        to_ms(clock.now()),
                    ),
                )
                created += 1
            elif key in remotes and remotes[key] not in project.remotes:
                project.remotes.append(remotes[key])
                conn.execute(
                    "UPDATE projects SET remotes = ? WHERE id = ?",
                    (json.dumps(project.remotes), project.id),
                )
    return created


def add_project(
    conn: sqlite3.Connection, clock: Clock, name: str, roots: list[str], keywords: list[str]
) -> Project:
    with transaction(conn):
        pid = _unique_id(conn, slugify(name))
        conn.execute(
            "INSERT INTO projects (id, name, roots, keywords, source, created_at) "
            "VALUES (?, ?, ?, ?, 'manual', ?)",
            (
                pid,
                name,
                json.dumps([r.replace("\\", "/").rstrip("/") for r in roots]),
                json.dumps(keywords),
                to_ms(clock.now()),
            ),
        )
    return Project(pid, name, roots, [], keywords, "manual")


def update_project(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    name: str | None = None,
    add_keywords: list[str] | None = None,
    archived: bool | None = None,
) -> None:
    project = get_project(conn, project_id)
    if project is None:
        raise KeyError(project_id)
    with transaction(conn):
        if name is not None:
            conn.execute("UPDATE projects SET name = ? WHERE id = ?", (name, project_id))
        if add_keywords:
            merged = sorted(
                {*project.keywords, *(k.strip().lower() for k in add_keywords if k.strip())}
            )
            conn.execute(
                "UPDATE projects SET keywords = ? WHERE id = ?", (json.dumps(merged), project_id)
            )
        if archived is not None:
            conn.execute(
                "UPDATE projects SET archived = ? WHERE id = ?", (int(archived), project_id)
            )


def get_project(conn: sqlite3.Connection, project_id: str) -> Project | None:
    for p in load_projects(conn, include_archived=True):
        if p.id == project_id:
            return p
    return None


def find_project(conn: sqlite3.Connection, text: str) -> Project | None:
    """By id, or by unique case-insensitive name/id prefix (for the CLI)."""
    projects = load_projects(conn, include_archived=True)
    t = text.lower()
    exact = [p for p in projects if p.id == t or p.name.lower() == t]
    if exact:
        return exact[0]
    prefix = [p for p in projects if p.id.startswith(t) or p.name.lower().startswith(t)]
    return prefix[0] if len(prefix) == 1 else None
