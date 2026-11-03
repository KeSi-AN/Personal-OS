"""Scenario DSL for the fake desktop simulator (DESIGN.md §4.13).

A scenario is a TOML file: a start time and a list of human-level steps run one after the
other. Durations are numbers of seconds or strings like "90s", "25m", "1h30m".

    name = "morning"
    start = 2026-11-02T09:00:00+05:30

    [[steps]]
    action = "focus"
    app = "Code"
    exe = "Code.exe"
    title = "session.py - auth-service - Visual Studio Code"

    [[steps]]
    action = "edit"
    path = "C:/Users/me/Documents/auth-service/auth/session.py"
    saves = 6
    over = "20m"
"""

from __future__ import annotations

import re
import tomllib
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field

_DURATION_RE = re.compile(
    r"^\s*(?:(\d+(?:\.\d+)?)h)?\s*(?:(\d+(?:\.\d+)?)m)?\s*(?:(\d+(?:\.\d+)?)s)?\s*$"
)


def parse_duration(value: Any) -> float:
    """'1h30m' / '25m' / '90s' / 90 → seconds."""
    if isinstance(value, int | float):
        if value < 0:
            raise ValueError("duration cannot be negative")
        return float(value)
    if isinstance(value, str):
        m = _DURATION_RE.match(value)
        if m and any(m.groups()):
            h, mi, s = (float(g) if g else 0.0 for g in m.groups())
            return h * 3600 + mi * 60 + s
    raise ValueError(f"invalid duration: {value!r}")


Duration = Annotated[float, BeforeValidator(parse_duration)]


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gap: Duration = 0.0  # wait this long before the step starts
    # Ground truth for evaluation only (which project this activity really belonged to);
    # never put into events. Use "none" for activity that isn't project work.
    project: str | None = None


class FocusStep(_Step):
    action: Literal["focus"]
    app: str
    exe: str | None = None
    title: str = ""
    cwd: str | None = None
    duration: Duration = 0.0
    transient_switches: int = 0


class EditStep(_Step):
    action: Literal["edit"]
    path: str
    repo_root: str | None = None
    change: Literal["created", "modified", "deleted"] = "modified"
    saves: int = Field(1, ge=1)
    over: Duration = 0.0
    lines_added: int = 0
    lines_removed: int = 0
    symbols_added: list[str] = []
    symbols_changed: list[str] = []


class CommandStep(_Step):
    action: Literal["command"]
    command: str
    cwd: str | None = None
    repo_root: str | None = None
    branch: str | None = None
    shell: Literal["powershell", "bash"] = "powershell"
    exit_code: int = 0
    duration: Duration = 1.0


class ExtractSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meta_description: str = ""
    headings: list[str] = []
    main_text: str = ""
    lang: str | None = "en"


class BrowseStep(_Step):
    action: Literal["browse"]
    url: str
    title: str = ""
    dwell: Duration = 60.0
    search_query: str | None = None
    search_engine: str = "google"
    content_type: str = "other"
    browser_app: str = "Brave"
    browser_exe: str = "brave.exe"
    bookmarked: bool = False
    incognito: bool = False
    has_password_field: bool = False
    scroll_max_pct: int | None = None
    extract: ExtractSpec | None = None


class CommitStep(_Step):
    action: Literal["commit"]
    repo_root: str
    message: str
    remote: str | None = None
    sha: str | None = None
    branch: str | None = None
    files: list[str] = []
    insertions: int = 0
    deletions: int = 0


class IdleStep(_Step):
    action: Literal["idle"]
    duration: Duration
    media_playing: bool = False


class AwayStep(_Step):
    """Screen locked or machine asleep for `duration`."""

    action: Literal["lock", "sleep"]
    duration: Duration


AnyStep = Annotated[
    FocusStep | EditStep | CommandStep | BrowseStep | CommitStep | IdleStep | AwayStep,
    Field(discriminator="action"),
]


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    start: AwareDatetime
    idle_threshold_s: int = 300
    # Project-resolution evaluation: events before this are the "learning" day, after it the test.
    evaluate_from: AwareDatetime | None = None
    steps: list[AnyStep]


def load_scenario(path: Path) -> Scenario:
    raw = tomllib.loads(path.read_text("utf-8"))
    start = raw.get("start")
    if isinstance(start, datetime) and start.tzinfo is None:
        raise ValueError("scenario start must include a UTC offset, e.g. 2026-11-02T09:00:00+05:30")
    return Scenario.model_validate(raw)
