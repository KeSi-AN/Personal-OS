"""Runs one action through validation, rails, policy, confirmation and the audit log (§6.3)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from pydantic import ValidationError

from personal_os.actions import killswitch
from personal_os.actions.audit import Audit, history
from personal_os.actions.base import (
    ActionError,
    Confirmer,
    Origin,
    Registry,
    Result,
    ToolContext,
)
from personal_os.actions.desktop import Desktop
from personal_os.actions.paths import PathPolicy
from personal_os.actions.policy import decide
from personal_os.config.schema import Config
from personal_os.core.clock import Clock, to_ms
from personal_os.core.ids import uuid7
from personal_os.llm.provider import LLMProvider

Status = Literal[
    "succeeded", "failed", "blocked", "declined", "cancelled", "invalid", "dry_run", "pending"
]
EXFIL_RUN = 40  # chars of earlier-read text that may not reappear in a network call


class Deferred(Exception):
    """Raised by a confirmer that asks elsewhere (the dashboard): the action waits for approval."""


class Cancelled(Exception):
    """The kill switch was engaged while the action ran."""


@dataclass
class Outcome:
    action_id: str
    tool: str
    status: Status
    message: str
    preview: str = ""
    result: Result | None = None

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


def leaks(args: dict[str, Any], sources: Sequence[str], run: int = EXFIL_RUN) -> bool:
    """True if any argument contains `run` consecutive characters of an earlier-read text.
    Whitespace is normalised on both sides: what the planner saw had it collapsed."""
    norm = [" ".join(s.split()) for s in sources]
    for value in _strings(args):
        value = " ".join(value.replace("+", " ").split())
        for i in range(0, max(0, len(value) - run + 1)):
            chunk = value[i : i + run]
            if any(chunk in s for s in norm):
                return True
    return False


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


class Executor:
    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        clock: Clock,
        registry: Registry,
        desktop: Desktop,
        confirmer: Confirmer,
        *,
        provider: LLMProvider | None = None,
    ) -> None:
        self.config = config
        self.conn = conn
        self.clock = clock
        self.registry = registry
        self.desktop = desktop
        self.confirmer = confirmer
        self.provider = provider
        self.audit = Audit(conn, clock)
        self.paths = PathPolicy(config)
        self.runtime_dir = config.runtime_dir

    def request(
        self,
        tool_name: str,
        raw_args: dict[str, Any],
        *,
        origin: Origin,
        tainted: bool = False,
        request_text: str | None = None,
        dry_run: bool = False,
        undo_of: str | None = None,
        read_so_far: Sequence[str] = (),
    ) -> Outcome:
        aid = str(uuid7(self.clock.now()))
        tool = self.registry.get(tool_name)
        if tool is None:
            return Outcome(aid, tool_name, "invalid", f"there's no tool called {tool_name!r}")
        meta: dict[str, Any] = {
            "tool": tool.name,
            "tier": int(tool.tier),
            "origin": str(origin),
            "tainted": tainted,
            "args": raw_args,
            "request": request_text,
            "undo_of": undo_of,
        }

        def log(stage: str, detail: str = "", undo: dict[str, Any] | None = None) -> None:
            self.audit.record(aid, stage, detail=detail, undo=undo, **meta)

        log("proposed")
        try:
            args = tool.args_model.model_validate(raw_args)
        except ValidationError as exc:
            msg = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'args'}: {e['msg']}" for e in exc.errors()[:3]
            )
            log("blocked", f"invalid arguments: {msg}")
            return Outcome(aid, tool.name, "invalid", f"invalid arguments for {tool.name}: {msg}")
        meta["args"] = args.model_dump(mode="json")
        # The policy first: a blocked tool or the kill switch is reported as such, before
        # its arguments are even looked at.
        now = self.clock.now()
        since = to_ms(now - timedelta(minutes=1))
        decision = decide(
            tool,
            origin,
            tainted,
            self.config.actions,
            killed=killswitch.engaged(self.runtime_dir),
            unlocked=tool.name in killswitch.unlocks(self.runtime_dir, now),
            recent=self.audit.recent_count(since),
            recent_tier2=self.audit.recent_count(since, min_tier=2),
        )
        if decision.verdict == "block":
            log("blocked", decision.reason)
            return Outcome(aid, tool.name, "blocked", decision.reason)
        ctx = self._context(aid)
        try:
            preview = tool.preview(args, ctx)
        except ActionError as exc:
            log("blocked", str(exc))
            return Outcome(aid, tool.name, "blocked", str(exc))
        if tool.network and read_so_far and leaks(meta["args"], read_so_far):
            reason = "would send text read earlier in this request off the machine"
            log("blocked", reason)
            return Outcome(aid, tool.name, "blocked", reason, preview)
        if dry_run:
            log("declined", f"dry run ({decision.verdict}: {decision.reason})")
            return Outcome(aid, tool.name, "dry_run", decision.reason, preview)
        if decision.verdict == "confirm":
            prompt = self._prompt(
                tool.name, int(tool.tier), origin, tainted, preview, decision.reason
            )
            try:
                approved = self.confirmer(prompt)
            except Deferred:
                log("declined", "waiting for your approval in the dashboard")
                return Outcome(aid, tool.name, "pending", decision.reason, preview)
            if not approved:
                log("declined", decision.reason)
                return Outcome(aid, tool.name, "declined", "you declined", preview)
            log("confirmed", decision.reason)
        else:
            log("auto", decision.reason)

        if killswitch.engaged(self.runtime_dir):  # engaged while you were confirming
            log("blocked", "kill switch is on")
            return Outcome(aid, tool.name, "blocked", "kill switch is on", preview)
        log("started", preview)
        stop_watch = threading.Event()
        watcher = threading.Thread(target=self._watch_kill, args=(ctx, stop_watch), daemon=True)
        watcher.start()
        try:
            result = tool.run(args, ctx)
            if ctx.cancel.is_set():
                raise Cancelled
        except Cancelled:
            log("cancelled", "kill switch")
            return Outcome(aid, tool.name, "cancelled", "cancelled by the kill switch", preview)
        except ActionError as exc:
            log("failed", str(exc))
            return Outcome(aid, tool.name, "failed", str(exc), preview)
        except Exception as exc:  # a tool bug: record it, never crash the caller
            log("failed", f"{type(exc).__name__}: {exc}")
            return Outcome(aid, tool.name, "failed", f"{type(exc).__name__}: {exc}", preview)
        finally:
            stop_watch.set()
        log("succeeded", result.message, result.undo)
        return Outcome(aid, tool.name, "succeeded", result.message, preview, result)

    def undo(self, action_id: str) -> Outcome:
        stages = history(self.conn, action_id)
        done = next((s for s in stages if s.stage == "succeeded"), None)
        if done is None:
            return Outcome(action_id, "?", "invalid", "that action didn't succeed, nothing to undo")
        if any(s.stage == "undone" for s in stages):
            return Outcome(action_id, done.tool, "invalid", "already undone")
        if not done.undo:
            return Outcome(action_id, done.tool, "invalid", f"{done.tool} can't be undone")
        out = self.request(
            done.undo["tool"], done.undo["args"], origin=Origin.USER, undo_of=action_id
        )
        if out.ok:
            self.audit.record(
                action_id, "undone", tool=done.tool, tier=done.tier, origin=done.origin,
                tainted=done.tainted, args=done.args, detail=f"by {out.action_id}",
            )  # fmt: skip
        return out

    # --- helpers ------------------------------------------------------------------------------

    def _context(self, action_id: str) -> ToolContext:
        return ToolContext(
            self.config, self.conn, self.clock, self.desktop, self.paths, action_id,
            provider=self.provider,
        )  # fmt: skip

    def _watch_kill(self, ctx: ToolContext, stop: threading.Event) -> None:
        while not stop.wait(0.2):
            if killswitch.engaged(self.runtime_dir):
                ctx.cancel.set()
                return

    @staticmethod
    def _prompt(
        tool: str, tier: int, origin: Origin, tainted: bool, preview: str, reason: str
    ) -> str:
        source = {"user": "you asked for it", "planned": "planned from your request",
                  "proactive": "suggested, not asked for"}[str(origin)]  # fmt: skip
        taint = (
            "\n  ! follows captured content (page text, files, command output)" if tainted else ""
        )
        return (
            f"{tool} (tier {tier}, {source}; {reason}){taint}\n"
            + "\n".join(f"  {line}" for line in preview.splitlines())
            + "\nProceed? [y/N] "
        )
