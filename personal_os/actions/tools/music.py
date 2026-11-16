"""Your music, from the source you chose (actions/music.py, ADR-084).

    music   play, pause, next, previous on your music source only; "play" with nothing
            open starts it                                                      tier 1

A song by name is play_media; what's playing is whats_playing. Both follow the same source.
"""

from __future__ import annotations

from typing import Literal

from personal_os.actions import music
from personal_os.actions.base import ActionError, Args, Result, Tier, Tool, ToolContext


def backend(ctx: ToolContext) -> music.LiveBackend:
    return music.LiveBackend(
        ctx.config.runtime_dir, ctx.config.actions.browser, launch=ctx.desktop.launch,
        cancel=ctx.cancel,
    )  # fmt: skip


def source(ctx: ToolContext) -> music.Source:
    """The source you chose; YouTube Music stands in for "any" when something must be started."""
    return music.chosen(ctx.config.music.source) or music.SOURCES["youtube_music"]


class MusicArgs(Args):
    action: Literal["play", "pause", "toggle", "next", "previous"]


class MusicControl(Tool):
    name = "music"
    summary = (
        "the user's music, from their music app or site only: play (with nothing open, starts "
        "it), pause, toggle, next, previous; for a song by name use play_media"
    )
    tier = Tier.LOW
    args_model = MusicArgs

    def preview(self, args: MusicArgs, ctx: ToolContext) -> str:
        return f"{args.action} {source(ctx).name}"

    def run(self, args: MusicArgs, ctx: ToolContext) -> Result:
        cfg = ctx.config.music
        reply = music.control(
            source(ctx), backend(ctx), args.action, open_in=cfg.open_in,
            default_query=cfg.default_query,
        )  # fmt: skip
        if not reply.ok:
            raise ActionError(reply.text.rstrip("."))
        return Result(reply.text)
