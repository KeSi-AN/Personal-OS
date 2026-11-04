"""Tier 2 command and git tools (confirm every time), tier 3 shell (blocked unless unlocked).

Commands run as argument lists without a shell, in an allowed folder, with a timeout, and stop
when the kill switch is engaged. Their output is captured content.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import Field

from personal_os.actions.base import ActionError, Args, Result, Tier, Tool, ToolContext
from personal_os.actions.tools.common import run_process
from personal_os.projects.registry import load_projects

_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")


class NamedArgs(Args):
    name: str


class RunCommand(Tool):
    name = "run_command"
    summary = "run one of your named commands from config (actions.commands)"
    tier = Tier.CONFIRM
    args_model = NamedArgs

    def _spec(self, args: NamedArgs, ctx: ToolContext) -> tuple[list[str], Path | None, float]:
        commands = ctx.config.actions.commands
        spec = commands.get(args.name)
        if spec is None:
            known = ", ".join(commands) or "none yet; add [actions.commands.NAME] to config.toml"
            raise ActionError(f"no named command '{args.name}' (known: {known})")
        cwd = ctx.paths.check(os.path.expanduser(spec.cwd), must_exist=True) if spec.cwd else None
        return list(spec.argv), cwd, spec.timeout_s or ctx.config.actions.command_timeout_s

    def preview(self, args: NamedArgs, ctx: ToolContext) -> str:
        argv, cwd, timeout = self._spec(args, ctx)
        return (
            f"run {' '.join(argv)}"
            + (f"\n  in {cwd}" if cwd else "")
            + f"\n  (stops after {timeout:.0f} s)"
        )

    def run(self, args: NamedArgs, ctx: ToolContext) -> Result:
        argv, cwd, timeout = self._spec(args, ctx)
        code, out = run_process(argv, cwd, timeout, ctx.cancel)
        msg = f"{args.name} exited with {code}"
        return Result(msg, data=f"{msg}\n{out}", captured=True)


class ShellArgs(Args):
    command: str = Field(min_length=1, max_length=2000)
    cwd: str | None = None


class RunShell(Tool):
    name = "run_shell"
    summary = "run a PowerShell command that isn't in your named list"
    tier = Tier.BLOCKED
    args_model = ShellArgs

    def preview(self, args: ShellArgs, ctx: ToolContext) -> str:
        cwd = ctx.paths.check(args.cwd, must_exist=True) if args.cwd else None
        return f"powershell: {args.command}" + (f"\n  in {cwd}" if cwd else "")

    def run(self, args: ShellArgs, ctx: ToolContext) -> Result:
        cwd = ctx.paths.check(args.cwd, must_exist=True) if args.cwd else None
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", args.command]
        code, out = run_process(argv, cwd, ctx.config.actions.command_timeout_s, ctx.cancel)
        return Result(f"exited with {code}", data=out, captured=True)


# --- git ------------------------------------------------------------------------------------


def _repo(raw: str, ctx: ToolContext) -> Path:
    """A project's repo folder, by project id or path."""
    for p in load_projects(ctx.conn):
        if raw.lower() == p.id.lower() and p.roots:
            raw = p.roots[0]
            break
    path = ctx.paths.check(raw, must_exist=True)
    if not (path / ".git").exists():
        raise ActionError(f"{path} isn't a git repository")
    return path


def _git(repo: Path, ctx: ToolContext, *argv: str) -> tuple[int, str]:
    return run_process(["git", "-C", str(repo), *argv], None, 60, ctx.cancel)


def _head(repo: Path, ctx: ToolContext) -> str:
    code, out = _git(repo, ctx, "rev-parse", "HEAD")
    return out.strip() if code == 0 else ""


class CommitArgs(Args):
    repo: str
    message: str = Field(min_length=3, max_length=500)
    all_tracked: bool = False  # also stage changes to tracked files (git commit -a)


class GitCommit(Tool):
    name = "git_commit"
    summary = "commit staged changes (or all tracked changes) in a project's repo"
    tier = Tier.CONFIRM
    reversible = True
    args_model = CommitArgs

    def preview(self, args: CommitArgs, ctx: ToolContext) -> str:
        repo = _repo(args.repo, ctx)
        _, status = _git(repo, ctx, "status", "--porcelain")
        lines = [ln for ln in status.splitlines() if ln[:1] not in (" ", "?") or args.all_tracked]
        lines = [ln for ln in lines if not ln.startswith("??")]
        if not lines:
            raise ActionError(
                "nothing to commit" + ("" if args.all_tracked else " (nothing staged)")
            )
        return (
            f"git commit{' -a' if args.all_tracked else ''} in {repo}\n  message: {args.message}\n"
            + "\n".join(f"  {ln}" for ln in lines[:30])
        )

    def run(self, args: CommitArgs, ctx: ToolContext) -> Result:
        repo = _repo(args.repo, ctx)
        argv = ["commit", "-m", args.message] + (["-a"] if args.all_tracked else [])
        code, out = _git(repo, ctx, *argv)
        if code != 0:
            raise ActionError(f"git commit failed: {out.strip()[:300]}")
        head = _head(repo, ctx)
        undo = {"tool": "git_uncommit", "args": {"repo": str(repo), "sha": head}}
        return Result(f"committed {head[:8]} in {repo.name}", undo=undo)


class UncommitArgs(Args):
    repo: str
    sha: str = Field(pattern=r"^[0-9a-f]{40}$")


class GitUncommit(Tool):
    """Undo for git_commit: `git reset --soft HEAD~1`, only while that commit is still HEAD."""

    name = "git_uncommit"
    summary = "(undo) take back the last commit this assistant made, keeping the changes"
    tier = Tier.CONFIRM
    args_model = UncommitArgs
    internal = True

    def preview(self, args: UncommitArgs, ctx: ToolContext) -> str:
        repo = _repo(args.repo, ctx)
        if _head(repo, ctx) != args.sha:
            raise ActionError("that commit isn't the latest any more; undo it by hand")
        return f"git reset --soft HEAD~1 in {repo} (the changes stay staged)"

    def run(self, args: UncommitArgs, ctx: ToolContext) -> Result:
        repo = _repo(args.repo, ctx)
        code, out = _git(repo, ctx, "reset", "--soft", "HEAD~1")
        if code != 0:
            raise ActionError(f"git reset failed: {out.strip()[:300]}")
        return Result(f"took back {args.sha[:8]} in {repo.name}")


class SwitchArgs(Args):
    repo: str
    branch: str


class GitSwitch(Tool):
    name = "git_switch"
    summary = "switch a project's repo to an existing branch"
    tier = Tier.CONFIRM
    reversible = True
    args_model = SwitchArgs

    def preview(self, args: SwitchArgs, ctx: ToolContext) -> str:
        if not _BRANCH.match(args.branch) or args.branch.startswith("-"):
            raise ActionError(f"'{args.branch}' isn't a valid branch name")
        repo = _repo(args.repo, ctx)
        code, _ = _git(repo, ctx, "rev-parse", "--verify", "--quiet", f"refs/heads/{args.branch}")
        if code != 0:
            raise ActionError(f"no branch '{args.branch}' in {repo.name}")
        return f"git switch {args.branch} in {repo}"

    def run(self, args: SwitchArgs, ctx: ToolContext) -> Result:
        repo = _repo(args.repo, ctx)
        _, current = _git(repo, ctx, "branch", "--show-current")
        code, out = _git(repo, ctx, "switch", args.branch)
        if code != 0:
            raise ActionError(f"git switch failed: {out.strip()[:300]}")
        undo = (
            {"tool": "git_switch", "args": {"repo": str(repo), "branch": current.strip()}}
            if current.strip()
            else None
        )
        return Result(f"{repo.name} is on {args.branch}", undo=undo)


class StashArgs(Args):
    repo: str


class GitStash(Tool):
    name = "git_stash"
    summary = "stash a project's uncommitted changes"
    tier = Tier.CONFIRM
    reversible = True
    args_model = StashArgs

    def preview(self, args: StashArgs, ctx: ToolContext) -> str:
        repo = _repo(args.repo, ctx)
        _, status = _git(repo, ctx, "status", "--porcelain", "--untracked-files=no")
        if not status.strip():
            raise ActionError("nothing to stash")
        return f"git stash in {repo}\n" + "\n".join(f"  {ln}" for ln in status.splitlines()[:30])

    def run(self, args: StashArgs, ctx: ToolContext) -> Result:
        repo = _repo(args.repo, ctx)
        code, out = _git(repo, ctx, "stash", "push", "-m", f"personal-os {ctx.action_id[-8:]}")
        if code != 0:
            raise ActionError(f"git stash failed: {out.strip()[:300]}")
        return Result(f"stashed changes in {repo.name}",
                      undo={"tool": "git_stash_pop", "args": {"repo": str(repo)}})  # fmt: skip


class GitStashPop(Tool):
    name = "git_stash_pop"
    summary = "(undo) re-apply the latest stash"
    tier = Tier.CONFIRM
    args_model = StashArgs
    internal = True

    def preview(self, args: StashArgs, ctx: ToolContext) -> str:
        return f"git stash pop in {_repo(args.repo, ctx)}"

    def run(self, args: StashArgs, ctx: ToolContext) -> Result:
        repo = _repo(args.repo, ctx)
        code, out = _git(repo, ctx, "stash", "pop")
        if code != 0:
            raise ActionError(f"git stash pop failed: {out.strip()[:300]}")
        return Result(f"re-applied the stash in {repo.name}")
