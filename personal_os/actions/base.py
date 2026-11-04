"""Tool contract for the action layer (DESIGN.md §6.3).

A tool is a typed function with a risk tier. `preview` says exactly what will happen (shown for
dry runs and confirmations); `run` does it and may return an undo recipe. Tools never run a
shell on an LLM's behalf: a model can only fill in a tool's arguments.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict

from personal_os.config.schema import Config
from personal_os.core.clock import Clock

if TYPE_CHECKING:
    from personal_os.actions.desktop import Desktop
    from personal_os.actions.paths import PathPolicy
    from personal_os.llm.provider import LLMProvider


class Tier(IntEnum):
    READ = 0  # read-only
    LOW = 1  # low-risk, reversible
    CONFIRM = 2  # confirm every time
    BLOCKED = 3  # blocked unless unlocked, then confirm every time


class Origin(StrEnum):
    USER = "user"  # you named the tool and its arguments yourself
    PLANNED = "planned"  # the planner chose it from words you typed
    PROACTIVE = "proactive"  # suggested by the system, not asked for


class ActionError(Exception):
    """The action can't run as asked (bad path, missing file...). Message is shown to you."""


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


@dataclass
class Result:
    message: str
    data: Any = None  # returned to the planner (tier 0 results)
    undo: dict[str, Any] | None = None  # {"tool": name, "args": {...}}
    captured: bool = False  # data contains captured or file content (taints what follows)


@dataclass
class ToolContext:
    config: Config
    conn: sqlite3.Connection
    clock: Clock
    desktop: Desktop
    paths: PathPolicy
    action_id: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)
    provider: LLMProvider | None = None

    @property
    def undo_dir(self) -> Path:
        return self.config.data_dir / "undo"


class Tool:
    name: ClassVar[str]
    summary: ClassVar[str]  # one line, shown to the planner and in `actions list`
    tier: ClassVar[Tier]
    reversible: ClassVar[bool] = False
    network: ClassVar[bool] = False  # can send data off the machine (exfiltration guard)
    internal: ClassVar[bool] = False  # only used as an undo step; never offered to the planner
    args_model: ClassVar[type[Args]]

    def preview(self, args: Any, ctx: ToolContext) -> str:
        """Exactly what will happen. Raises ActionError if it can't happen."""
        raise NotImplementedError

    def run(self, args: Any, ctx: ToolContext) -> Result:
        raise NotImplementedError

    def signature(self, *, choices: bool = False) -> str:
        """`name(a, b?)`; with choices, fixed values are listed: `media(action: next|mute)`."""
        fields = []
        for name, f in self.args_model.model_fields.items():
            label = name if f.is_required() else f"{name}?"
            values = get_args(f.annotation) if get_origin(f.annotation) is Literal else ()
            if choices and values:
                label += ": " + "|".join(str(v) for v in values)
            fields.append(label)
        return f"{self.name}({', '.join(fields)})"


class Registry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {t.name: t for t in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return sorted(self._tools.values(), key=lambda t: (t.tier, t.name))

    def for_planner(self) -> list[Tool]:
        return [t for t in self.all() if t.tier <= Tier.CONFIRM and not t.internal]


Confirmer = Callable[[str], bool]  # shows the prompt text, returns True only on an explicit yes
