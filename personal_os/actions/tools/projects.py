"""Projects you curate (phase 9k): sources, notes, to-dos and resume, by voice or chat.

project_create      a new project by name                                   tier 1
project_add_source  the current tab, a tab group, matching tabs or a link   tier 1
project_note        a quick note                                            tier 1
todo_add / todo_done                                                         tier 1
project_items       a project's sources, notes and to-dos                   tier 0
project_file_inbox  file things added from the browser into a project       tier 1
project_resume      reopen a project's sources as a tab group, with its     tier 1
                    last note and open to-dos
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from personal_os.actions.base import ActionError, Args, Result, Tier, Tool, ToolContext
from personal_os.projects import items
from personal_os.projects.registry import Project


def _project(ctx: ToolContext, name: str) -> Project:
    p = items.resolve(ctx.conn, name)
    if p is None:
        raise ActionError(
            f'there\'s no project called "{name}" (say "create a project called {name}")'
        )
    return p


def _tabs(ctx: ToolContext) -> list[dict[str, Any]]:
    from personal_os.browser import control

    try:
        return list(control.send(ctx.config.runtime_dir, "tabs.list",
                                 browser=ctx.config.actions.browser) or [])  # fmt: skip
    except (control.BrowserUnavailable, control.BrowserError) as exc:
        raise ActionError(str(exc)) from None


PROJECT = Field(min_length=1, max_length=80, description="the project's name")


class CreateArgs(Args):
    name: str = Field(min_length=2, max_length=60)


class ProjectCreate(Tool):
    name = "project_create"
    summary = "create a new project by name (for sources, notes and to-dos)"
    tier = Tier.LOW
    args_model = CreateArgs

    def preview(self, args: CreateArgs, ctx: ToolContext) -> str:
        return f'create the project "{args.name}"'

    def run(self, args: CreateArgs, ctx: ToolContext) -> Result:
        from personal_os.projects.registry import add_project

        existing = items.resolve(ctx.conn, args.name)
        if existing is not None and existing.name.lower() == args.name.lower():
            return Result(f'"{existing.name}" already exists')
        p = add_project(ctx.conn, ctx.clock, args.name, [], [])
        return Result(f'Created the project "{p.name}"')


class SourceArgs(Args):
    project: str = PROJECT
    what: Literal["current_tab", "tab_group", "matching_tabs", "url"] = "current_tab"
    which: str = Field(
        "", max_length=80, description="tab_group: its name; matching_tabs: words in the titles"
    )
    url: str = Field("", max_length=2000)
    title: str = Field("", max_length=200)
    note: str = Field("", max_length=500, description="why it matters, to come back to")


class ProjectAddSource(Tool):
    name = "project_add_source"
    summary = (
        "attach links to a project to come back to later: the current browser tab, a whole tab "
        "group, tabs matching some words, or a web address"
    )
    tier = Tier.LOW
    args_model = SourceArgs

    def _pick(self, args: SourceArgs, ctx: ToolContext) -> list[dict[str, Any]]:
        if args.what == "url":
            if not args.url.startswith(("http://", "https://")):
                raise ActionError("give a web address starting with http:// or https://")
            return [{"url": args.url, "title": args.title or args.url}]
        tabs = _tabs(ctx)
        w = args.which.lower().strip()
        if args.what == "current_tab":
            found = [t for t in tabs if t.get("active")][:1]
        elif args.what == "tab_group":
            found = [t for t in tabs if w and w in str(t.get("groupTitle", "")).lower()]
        else:
            found = [
                t
                for t in tabs
                if w
                and (w in str(t.get("title", "")).lower() or w in str(t.get("url", "")).lower())
            ]
        if not found:
            raise ActionError("no tabs matched" if w else "no current tab")
        return found[:30]

    def preview(self, args: SourceArgs, ctx: ToolContext) -> str:
        p = _project(ctx, args.project)
        what = {
            "current_tab": "the current tab",
            "tab_group": f'the "{args.which}" tab group',
            "matching_tabs": f'tabs matching "{args.which}"',
            "url": args.url,
        }[args.what]
        return f'add {what} to "{p.name}"'

    def run(self, args: SourceArgs, ctx: ToolContext) -> Result:
        p = _project(ctx, args.project)
        found = self._pick(args, ctx)
        kind: items.Kind = "link" if args.what == "url" else "tab"
        for t in found:
            items.add(
                ctx.conn, ctx.clock, p.id, kind, title=str(t.get("title") or ""),
                url=str(t.get("url")), body=args.note, grp=str(t.get("groupTitle") or ""),
                source="chat",
            )  # fmt: skip
        n = len(found)
        return Result(f'Added {n} source{"s" if n != 1 else ""} to "{p.name}"')


class NoteArgs(Args):
    project: str = Field("", max_length=80, description="empty: the inbox")
    text: str = Field(min_length=1, max_length=5000)


class ProjectNote(Tool):
    name = "project_note"
    summary = "save a quick note to a project (or to the inbox when no project is named)"
    tier = Tier.LOW
    args_model = NoteArgs

    def preview(self, args: NoteArgs, ctx: ToolContext) -> str:
        where = _project(ctx, args.project).name if args.project else "the inbox"
        return f'note for {where}: "{args.text[:80]}"'

    def run(self, args: NoteArgs, ctx: ToolContext) -> Result:
        p = _project(ctx, args.project) if args.project else None
        items.add(ctx.conn, ctx.clock, p.id if p else None, "note", title=args.text[:80],
                  body=args.text, source="chat")  # fmt: skip
        return Result(f"Noted{' for ' + p.name if p else ''}")


class TodoArgs(Args):
    text: str = Field(min_length=1, max_length=300)
    project: str = Field("", max_length=80, description="empty: the inbox")
    minutes: int | None = Field(None, ge=1, le=60 * 24 * 30, description="due in N minutes")
    at: str | None = Field(None, max_length=12, description="due at a time like 17:30 or 5pm")


class TodoAdd(Tool):
    name = "todo_add"
    summary = "add a to-do (optionally to a project, optionally due at a time)"
    tier = Tier.LOW
    args_model = TodoArgs

    def _due(self, args: TodoArgs, ctx: ToolContext) -> datetime | None:
        if args.minutes is None and not args.at:
            return None
        from personal_os.actions import reminders

        try:
            return reminders.due_time(ctx.clock.now(), args.minutes, args.at)
        except ValueError as exc:
            raise ActionError(str(exc)) from None

    def preview(self, args: TodoArgs, ctx: ToolContext) -> str:
        where = f" in {_project(ctx, args.project).name}" if args.project else ""
        return f'add the to-do "{args.text}"{where}'

    def run(self, args: TodoArgs, ctx: ToolContext) -> Result:
        p = _project(ctx, args.project) if args.project else None
        due = self._due(args, ctx)
        items.add(ctx.conn, ctx.clock, p.id if p else None, "todo", title=args.text, due=due,
                  source="chat")  # fmt: skip
        when = ""
        if due is not None:
            from personal_os.actions import reminders

            when = f", due {reminders.when(due, ctx.clock.now())}"
        return Result(f'Added the to-do "{args.text}"{" to " + p.name if p else ""}{when}')


class DoneArgs(Args):
    which: str = Field("", max_length=120, description="words from the to-do; empty: the newest")
    project: str = Field("", max_length=80)


class TodoDone(Tool):
    name = "todo_done"
    summary = "tick off a to-do, found by its words"
    tier = Tier.LOW
    reversible = True
    args_model = DoneArgs

    def _find(self, args: DoneArgs, ctx: ToolContext) -> items.Item:
        pid = _project(ctx, args.project).id if args.project else None
        t = items.find_todo(ctx.conn, args.which, pid)
        if t is None:
            raise ActionError(
                f'no open to-do matches "{args.which}"' if args.which else "no open to-dos"
            )
        return t

    def preview(self, args: DoneArgs, ctx: ToolContext) -> str:
        return f'tick off "{self._find(args, ctx).title}"'

    def run(self, args: DoneArgs, ctx: ToolContext) -> Result:
        t = self._find(args, ctx)
        items.set_done(ctx.conn, ctx.clock, t.id)
        return Result(
            f'Ticked off "{t.title}"', undo={"tool": "todo_reopen", "args": {"item_id": t.id}}
        )


class ReopenArgs(Args):
    item_id: str


class TodoReopen(Tool):
    name = "todo_reopen"
    summary = "(undo) mark a to-do as not done"
    tier = Tier.LOW
    internal = True
    args_model = ReopenArgs

    def preview(self, args: ReopenArgs, ctx: ToolContext) -> str:
        return "reopen a to-do"

    def run(self, args: ReopenArgs, ctx: ToolContext) -> Result:
        if not items.set_done(ctx.conn, ctx.clock, args.item_id, False):
            raise ActionError("that to-do is gone")
        return Result("Reopened the to-do")


class ItemsArgs(Args):
    project: str = Field(
        "", max_length=80, description="empty: the inbox, plus open to-dos everywhere"
    )
    show: Literal["all", "sources", "notes", "todos"] = "all"


class ProjectItems(Tool):
    name = "project_items"
    summary = "a project's sources (links, tabs), notes and to-dos; or the inbox"
    tier = Tier.READ
    args_model = ItemsArgs

    def preview(self, args: ItemsArgs, ctx: ToolContext) -> str:
        return f"list {args.show} for {args.project or 'the inbox'}"

    def run(self, args: ItemsArgs, ctx: ToolContext) -> Result:
        p = _project(ctx, args.project) if args.project else None
        kinds = {"all": ("link", "tab", "file", "note", "todo"), "sources": ("link", "tab", "file"),
                 "notes": ("note",), "todos": ("todo",)}[args.show]  # fmt: skip
        found = items.items(ctx.conn, p.id if p else None, kinds, open_only=args.show == "todos")
        lines = []
        for i in found:
            if i.kind == "todo":
                lines.append(f"- [{'x' if i.done else ' '}] {i.title}")
            elif i.kind == "note":
                lines.append(f"- note: {i.body[:300]}")
            else:
                lines.append(f"- {i.title or i.url}: {i.url}" + (f" ({i.body})" if i.body else ""))
        where = p.name if p else "the inbox"
        # Tab titles came from pages: captured content (taints what follows).
        return Result(
            f"{len(found)} item(s) in {where}",
            data="\n".join(lines) or "(nothing yet)",
            captured=True,
        )


class FileArgs(Args):
    project: str = PROJECT
    which: str = Field(
        "", max_length=80, description="words from the inbox item; empty: everything in the inbox"
    )


class ProjectFileInbox(Tool):
    name = "project_file_inbox"
    summary = "move things from the inbox (added from the browser) into a project"
    tier = Tier.LOW
    args_model = FileArgs

    def _pick(self, args: FileArgs, ctx: ToolContext) -> list[items.Item]:
        inbox = items.items(ctx.conn, None)
        w = args.which.lower().strip()
        return [i for i in inbox if not w or w in (i.title + " " + i.url + " " + i.body).lower()]

    def preview(self, args: FileArgs, ctx: ToolContext) -> str:
        n, name = len(self._pick(args, ctx)), _project(ctx, args.project).name
        return f'move {n} inbox item(s) to "{name}"'

    def run(self, args: FileArgs, ctx: ToolContext) -> Result:
        p = _project(ctx, args.project)
        moved = self._pick(args, ctx)
        if not moved:
            raise ActionError("nothing in the inbox matches")
        for i in moved:
            items.update(ctx.conn, ctx.clock, i.id, project_id=p.id)
            if i.url and i.kind in ("link", "tab"):
                items.learn_from(ctx.conn, ctx.clock, i.url, p.id)
        return Result(f'Moved {len(moved)} item(s) to "{p.name}"')


class ResumeArgs(Args):
    project: str = PROJECT


class ProjectResume(Tool):
    name = "project_resume"
    summary = (
        "pick a project back up: reopen its saved tabs and links as one named tab group, and "
        "say its latest note and open to-dos"
    )
    tier = Tier.LOW
    network = True
    args_model = ResumeArgs

    def preview(self, args: ResumeArgs, ctx: ToolContext) -> str:
        return f'reopen the sources of "{_project(ctx, args.project).name}"'

    def run(self, args: ResumeArgs, ctx: ToolContext) -> Result:
        from personal_os.browser import control

        p = _project(ctx, args.project)
        sources = [
            i for i in items.items(ctx.conn, p.id, ("link", "tab")) if i.url.startswith("http")
        ]
        opened = 0
        if sources:
            try:
                data = control.send(
                    ctx.config.runtime_dir, "tabs.open_group",
                    {"urls": [i.url for i in sources[:20]], "title": p.name},
                    browser=ctx.config.actions.browser,
                ) or {}  # fmt: skip
                opened = int(data.get("opened", 0))
            except (control.BrowserUnavailable, control.BrowserError) as exc:
                raise ActionError(str(exc)) from None
        notes = items.items(ctx.conn, p.id, ("note",), limit=1)
        todo = items.items(ctx.conn, p.id, ("todo",), open_only=True, limit=5)
        parts = [f'Reopened {opened} tab{"s" if opened != 1 else ""} for "{p.name}"' if opened
                 else f'"{p.name}" has no saved tabs yet']  # fmt: skip
        if notes:
            parts.append(f"Last note: {notes[0].body[:200]}")
        if todo:
            parts.append("Open to-dos: " + "; ".join(t.title for t in todo))
        return Result(". ".join(parts))
