"""Tier 0: read-only tools. Their results are captured content, so they taint what follows."""

from __future__ import annotations

import shutil
from datetime import timedelta

import psutil
from pydantic import Field

from personal_os.actions.base import ActionError, Args, Result, Tier, Tool, ToolContext
from personal_os.context.evidence import fetch_by_ids, finalize, lines_from_rows
from personal_os.context.retrieval import hybrid_search
from personal_os.core import runtime
from personal_os.core.clock import to_ms


class SearchArgs(Args):
    query: str = Field(min_length=1, max_length=300)
    project: str | None = None
    days: int = Field(30, ge=1, le=365)


class SearchContext(Tool):
    name = "search_context"
    summary = "search what you've done (pages, commands, files, commits, summaries)"
    tier = Tier.READ
    args_model = SearchArgs

    def preview(self, args: SearchArgs, ctx: ToolContext) -> str:
        scope = f" in {args.project}" if args.project else ""
        return f"search your activity for '{args.query}'{scope}, last {args.days} days"

    def run(self, args: SearchArgs, ctx: ToolContext) -> Result:
        now = ctx.clock.now()
        res = hybrid_search(
            ctx.conn, ctx.provider, args.query, start_ms=to_ms(now - timedelta(days=args.days)),
            end_ms=to_ms(now), project_id=args.project, include_summaries=False, limit=10,
        )  # fmt: skip
        pack = finalize(lines_from_rows(fetch_by_ids(ctx.conn, [h.id for h in res.hits])), 10)
        text = pack.render() or "(nothing found)"
        return Result(f"{len(pack.lines)} item(s) found", data=text, captured=True)


class ReadFileArgs(Args):
    path: str
    max_chars: int = Field(4000, ge=100, le=20000)


class ReadFile(Tool):
    name = "read_file"
    summary = "read a text file inside your folders"
    tier = Tier.READ
    args_model = ReadFileArgs

    def preview(self, args: ReadFileArgs, ctx: ToolContext) -> str:
        path = ctx.paths.check(args.path, must_exist=True)
        if not path.is_file():
            raise ActionError(f"{path} isn't a file")
        return f"read {path} (first {args.max_chars} characters)"

    def run(self, args: ReadFileArgs, ctx: ToolContext) -> Result:
        path = ctx.paths.check(args.path, must_exist=True)
        raw = path.read_bytes()[: args.max_chars * 4]
        if b"\x00" in raw[:4096]:
            raise ActionError(f"{path.name} looks binary")
        text = raw.decode("utf-8", errors="replace")[: args.max_chars]
        return Result(f"read {len(text)} characters of {path.name}", data=text, captured=True)


class ListArgs(Args):
    path: str


class ListFolder(Tool):
    name = "list_folder"
    summary = "list a folder inside your folders"
    tier = Tier.READ
    args_model = ListArgs

    def preview(self, args: ListArgs, ctx: ToolContext) -> str:
        path = ctx.paths.check(args.path, must_exist=True)
        if not path.is_dir():
            raise ActionError(f"{path} isn't a folder")
        return f"list {path}"

    def run(self, args: ListArgs, ctx: ToolContext) -> Result:
        path = ctx.paths.check(args.path, must_exist=True)
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:200]
        lines = [f"{p.name}{'/' if p.is_dir() else ''}" for p in entries]
        return Result(f"{len(lines)} entries", data="\n".join(lines), captured=True)


class StatusArgs(Args):
    pass


class SystemStatus(Tool):
    name = "system_status"
    summary = "CPU, memory, battery, disk and whether capture is running"
    tier = Tier.READ
    args_model = StatusArgs

    def preview(self, args: StatusArgs, ctx: ToolContext) -> str:
        return "read CPU, memory, battery, disk and daemon status"

    def run(self, args: StatusArgs, ctx: ToolContext) -> Result:
        mem = psutil.virtual_memory()
        disk = shutil.disk_usage(ctx.config.data_dir)
        battery = psutil.sensors_battery()
        info = runtime.daemon_info(ctx.config.runtime_dir)
        parts = [
            f"CPU {psutil.cpu_percent(0.3):.0f}%",
            f"memory {mem.percent:.0f}% used",
            f"disk {disk.free / 1e9:.0f} GB free",
            "battery n/a" if battery is None else
            f"battery {battery.percent:.0f}%{' (charging)' if battery.power_plugged else ''}",
            f"capture {'running' if info.running else 'not running'}",
        ]  # fmt: skip
        text = ", ".join(parts)
        return Result(text, data=text)
