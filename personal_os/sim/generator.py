"""Expands a scenario into the exact events the real observers would emit.

That includes open focus intervals with 60 s checkpoints (same id, ts_end=None, rising
last_seen) and a final close, so storage upserts and crash recovery get exercised.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, tzinfo
from pathlib import PurePosixPath
from urllib.parse import quote_plus, urlsplit
from uuid import UUID

from personal_os.core.clock import SimClock
from personal_os.core.events import (
    BrowserSignal,
    Event,
    FsChange,
    GitCommit,
    GitState,
    Idle,
    PageExtract,
    PageVisit,
    SystemEvent,
    TerminalCommand,
    WindowFocus,
)
from personal_os.core.ids import uuid7
from personal_os.sim.scenario import (
    AwayStep,
    BrowseStep,
    CommandStep,
    CommitStep,
    EditStep,
    FocusStep,
    IdleStep,
    Scenario,
)

SIM_VERSION = "sim-0.1.0"

_CODE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java", ".kt", ".cs", ".cpp",
              ".c", ".h", ".rb", ".php", ".swift", ".sql", ".sh", ".ps1"}  # fmt: skip
_TEXT_EXTS = {".md", ".txt", ".toml", ".yaml", ".yml", ".json", ".ini", ".cfg", ".csv"}


def file_type(path: str) -> str:
    ext = PurePosixPath(path.replace("\\", "/")).suffix.lower()
    if ext in _CODE_EXTS:
        return "code"
    if ext in _TEXT_EXTS:
        return "text"
    return "other"


class _OpenFocus:
    def __init__(self, id: UUID, payload: WindowFocus, start: datetime) -> None:
        self.id = id
        self.payload = payload
        self.start = start
        self.last_checkpoint = start


class Generator:
    def __init__(self, scenario: Scenario, *, checkpoint_s: float = 60.0) -> None:
        self.scenario = scenario
        self.checkpoint_s = checkpoint_s
        self.clock = SimClock(scenario.start)
        tz = scenario.start.tzinfo
        assert tz is not None
        self.tz: tzinfo = tz
        self.events: list[Event] = []
        self._focus: _OpenFocus | None = None

    def now(self) -> datetime:
        return self.clock.now().astimezone(self.tz)

    def run(self) -> list[Event]:
        self._emit(Event.point(SystemEvent(kind="system.daemon_start"), self.now(),
                               observer_version=SIM_VERSION))  # fmt: skip
        for step in self.scenario.steps:
            self._advance(step.gap)
            match step:
                case FocusStep():
                    self._focus_step(step)
                case EditStep():
                    self._edit_step(step)
                case CommandStep():
                    self._command_step(step)
                case BrowseStep():
                    self._browse_step(step)
                case CommitStep():
                    self._commit_step(step)
                case IdleStep():
                    self._idle_step(step)
                case AwayStep():
                    self._away_step(step)
        self._close_focus()
        self._emit(Event.point(SystemEvent(kind="system.daemon_stop"), self.now(),
                               observer_version=SIM_VERSION))  # fmt: skip
        return self.events

    # --- time and focus --------------------------------------------------------------------

    def _emit(self, event: Event) -> None:
        self.events.append(event)

    def _advance(self, seconds: float) -> None:
        """Move time forward, emitting checkpoints for the open focus interval on the way."""
        target = self.clock.now() + timedelta(seconds=seconds)
        focus = self._focus
        while focus is not None:
            next_cp = focus.last_checkpoint.astimezone(self.tz) + timedelta(
                seconds=self.checkpoint_s
            )
            if next_cp > target:
                break
            self.clock.advance((next_cp - self.clock.now()).total_seconds())
            focus.last_checkpoint = next_cp
            self._emit(self._focus_event(focus, end=None))
        remaining = (target - self.clock.now()).total_seconds()
        if remaining > 0:
            self.clock.advance(remaining)

    def _focus_event(self, focus: _OpenFocus, end: datetime | None) -> Event:
        return Event.interval(
            focus.payload,
            focus.start,
            end,
            id=focus.id,
            last_seen=end or self.now(),
            observer_version=SIM_VERSION,
        )

    def _open_focus(self, payload: WindowFocus) -> None:
        self._close_focus()
        start = self.now()
        self._focus = _OpenFocus(uuid7(start), payload, start)
        self._emit(self._focus_event(self._focus, end=None))

    def _close_focus(self) -> None:
        if self._focus is not None:
            self._emit(self._focus_event(self._focus, end=self.now()))
            self._focus = None

    # --- steps -----------------------------------------------------------------------------

    def _focus_step(self, step: FocusStep) -> None:
        self._open_focus(
            WindowFocus(
                app=step.app,
                exe=step.exe,
                title=step.title,
                cwd=step.cwd,
                transient_switches=step.transient_switches,
            )
        )
        self._advance(step.duration)

    def _edit_step(self, step: EditStep) -> None:
        per_save = step.over / step.saves
        for i in range(step.saves):
            self._advance(per_save)
            last = i == step.saves - 1
            ext = PurePosixPath(step.path.replace("\\", "/")).suffix.lower() or None
            self._emit(
                Event.point(
                    FsChange(
                        path=step.path,
                        change=step.change if (i == 0 or step.change != "created") else "modified",
                        ext=ext,
                        file_type=file_type(step.path),
                        repo_root=step.repo_root,
                        lines_added=_share(step.lines_added, step.saves, i),
                        lines_removed=_share(step.lines_removed, step.saves, i),
                        symbols_added=step.symbols_added if last else [],
                        symbols_changed=step.symbols_changed if last else [],
                    ),
                    self.now(),
                    observer_version=SIM_VERSION,
                )
            )

    def _command_step(self, step: CommandStep) -> None:
        start = self.now()
        self._advance(step.duration)
        self._emit(
            Event.interval(
                TerminalCommand(
                    command=step.command,
                    cwd=step.cwd,
                    shell=step.shell,
                    shell_session="sim-1",
                    host="WindowsTerminal",
                    exit_code=step.exit_code,
                    success=step.exit_code == 0,
                    duration_ms=int(step.duration * 1000),
                    repo_root=step.repo_root,
                    branch=step.branch,
                ),
                start,
                self.now(),
                observer_version=SIM_VERSION,
            )
        )

    def _browse_step(self, step: BrowseStep) -> None:
        domain = (urlsplit(step.url).hostname or "").lower()
        if step.search_query:
            self._emit(
                Event.point(
                    BrowserSignal(
                        kind="browser.search",
                        url=f"https://www.{step.search_engine}.com/search?q={quote_plus(step.search_query)}",
                        domain=f"www.{step.search_engine}.com",
                        engine=step.search_engine,
                        text=step.search_query,
                        incognito=step.incognito,
                    ),
                    self.now(),
                    observer_version=SIM_VERSION,
                )
            )
        window_title = step.title or step.url
        if step.incognito:
            window_title = f"{window_title} - Private window"
        self._open_focus(
            WindowFocus(
                app=step.browser_app,
                exe=step.browser_exe,
                title=f"{window_title} - {step.browser_app}",
            )
        )
        start = self.now()
        self._advance(step.dwell)
        extract = None
        if step.extract is not None:
            text = step.extract.main_text
            extract = PageExtract(
                title=step.title,
                meta_description=step.extract.meta_description,
                headings=step.extract.headings,
                main_text=text,
                word_count=len(text.split()),
                content_hash=hashlib.sha256(text.encode()).hexdigest()[:32],
                lang=step.extract.lang,
            )
        self._emit(
            Event.interval(
                PageVisit(
                    url=step.url,
                    domain=domain,
                    title=step.title,
                    transition="search" if step.search_query else "link",
                    active_dwell_ms=int(step.dwell * 1000),
                    scroll_max_pct=step.scroll_max_pct,
                    bookmarked=step.bookmarked,
                    searched=bool(step.search_query),
                    search_query=step.search_query,
                    content_type=step.content_type,
                    lang=step.extract.lang if step.extract else None,
                    incognito=step.incognito,
                    has_password_field=step.has_password_field,
                    extract=extract,
                ),
                start,
                self.now(),
                observer_version=SIM_VERSION,
            )
        )

    def _commit_step(self, step: CommitStep) -> None:
        sha = step.sha or hashlib.sha1(f"{step.message}{self.now()}".encode()).hexdigest()
        now = self.now()
        self._emit(
            Event.point(
                GitCommit(
                    repo_root=step.repo_root,
                    sha=sha,
                    branch=step.branch,
                    message=step.message,
                    files_changed=len(step.files),
                    insertions=step.insertions,
                    deletions=step.deletions,
                    files=step.files,
                ),
                now,
                observer_version=SIM_VERSION,
            )
        )
        self._emit(
            Event.point(
                GitState(repo_root=step.repo_root, branch=step.branch, head=sha[:12]),
                now,
                observer_version=SIM_VERSION,
            )
        )

    def _idle_step(self, step: IdleStep) -> None:
        self._close_focus()
        start = self.now()
        self._advance(step.duration)
        self._emit(
            Event.interval(
                Idle(threshold_s=self.scenario.idle_threshold_s, media_playing=step.media_playing),
                start,
                self.now(),
                observer_version=SIM_VERSION,
            )
        )

    def _away_step(self, step: AwayStep) -> None:
        self._close_focus()
        begin, end_kind = (
            ("system.lock", "system.unlock")
            if step.action == "lock"
            else ("system.sleep", "system.wake")
        )
        start = self.now()
        self._emit(Event.point(SystemEvent(kind=begin), start, observer_version=SIM_VERSION))
        self._advance(step.duration)
        self._emit(
            Event.interval(
                Idle(
                    threshold_s=self.scenario.idle_threshold_s,
                    reason="locked" if step.action == "lock" else "asleep",
                ),
                start,
                self.now(),
                observer_version=SIM_VERSION,
            )
        )
        self._emit(
            Event.point(SystemEvent(kind=end_kind), self.now(), observer_version=SIM_VERSION)
        )


def _share(total: int, parts: int, index: int) -> int:
    base, rem = divmod(total, parts)
    return base + (1 if index < rem else 0)


def generate(scenario: Scenario, *, checkpoint_s: float = 60.0) -> list[Event]:
    return Generator(scenario, checkpoint_s=checkpoint_s).run()
