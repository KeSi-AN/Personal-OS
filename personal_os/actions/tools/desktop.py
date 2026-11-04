"""Tier 1: low-risk, reversible desktop actions."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field

from personal_os.actions.base import ActionError, Args, Result, Tier, Tool, ToolContext
from personal_os.actions.desktop import MEDIA_KEYS, WINDOW_ACTIONS
from personal_os.actions.tools.common import sha256
from personal_os.privacy.rules import PrivacyRules

URL_MAX = 2048


class PathArgs(Args):
    path: str


class OpenPath(Tool):
    name = "open_path"
    summary = "open a file or folder with its default app"
    tier = Tier.LOW
    reversible = True
    args_model = PathArgs

    def preview(self, args: PathArgs, ctx: ToolContext) -> str:
        path = ctx.paths.check(ctx.paths.locate(args.path), must_exist=True)
        if path.suffix.lower() in {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".msi", ".lnk", ".js"}:
            raise ActionError(f"{path.name} is a program or script; open_path only opens documents")
        return f"open {path}"

    def run(self, args: PathArgs, ctx: ToolContext) -> Result:
        path = ctx.paths.check(ctx.paths.locate(args.path), must_exist=True)
        ctx.desktop.open_path(path)
        return Result(f"Opened {path.name or path}")


class UrlArgs(Args):
    url: str = Field(max_length=URL_MAX)


class OpenUrl(Tool):
    name = "open_url"
    summary = "open a web page (http/https) in your default browser"
    tier = Tier.LOW
    reversible = True
    network = True
    args_model = UrlArgs

    def preview(self, args: UrlArgs, ctx: ToolContext) -> str:
        parts = urlsplit(args.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ActionError("only http and https links can be opened")
        rules = PrivacyRules.from_config(ctx.config.privacy)
        if rules.is_blocked_domain(parts.hostname):
            raise ActionError(f"{parts.hostname} is on your blocked list")
        return f"open {args.url}"

    def run(self, args: UrlArgs, ctx: ToolContext) -> Result:
        ctx.desktop.open_url(args.url)
        return Result(f"opened {args.url}")


def _program_exists(program: str) -> bool:
    """A full path must exist; a bare name ("wt.exe") must be on PATH."""
    if os.path.dirname(program):
        return Path(program).is_file()
    return shutil.which(program) is not None


class AppArgs(Args):
    app: str
    target: str | None = None


class OpenApp(Tool):
    name = "open_app"
    summary = "start an app from your list, optionally with a file or folder"
    tier = Tier.LOW
    reversible = True
    args_model = AppArgs

    def _argv(self, args: AppArgs, ctx: ToolContext) -> list[str]:
        apps = {k.lower(): v for k, v in ctx.config.actions.apps.items()}
        spec = apps.get(args.app.lower())
        if not spec:
            raise ActionError(f"'{args.app}' isn't in your app list ({', '.join(apps)})")
        program = os.path.expandvars(spec[0])
        if not _program_exists(program):
            raise ActionError(
                f"{args.app} isn't installed at {program}. Set where it is with: personal-os "
                f"config set actions.apps.{args.app.lower()} '[\"C:/path/to/app.exe\"]'"
            )
        argv = [program, *spec[1:]]
        if args.target:
            argv.append(str(ctx.paths.check(ctx.paths.locate(args.target), must_exist=True)))
        return argv

    def preview(self, args: AppArgs, ctx: ToolContext) -> str:
        return "start " + " ".join(f'"{a}"' if " " in a else a for a in self._argv(args, ctx))

    def run(self, args: AppArgs, ctx: ToolContext) -> Result:
        pid = ctx.desktop.launch(self._argv(args, ctx))
        if args.target:
            name = Path(args.target.rstrip("/\\")).name or args.target
            return Result(f"Opened {name} in {args.app}", data=f"pid {pid}")
        return Result(f"Started {args.app}", data=f"pid {pid}")


class NoteArgs(Args):
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(max_length=20000)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "note"


class CreateNote(Tool):
    name = "create_note"
    summary = "save a short Markdown note in your notes folder"
    tier = Tier.LOW
    reversible = True
    args_model = NoteArgs

    def _path(self, args: NoteArgs, ctx: ToolContext) -> Path:
        stamp = ctx.clock.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
        return ctx.paths.notes_dir / f"{stamp}-{_slug(args.title)}.md"

    def preview(self, args: NoteArgs, ctx: ToolContext) -> str:
        return f"write {self._path(args, ctx)}:\n# {args.title}\n{args.text[:300]}"

    def run(self, args: NoteArgs, ctx: ToolContext) -> Result:
        path = self._path(args, ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {args.title}\n\n{args.text}\n", encoding="utf-8")
        undo = {"tool": "remove_created", "args": {"path": str(path), "sha256": sha256(path)}}
        return Result(f"saved {path}", undo=undo)


class ClipArgs(Args):
    text: str = Field(max_length=100_000)


class CopyToClipboard(Tool):
    name = "copy_to_clipboard"
    summary = "put text on the clipboard"
    tier = Tier.LOW
    reversible = True
    args_model = ClipArgs

    def preview(self, args: ClipArgs, ctx: ToolContext) -> str:
        return f"copy {len(args.text)} characters: {args.text[:120]}"

    def run(self, args: ClipArgs, ctx: ToolContext) -> Result:
        before = ctx.desktop.get_clipboard()
        ctx.desktop.set_clipboard(args.text)
        undo = (
            {"tool": "copy_to_clipboard", "args": {"text": before}} if before is not None else None
        )
        return Result("copied to the clipboard", undo=undo)


class MediaArgs(Args):
    action: Literal["play_pause", "next", "previous", "mute", "volume_up", "volume_down"]
    times: int = Field(1, ge=1, le=10)


INVERSE = {"play_pause": "play_pause", "next": "previous", "previous": "next", "mute": "mute",
           "volume_up": "volume_down", "volume_down": "volume_up"}  # fmt: skip


class Media(Tool):
    name = "media"
    summary = "music and sound: play/pause, next or previous track, mute, sound volume up/down"
    tier = Tier.LOW
    reversible = True
    args_model = MediaArgs

    def preview(self, args: MediaArgs, ctx: ToolContext) -> str:
        return f"press the {args.action} key" + (f" {args.times} times" if args.times > 1 else "")

    def run(self, args: MediaArgs, ctx: ToolContext) -> Result:
        assert args.action in MEDIA_KEYS
        for _ in range(args.times):
            ctx.desktop.media_key(args.action)
        undo = {"tool": "media", "args": {"action": INVERSE[args.action], "times": args.times}}
        return Result(f"pressed {args.action}", undo=undo)


class ShotArgs(Args):
    pass


class Screenshot(Tool):
    name = "screenshot"
    summary = "save a screenshot of all screens to your screenshots folder"
    tier = Tier.LOW
    reversible = True
    args_model = ShotArgs

    def _path(self, ctx: ToolContext) -> Path:
        stamp = ctx.clock.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
        return ctx.paths.screenshots_dir / f"screenshot-{stamp}.png"

    def preview(self, args: ShotArgs, ctx: ToolContext) -> str:
        return f"save {self._path(ctx)}"

    def run(self, args: ShotArgs, ctx: ToolContext) -> Result:
        path = self._path(ctx)
        ctx.desktop.screenshot(path)
        undo = {"tool": "remove_created", "args": {"path": str(path), "sha256": sha256(path)}}
        return Result(f"saved {path}", undo=undo)


class WindowArgs(Args):
    title: str = Field(min_length=2, max_length=200)
    action: Literal["focus", "minimize", "maximize", "restore"]


class Window(Tool):
    name = "window"
    summary = "focus, minimize, maximize or restore a window by part of its title"
    tier = Tier.LOW
    reversible = True
    args_model = WindowArgs

    def _find(self, args: WindowArgs, ctx: ToolContext) -> tuple[int, str, str]:
        matches = ctx.desktop.windows(args.title)
        if not matches:
            raise ActionError(f"no open window has '{args.title}' in its title")
        if len(matches) > 1:
            titles = "; ".join(w.title[:50] for w in matches[:4])
            raise ActionError(f"{len(matches)} windows match '{args.title}': {titles}")
        w = matches[0]
        return w.handle, w.title, w.state

    def preview(self, args: WindowArgs, ctx: ToolContext) -> str:
        assert args.action in WINDOW_ACTIONS
        _, title, _ = self._find(args, ctx)
        return f"{args.action} '{title}'"

    def run(self, args: WindowArgs, ctx: ToolContext) -> Result:
        handle, title, state = self._find(args, ctx)
        ctx.desktop.set_window(handle, args.action)
        back = {"minimized": "minimize", "maximized": "maximize"}.get(state, "restore")
        undo = None if back == args.action else {
            "tool": "window", "args": {"title": args.title, "action": back}}  # fmt: skip
        return Result(f"{args.action}: {title}", undo=undo)


class RemoveArgs(Args):
    path: str
    sha256: str


class RemoveCreated(Tool):
    """Undo for things the assistant created: only if the file is exactly as it was made."""

    name = "remove_created"
    summary = "(undo) remove a note or screenshot this assistant created, if unchanged"
    tier = Tier.LOW
    args_model = RemoveArgs
    internal = True

    def preview(self, args: RemoveArgs, ctx: ToolContext) -> str:
        path = ctx.paths.check(args.path, must_exist=True)
        if sha256(path) != args.sha256:
            raise ActionError(f"{path.name} changed since it was created; not removing it")
        return f"move {path} to the Recycle Bin"

    def run(self, args: RemoveArgs, ctx: ToolContext) -> Result:
        path = ctx.paths.check(args.path, must_exist=True)
        ctx.desktop.recycle(path)
        return Result(f"moved {path.name} to the Recycle Bin")
