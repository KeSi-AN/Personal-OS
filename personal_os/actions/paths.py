"""Which paths actions may touch (DESIGN.md §6.3).

Allowed: inside the watched folders, the notes and screenshots folders, or actions.allowed_paths,
after resolving links. Never: secret paths (the privacy filter's globs and folder names), the
data folder itself (database, logs, spool), or anything that doesn't resolve inside a root.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path, PurePath

from personal_os.actions.base import ActionError
from personal_os.config.schema import Config


class PathPolicy:
    def __init__(self, config: Config) -> None:
        self.config = config
        data = config.data_dir.resolve()
        self.notes_dir = data / "notes"
        self.screenshots_dir = data / "screenshots"
        self.data_dir = data
        roots = [*config.watched_roots, *config.actions.allowed_paths]
        self.roots = [_resolve(Path(os.path.expandvars(str(r))).expanduser()) for r in roots]
        self.roots += [self.notes_dir, self.screenshots_dir]

    def check(self, raw: str, *, must_exist: bool = False) -> Path:
        """The resolved path, or ActionError saying why it isn't allowed."""
        if not raw or "\x00" in raw:
            raise ActionError("empty or invalid path")
        path = _resolve(Path(os.path.expandvars(raw)).expanduser())
        if self._secret(path):
            raise ActionError(f"{path} looks like a secret (keys, credentials): never touched")
        inside_data = _within(path, self.data_dir)
        allowed_data = _within(path, self.notes_dir) or _within(path, self.screenshots_dir)
        if inside_data and not allowed_data:
            raise ActionError(f"{path} is inside Personal OS's own data folder")
        if not any(_within(path, root) for root in self.roots):
            shown = ", ".join(str(r) for r in self.roots)
            raise ActionError(f"{path} is outside the folders actions may touch ({shown})")
        if must_exist and not path.exists():
            raise ActionError(f"{path} doesn't exist")
        return path

    def locate(self, raw: str) -> str:
        """A bare or relative name ("newtab") that isn't in the current folder is looked up
        directly inside each allowed folder (E:/newtab). Several matches are an error, so a
        guess never picks the wrong one. Anything else is returned unchanged for check()."""
        if not raw or "\x00" in raw:
            return raw
        given = Path(os.path.expandvars(raw)).expanduser()
        if given.is_absolute() or given.drive or given.exists():
            return raw
        found: list[Path] = []
        for root in self.roots:
            candidate = _resolve(root / given)
            if candidate.exists() and candidate not in found:
                found.append(candidate)
        if len(found) > 1:
            shown = ", ".join(str(p) for p in found)
            raise ActionError(f"'{raw}' is in more than one of your folders ({shown}); say which")
        return str(found[0]) if found else raw

    def _secret(self, path: Path) -> bool:
        privacy = self.config.privacy
        parts = {p.lower() for p in path.parts}
        if any(d.lower() in parts for d in privacy.secret_dir_names):
            return True
        return any(fnmatch.fnmatch(path.name.lower(), g.lower()) for g in privacy.secret_path_globs)


def _resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def _within(path: PurePath, root: PurePath) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
