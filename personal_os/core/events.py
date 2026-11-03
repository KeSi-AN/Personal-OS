"""The common event envelope and the typed payload for every source (DESIGN.md §4.1).

Each payload class declares which of its fields hold free text, URLs and paths. The privacy
filter uses those declarations to redact generically, so a new payload type is covered by the
filter as soon as its fields are classified.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from personal_os.core.ids import uuid7


class Source(StrEnum):
    WINDOW = "window"
    IDLE = "idle"
    SYSTEM = "system"
    META = "meta"
    BROWSER = "browser"
    FS = "fs"
    GIT = "git"
    EDITOR = "editor"
    TERMINAL = "terminal"
    CLIPBOARD = "clipboard"


class PrivacyClass(StrEnum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"
    BLOCKED = "blocked"


PRIVACY_RANK = {PrivacyClass.NORMAL: 0, PrivacyClass.SENSITIVE: 1, PrivacyClass.BLOCKED: 2}


def stricter(a: PrivacyClass, b: PrivacyClass) -> PrivacyClass:
    return a if PRIVACY_RANK[a] >= PRIVACY_RANK[b] else b


class RetentionClass(StrEnum):
    RAW = "raw"
    PAGE_TEXT = "page_text"
    TERMINAL_OUTPUT = "terminal_output"
    CLIPBOARD = "clipboard"
    SUMMARY = "summary"


RedactContext = Literal["text", "title", "command", "git", "url"]


class Redactable(BaseModel):
    """Base for payloads and nested payload parts. Field roles drive the privacy filter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    TEXT_FIELDS: ClassVar[tuple[str, ...]] = ()
    URL_FIELDS: ClassVar[tuple[str, ...]] = ()
    PATH_FIELDS: ClassVar[tuple[str, ...]] = ()
    REDACT_CONTEXT: ClassVar[RedactContext] = "text"

    def search_text(self) -> str:
        """Text indexed for full-text search: free text, URLs and paths, plus nested parts."""
        parts: list[str] = []
        for name in (*self.TEXT_FIELDS, *self.URL_FIELDS, *self.PATH_FIELDS):
            parts.extend(_strings(getattr(self, name)))
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, Redactable):
                parts.append(value.search_text())
            elif isinstance(value, list):
                parts.extend(v.search_text() for v in value if isinstance(v, Redactable))
        return "\n".join(p for p in parts if p)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str)]
    return []


class Payload(Redactable):
    kind: str


# --- window / idle / system / meta ---------------------------------------------------------


class WindowFocus(Payload):
    kind: Literal["window.focus"] = "window.focus"
    app: str
    exe: str | None = None
    exe_path: str | None = None
    pid: int | None = None
    title: str = ""
    window_class: str | None = None
    cwd: str | None = None
    is_uwp: bool = False
    elevated_unreadable: bool = False
    transient_switches: int = 0

    TEXT_FIELDS = ("title",)
    PATH_FIELDS = ("exe_path", "cwd")
    REDACT_CONTEXT = "title"


class Idle(Payload):
    kind: Literal["idle.idle"] = "idle.idle"
    threshold_s: int
    reason: Literal["no_input", "locked", "asleep"] = "no_input"
    media_playing: bool = False


class SystemEvent(Payload):
    kind: Literal[
        "system.lock",
        "system.unlock",
        "system.sleep",
        "system.wake",
        "system.logon",
        "system.logoff",
        "system.daemon_start",
        "system.daemon_stop",
    ]


class MetaEvent(Payload):
    kind: Literal["meta.pause", "meta.resume", "meta.forget"]
    range_start: AwareDatetime | None = None
    range_end: AwareDatetime | None = None
    until: AwareDatetime | None = None


# --- browser -------------------------------------------------------------------------------


class PageExtract(Redactable):
    title: str = ""
    meta_description: str = ""
    headings: list[str] = []
    main_text: str = ""
    word_count: int = 0
    content_hash: str = ""
    lang: str | None = None

    TEXT_FIELDS = ("title", "meta_description", "headings", "main_text")


class PageVisit(Payload):
    kind: Literal["browser.page_visit"] = "browser.page_visit"
    url: str
    domain: str = ""
    title: str = ""
    tab_id: int | None = None
    window_id: int | None = None
    transition: str | None = None
    referrer: str | None = None
    opener_visit_id: UUID | None = None
    chain_id: UUID | None = None
    active_dwell_ms: int = 0
    scroll_max_pct: int | None = Field(None, ge=0, le=100)
    video_watched_s: int = 0
    bookmarked: bool = False
    searched: bool = False
    returned_count_7d: int = 0
    search_query: str | None = None
    content_type: str = "other"
    lang: str | None = None
    incognito: bool = False
    has_password_field: bool = False
    extract: PageExtract | None = None
    enrichment: dict[str, str | int | bool | None] = {}

    TEXT_FIELDS = ("title", "search_query")
    URL_FIELDS = ("url", "referrer")


class BrowserSignal(Payload):
    kind: Literal["browser.search", "browser.selection", "browser.copy", "browser.bookmark_added"]
    url: str | None = None
    domain: str = ""
    engine: str | None = None
    text: str = ""
    incognito: bool = False

    TEXT_FIELDS = ("text",)
    URL_FIELDS = ("url",)


# --- files ---------------------------------------------------------------------------------


class FsChange(Payload):
    kind: Literal["fs.change"] = "fs.change"
    path: str
    root: str | None = None
    change: Literal["created", "modified", "deleted", "moved"]
    dest_path: str | None = None
    size: int | None = None
    ext: str | None = None
    file_type: str = "other"
    repo_root: str | None = None
    lines_added: int | None = None
    lines_removed: int | None = None
    symbols_added: list[str] = []
    symbols_removed: list[str] = []
    symbols_changed: list[str] = []
    sensitive: bool = False
    via_git: bool = False  # made by a git operation (pull, merge, checkout, rebase), not by hand

    TEXT_FIELDS = ("symbols_added", "symbols_removed", "symbols_changed")
    PATH_FIELDS = ("path", "dest_path", "root", "repo_root")


class FsBulkChange(Payload):
    kind: Literal["fs.bulk_change"] = "fs.bulk_change"
    dir: str
    count: int
    kinds: dict[str, int] = {}
    sample_paths: list[str] = []

    PATH_FIELDS = ("dir", "sample_paths")


# --- git -----------------------------------------------------------------------------------


class GitFileStat(Redactable):
    path: str
    status: str = "M"
    added: int | None = None
    removed: int | None = None

    PATH_FIELDS = ("path",)


class GitState(Payload):
    kind: Literal["git.state"] = "git.state"
    repo_root: str
    branch: str | None = None
    head: str | None = None
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    staged: int = 0
    unstaged: int = 0
    untracked: int = 0
    files: list[GitFileStat] = []
    op: Literal["none", "merge", "rebase", "cherry_pick", "revert", "bisect"] = "none"
    stash_count: int = 0
    remote: str | None = None  # "github.com/owner/repo" from remote.origin.url, no credentials

    TEXT_FIELDS = ("branch", "upstream")
    PATH_FIELDS = ("repo_root",)
    REDACT_CONTEXT = "git"


class GitCommit(Payload):
    kind: Literal["git.commit"] = "git.commit"
    repo_root: str
    sha: str
    branch: str | None = None
    parents: int = 1
    author_is_me: bool = True
    message: str = ""
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    files: list[str] = []

    TEXT_FIELDS = ("message", "branch")
    PATH_FIELDS = ("repo_root", "files")
    REDACT_CONTEXT = "git"


class GitBranchSwitch(Payload):
    kind: Literal["git.branch_switch"] = "git.branch_switch"
    repo_root: str
    from_branch: str | None = None
    to_branch: str | None = None

    TEXT_FIELDS = ("from_branch", "to_branch")
    PATH_FIELDS = ("repo_root",)
    REDACT_CONTEXT = "git"


class GitOp(Payload):
    kind: Literal["git.op"] = "git.op"
    repo_root: str
    op: Literal["merge", "rebase", "cherry_pick", "revert", "bisect"]
    state: Literal["started", "finished", "aborted"]

    PATH_FIELDS = ("repo_root",)


# --- editor --------------------------------------------------------------------------------


class EditorFocus(Payload):
    kind: Literal["editor.focus"] = "editor.focus"
    editor: str = "vscode"
    workspace: str | None = None
    file: str | None = None
    language: str | None = None
    symbol_path: str | None = None

    TEXT_FIELDS = ("symbol_path",)
    PATH_FIELDS = ("workspace", "file")


class EditorTabs(Payload):
    kind: Literal["editor.tabs"] = "editor.tabs"
    editor: str = "vscode"
    workspace: str | None = None
    files: list[str] = []

    PATH_FIELDS = ("workspace", "files")


class EditorDebug(Payload):
    kind: Literal["editor.debug"] = "editor.debug"
    editor: str = "vscode"
    workspace: str | None = None
    config_type: str | None = None
    state: Literal["started", "ended"]

    PATH_FIELDS = ("workspace",)


class EditorTask(Payload):
    kind: Literal["editor.task"] = "editor.task"
    editor: str = "vscode"
    workspace: str | None = None
    name: str
    exit_code: int | None = None

    TEXT_FIELDS = ("name",)
    PATH_FIELDS = ("workspace",)
    REDACT_CONTEXT = "command"


# --- terminal / clipboard ------------------------------------------------------------------


class TerminalCommand(Payload):
    kind: Literal["terminal.command"] = "terminal.command"
    command: str
    cwd: str | None = None
    shell: Literal["powershell", "bash", "zsh", "fish", "other"] = "other"
    shell_session: str = ""
    host: str | None = None
    exit_code: int | None = None
    success: bool | None = None
    duration_ms: int | None = None
    history_id: int | None = None
    repo_root: str | None = None
    branch: str | None = None

    TEXT_FIELDS = ("command",)
    PATH_FIELDS = ("cwd", "repo_root")
    REDACT_CONTEXT = "command"


class ClipboardText(Payload):
    kind: Literal["clipboard.text"] = "clipboard.text"
    text: str
    app: str | None = None

    TEXT_FIELDS = ("text",)


AnyPayload = Annotated[
    WindowFocus
    | Idle
    | SystemEvent
    | MetaEvent
    | PageVisit
    | BrowserSignal
    | FsChange
    | FsBulkChange
    | GitState
    | GitCommit
    | GitBranchSwitch
    | GitOp
    | EditorFocus
    | EditorTabs
    | EditorDebug
    | EditorTask
    | TerminalCommand
    | ClipboardText,
    Field(discriminator="kind"),
]


# --- envelope ------------------------------------------------------------------------------


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    ts_start: AwareDatetime
    ts_end: AwareDatetime | None = None  # None = interval still open
    last_seen: AwareDatetime | None = None  # checkpoint time of an open interval
    source: Source
    type: str
    payload: AnyPayload
    project_hint: str | None = None
    confidence: float = Field(1.0, ge=0.0, le=1.0)
    privacy_class: PrivacyClass = PrivacyClass.NORMAL
    observer_version: str
    tz_offset_min: int = 0
    retention: RetentionClass = RetentionClass.RAW
    truncated: bool = False

    @field_validator("ts_start", "ts_end", "last_seen")
    @classmethod
    def _utc(cls, v: datetime | None) -> datetime | None:
        return v.astimezone(UTC) if v is not None else None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.payload.kind != f"{self.source}.{self.type}":
            raise ValueError(
                f"payload kind {self.payload.kind!r} does not match {self.source}.{self.type}"
            )
        if self.ts_end is not None and self.ts_end < self.ts_start:
            raise ValueError("ts_end is before ts_start")
        return self

    @property
    def is_open(self) -> bool:
        return self.ts_end is None

    @property
    def kind(self) -> str:
        return self.payload.kind

    @classmethod
    def point(cls, payload: Payload, at: datetime, *, observer_version: str, **kw: Any) -> Event:
        """A point event (ts_end == ts_start)."""
        return cls.interval(payload, at, at, observer_version=observer_version, **kw)

    @classmethod
    def interval(
        cls,
        payload: Payload,
        start: datetime,
        end: datetime | None = None,
        *,
        observer_version: str,
        id: UUID | None = None,
        **kw: Any,
    ) -> Event:
        """An interval event; pass end=None while it is still open, and reuse `id` to update it."""
        source, _, type_ = payload.kind.partition(".")
        offset = start.utcoffset()
        kw.setdefault("tz_offset_min", int(offset.total_seconds() // 60) if offset else 0)
        return cls(
            id=id or uuid7(start),
            ts_start=start,
            ts_end=end,
            source=Source(source),
            type=type_,
            payload=payload,
            observer_version=observer_version,
            **kw,
        )
