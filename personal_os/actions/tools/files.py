"""Tier 2 file tools (confirm every time) and tier 3 delete (blocked unless unlocked)."""

from __future__ import annotations

import difflib
import shutil
from pathlib import Path

from pydantic import Field

from personal_os.actions.base import ActionError, Args, Result, Tier, Tool, ToolContext
from personal_os.actions.tools.common import sha256

TEXT_MAX = 200_000


class CreateArgs(Args):
    path: str
    content: str = Field(max_length=TEXT_MAX)


class CreateFile(Tool):
    name = "create_file"
    summary = "create a new text file (never overwrites)"
    tier = Tier.CONFIRM
    reversible = True
    args_model = CreateArgs

    def preview(self, args: CreateArgs, ctx: ToolContext) -> str:
        path = ctx.paths.check(args.path)
        if path.exists():
            raise ActionError(f"{path} already exists; create_file never overwrites")
        if not path.parent.is_dir():
            raise ActionError(f"folder {path.parent} doesn't exist")
        body = "\n".join(f"+ {line}" for line in args.content.splitlines()[:40])
        return f"create {path} ({len(args.content)} characters)\n{body}"

    def run(self, args: CreateArgs, ctx: ToolContext) -> Result:
        path = ctx.paths.check(args.path)
        with path.open("x", encoding="utf-8", newline="") as f:  # "x": fails if it appeared
            f.write(args.content)
        undo = {"tool": "remove_created", "args": {"path": str(path), "sha256": sha256(path)}}
        return Result(f"created {path}", undo=undo)


class EditArgs(Args):
    path: str
    old: str = Field(min_length=1, max_length=TEXT_MAX)
    new: str = Field(max_length=TEXT_MAX)


class EditFile(Tool):
    name = "edit_file"
    summary = "replace one exact piece of text in a file (shown as a diff first)"
    tier = Tier.CONFIRM
    reversible = True
    args_model = EditArgs

    def _texts(self, args: EditArgs, ctx: ToolContext) -> tuple[Path, str, str]:
        path = ctx.paths.check(args.path, must_exist=True)
        if not path.is_file():
            raise ActionError(f"{path} isn't a file")
        before = path.read_text(encoding="utf-8")
        count = before.count(args.old)
        if count != 1:
            raise ActionError(f"the text to replace occurs {count} times in {path.name}, not once")
        return path, before, before.replace(args.old, args.new, 1)

    def preview(self, args: EditArgs, ctx: ToolContext) -> str:
        path, before, after = self._texts(args, ctx)
        diff = difflib.unified_diff(
            before.splitlines(), after.splitlines(), str(path), str(path), lineterm="", n=2
        )
        return "\n".join(list(diff)[:80])

    def run(self, args: EditArgs, ctx: ToolContext) -> Result:
        path, before, after = self._texts(args, ctx)
        backup = ctx.undo_dir / f"{ctx.action_id}.orig"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(before, encoding="utf-8", newline="")
        path.write_text(after, encoding="utf-8", newline="")
        undo_args = {"path": str(path), "backup": backup.name, "sha256": sha256(path)}
        undo = {"tool": "restore_file", "args": undo_args}
        return Result(f"edited {path}", undo=undo)


class RestoreArgs(Args):
    path: str
    backup: str
    sha256: str


class RestoreFile(Tool):
    """Undo for edit_file: put the original back, only if nobody changed the file since."""

    name = "restore_file"
    summary = "(undo) restore a file edited by this assistant, if unchanged since"
    tier = Tier.CONFIRM
    args_model = RestoreArgs
    internal = True

    def _backup(self, args: RestoreArgs, ctx: ToolContext) -> Path:
        backup = ctx.undo_dir / Path(args.backup).name
        if not backup.is_file():
            raise ActionError("the original is gone (kept for actions.undo_keep_days)")
        return backup

    def preview(self, args: RestoreArgs, ctx: ToolContext) -> str:
        path = ctx.paths.check(args.path, must_exist=True)
        self._backup(args, ctx)
        if sha256(path) != args.sha256:
            raise ActionError(f"{path.name} changed after the edit; restore it by hand")
        return f"restore {path} to its content before the edit"

    def run(self, args: RestoreArgs, ctx: ToolContext) -> Result:
        path = ctx.paths.check(args.path, must_exist=True)
        backup = self._backup(args, ctx)
        path.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8", newline="")
        backup.unlink()
        return Result(f"restored {path}")


class MoveArgs(Args):
    source: str
    destination: str


class MovePath(Tool):
    name = "move_path"
    summary = "move or rename a file or folder (never overwrites)"
    tier = Tier.CONFIRM
    reversible = True
    args_model = MoveArgs

    def _paths(self, args: MoveArgs, ctx: ToolContext) -> tuple[Path, Path]:
        src = ctx.paths.check(args.source, must_exist=True)
        dst = ctx.paths.check(args.destination)
        if dst.exists():
            raise ActionError(f"{dst} already exists; move_path never overwrites")
        if not dst.parent.is_dir():
            raise ActionError(f"folder {dst.parent} doesn't exist")
        return src, dst

    def preview(self, args: MoveArgs, ctx: ToolContext) -> str:
        src, dst = self._paths(args, ctx)
        return f"move {src}\n  -> {dst}"

    def run(self, args: MoveArgs, ctx: ToolContext) -> Result:
        src, dst = self._paths(args, ctx)
        shutil.move(str(src), str(dst))
        undo = {"tool": "move_path", "args": {"source": str(dst), "destination": str(src)}}
        return Result(f"moved to {dst}", undo=undo)


class DeleteArgs(Args):
    path: str


class DeletePath(Tool):
    name = "delete_path"
    summary = "move a file or folder to the Recycle Bin"
    tier = Tier.BLOCKED
    args_model = DeleteArgs

    def preview(self, args: DeleteArgs, ctx: ToolContext) -> str:
        path = ctx.paths.check(args.path, must_exist=True)
        what = "folder" if path.is_dir() else "file"
        return f"move the {what} {path} to the Recycle Bin (restore it from there)"

    def run(self, args: DeleteArgs, ctx: ToolContext) -> Result:
        path = ctx.paths.check(args.path, must_exist=True)
        ctx.desktop.recycle(path)
        return Result(f"moved {path} to the Recycle Bin")
