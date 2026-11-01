"""Git observer: the source of truth for code progress (DESIGN.md §4.7).

Refreshes a repo when the filesystem observer reports activity in it (3 s debounce), plus a
safety poll. Emits git.state only when the state changes, and git.commit / git.branch_switch /
git.op by comparing with the previous state. Last seen HEADs are persisted, so commits made
while the daemon was off are still recorded.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from personal_os.config.schema import GitObserverConfig
from personal_os.core.events import Event, GitBranchSwitch, GitCommit, GitOp, GitState
from personal_os.core.observer import BaseObserver, ObserverContext
from personal_os.observers.gitutil import (
    GitError,
    GitRunner,
    detect_op,
    discover_repos,
    norm,
    parse_log,
    parse_numstat_z,
    parse_status_v2,
)

VERSION = "1.0.0"
HEADS_KEY = "git.heads"
MAX_NEW_COMMITS = 20


@dataclass
class _RepoState:
    root: str
    state_hash: str | None = None
    head: str | None = None
    branch: str | None = None
    op: str = "none"
    last_activity: float = 0.0
    last_refresh: float = 0.0
    my_email: str | None = None
    unreadable: str | None = None


@dataclass
class _Pending:
    due: dict[str, float] = field(default_factory=dict)


class GitObserver(BaseObserver):
    name = "git"
    version = VERSION

    def __init__(
        self,
        roots: list[Path],
        cfg: GitObserverConfig,
        runner: GitRunner | None = None,
        *,
        debounce_s: float = 3.0,
        tick_s: float = 1.0,
    ) -> None:
        super().__init__()
        self.roots = roots
        self.cfg = cfg
        self.runner = runner or GitRunner()
        self.debounce_s = debounce_s
        self.tick_s = tick_s
        self.repos: dict[str, _RepoState] = {}
        self._pending = _Pending()

    # --- public ----------------------------------------------------------------------------

    def notify(self, repo_root: str) -> None:
        """Called by the fs observer (on the event loop) when something changed in a repo."""
        if self._ctx is None:
            return
        root = norm(repo_root)
        now = self._ctx.clock.monotonic()
        if root not in self.repos:
            self.repos[root] = _RepoState(root)
        self.repos[root].last_activity = now
        self._pending.due.setdefault(root, now + self.debounce_s)

    async def refresh(self, root: str, *, baseline: bool = False) -> list[Event]:
        """Refresh one repo and return (and emit) the resulting events."""
        assert self._ctx is not None
        repo = self.repos.setdefault(root, _RepoState(root))
        repo.last_refresh = self._ctx.clock.monotonic()
        try:
            events = await self._refresh(repo, baseline=baseline)
            repo.unreadable = None
        except (GitError, OSError) as exc:
            # e.g. "detected dubious ownership" (safe.directory): report, never change git config.
            repo.unreadable = str(exc)[:200]
            return []
        for e in events:
            self.emit(e)
        return events

    # --- loop ------------------------------------------------------------------------------

    async def run(self, ctx: ObserverContext) -> None:
        for root in await asyncio.to_thread(discover_repos, self.roots):
            self.repos.setdefault(root, _RepoState(root))
        for root in list(self.repos):
            if ctx.paused.is_set():
                break
            await self.refresh(root, baseline=True)
        while True:
            await ctx.clock.sleep(self.tick_s)
            if ctx.paused.is_set():
                continue
            now = ctx.clock.monotonic()
            due = [r for r, t in self._pending.due.items() if t <= now]
            for root in due:
                del self._pending.due[root]
                await self.refresh(root)
            for repo in list(self.repos.values()):
                active = now - repo.last_activity < 86_400 and repo.last_activity > 0
                interval = (
                    self.cfg.safety_poll_active_s if active else self.cfg.safety_poll_inactive_s
                )
                if now - repo.last_refresh >= interval and repo.root not in self._pending.due:
                    await self.refresh(repo.root)

    # --- internals -------------------------------------------------------------------------

    async def _refresh(self, repo: _RepoState, *, baseline: bool) -> list[Event]:
        assert self._ctx is not None
        r = self.runner
        status = parse_status_v2(
            await r.run(repo.root, "status", "--porcelain=v2", "--branch", "--show-stash", "-z")
        )
        numstat: dict[str, tuple[int | None, int | None]] = {}
        if status.head and (status.staged or status.unstaged):
            numstat = parse_numstat_z(await r.run(repo.root, "diff", "HEAD", "--numstat", "-z"))
        files = [
            f.model_copy(
                update={
                    "added": numstat.get(f.path, (None, None))[0],
                    "removed": numstat.get(f.path, (None, None))[1],
                }
            )
            for f in status.files
        ]
        op = detect_op(repo.root)
        now = self._ctx.clock.now().astimezone()
        events: list[Event] = []
        heads: dict[str, str] = self._ctx.state.get(HEADS_KEY, {}) or {}
        previous_head = repo.head or (heads.get(repo.root) if baseline else None)

        if repo.my_email is None:
            out = await r.run(repo.root, "config", "user.email", check=False)
            repo.my_email = out.decode("utf-8", "replace").strip().lower()

        if previous_head and status.head and status.head != previous_head:
            events.extend(
                await self._new_commits(repo, previous_head, status.head, status.branch, now)
            )
        if repo.state_hash is not None:
            if status.branch != repo.branch:
                events.append(
                    self._point(
                        GitBranchSwitch(
                            repo_root=repo.root, from_branch=repo.branch, to_branch=status.branch
                        ),
                        now,
                    )
                )
            if op != repo.op:
                if repo.op != "none":
                    events.append(
                        self._point(
                            GitOp(
                                repo_root=repo.root,
                                op=repo.op,
                                state="finished",
                            ),
                            now,
                        )
                    )
                if op != "none":
                    events.append(
                        self._point(
                            GitOp(
                                repo_root=repo.root,
                                op=op,
                                state="started",
                            ),
                            now,
                        )
                    )

        state = GitState(
            repo_root=repo.root,
            branch=status.branch,
            head=status.head[:12] if status.head else None,
            upstream=status.upstream,
            ahead=status.ahead,
            behind=status.behind,
            staged=status.staged,
            unstaged=status.unstaged,
            untracked=status.untracked,
            files=files,
            op=op,
            stash_count=status.stash_count,
        )
        # Line counts are left out of the change test: they move on every save (the fs observer
        # already reports each edit), while branch/HEAD/which files are dirty is what "state" means.
        shape = state.model_dump(mode="json")
        shape["files"] = [(f["path"], f["status"]) for f in shape["files"]]
        digest = hashlib.sha1(repr(sorted(shape.items())).encode()).hexdigest()
        if digest != repo.state_hash:
            events.append(self._point(state, now))
            repo.state_hash = digest
        repo.head, repo.branch, repo.op = status.head, status.branch, op
        if status.head and heads.get(repo.root) != status.head:
            heads[repo.root] = status.head
            self._ctx.state.set(HEADS_KEY, heads)
        return events

    async def _new_commits(
        self, repo: _RepoState, old: str, new: str, branch: str | None, now: datetime
    ) -> list[Event]:
        # Only commits reachable from the new HEAD and not from the old one (handles amend,
        # rebase, pull). A reset to an older commit yields nothing, which is right.
        raw = await self.runner.run(
            repo.root,
            "log",
            f"--max-count={MAX_NEW_COMMITS}",
            "--format=%H%x1f%P%x1f%ae%x1f%B%x1e",
            f"{old}..{new}",
            check=False,
        )
        events = []
        for c in reversed(parse_log(raw)):
            stats = parse_numstat_z(
                await self.runner.run(
                    repo.root,
                    "diff-tree",
                    "--no-commit-id",
                    "--numstat",
                    "-r",
                    "-z",
                    "--root",
                    c.sha,
                    check=False,
                )
            )
            payload = GitCommit(
                repo_root=repo.root,
                sha=c.sha,
                branch=branch,
                parents=c.parents,
                author_is_me=bool(repo.my_email) and c.author_email.lower() == repo.my_email,
                message=c.message[:2000],
                files_changed=len(stats),
                insertions=sum(a or 0 for a, _ in stats.values()),
                deletions=sum(d or 0 for _, d in stats.values()),
                files=sorted(stats)[:100],
            )
            events.append(self._point(payload, now))
        return events

    def _point(
        self, payload: GitState | GitCommit | GitBranchSwitch | GitOp, now: datetime
    ) -> Event:
        return Event.point(payload, now, observer_version=VERSION, project_hint=payload.repo_root)

    def describe(self) -> dict[str, object]:
        """For doctor: repos known and any that git refused to read."""
        return {
            "repos": len(self.repos),
            "unreadable": {r.root: r.unreadable for r in self.repos.values() if r.unreadable},
        }
