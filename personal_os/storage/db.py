"""SQLite connections and migrations (DESIGN.md §5, ADR-009).

Connections run in autocommit mode (isolation_level=None); code that writes opens its own
`BEGIN IMMEDIATE … COMMIT`, so transaction boundaries are always explicit.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from personal_os.core.logging import get_logger

_log = get_logger("storage")
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def connect(path: Path | str, *, readonly: bool = False) -> sqlite3.Connection:
    target = str(path)
    if readonly:
        conn = sqlite3.connect(
            f"file:{Path(target).as_posix()}?mode=ro",
            uri=True,
            isolation_level=None,
            check_same_thread=False,
        )
    else:
        if target != ":memory:":
            Path(target).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(target, isolation_level=None, check_same_thread=False, timeout=5)
        # Must precede the first table; a no-op on an existing database.
        conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA secure_delete = ON")  # deleted rows are zeroed on disk (forget)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def available_migrations() -> list[Migration]:
    pkg = resources.files("personal_os.storage.migrations")
    found: list[Migration] = []
    for entry in pkg.iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            found.append(Migration(int(m.group(1)), entry.name, entry.read_text("utf-8")))
    found.sort(key=lambda mig: mig.version)
    for expected, mig in enumerate(found, start=1):
        if mig.version != expected:
            raise RuntimeError(f"migration numbering gap at {mig.name}")
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, db_path: Path | None = None) -> list[int]:
    """Apply pending migrations in order, each in its own transaction. Returns applied versions.

    For an existing database a backup (VACUUM INTO) is taken first and deleted again once all
    migrations and an integrity check pass. Leaving old copies around would keep data that
    `forget` later removes, so the backup only survives a failed migration.
    """
    current = schema_version(conn)
    pending = [m for m in available_migrations() if m.version > current]
    if not pending:
        return []
    backup: Path | None = None
    if current > 0 and db_path is not None:
        backup = db_path.with_name(f"{db_path.name}.pre-{pending[0].version:04d}.bak")
        backup.unlink(missing_ok=True)
        conn.execute("VACUUM INTO ?", (str(backup),))
    applied: list[int] = []
    for mig in pending:
        try:
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{mig.sql}\nPRAGMA user_version = {mig.version};\nCOMMIT;"
            )
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            _log.error("migration failed", extra={"fields": {"migration": mig.name}})
            raise
        applied.append(mig.version)
    problems = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if problems != "ok":
        raise RuntimeError(f"integrity check failed after migration: {problems}")
    if backup is not None:
        backup.unlink(missing_ok=True)
    return applied


def open_db(path: Path | str) -> sqlite3.Connection:
    """Connect and bring the schema up to date."""
    conn = connect(path)
    migrate(conn, Path(path) if str(path) != ":memory:" else None)
    return conn
