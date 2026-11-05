"""Tier 1 tools added in phase 6: reminders and screen brightness."""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import Field

from personal_os.actions import reminders
from personal_os.actions.base import ActionError, Args, Result, Tier, Tool, ToolContext


class ReminderArgs(Args):
    text: str = Field(min_length=1, max_length=300)
    minutes: int | None = Field(None, ge=1, le=60 * 24 * 7)
    at: str | None = Field(None, max_length=12)  # "17:30", "5pm", "5 p.m."


def _due(minutes: int | None, at: str | None, ctx: ToolContext) -> datetime:
    try:
        return reminders.due_time(ctx.clock.now(), minutes, at)
    except ValueError as exc:
        raise ActionError(str(exc)) from None


class SetReminder(Tool):
    name = "set_reminder"
    summary = (
        "remind you of something in N minutes, or at a time like 17:30 or 5pm; text is what "
        "to remind about, in the user's words (never the request itself)"
    )
    tier = Tier.LOW
    reversible = True
    args_model = ReminderArgs

    def _check(self, args: ReminderArgs, ctx: ToolContext) -> datetime:
        if not reminders.is_real_message(args.text):
            raise ActionError("what should I remind you about?")
        if args.minutes is None and not args.at:
            raise ActionError("say when: in how many minutes, or at what time")
        return _due(args.minutes, args.at, ctx)

    def preview(self, args: ReminderArgs, ctx: ToolContext) -> str:
        return f"remind you {reminders.when(self._check(args, ctx), ctx.clock.now())}: {args.text}"

    def run(self, args: ReminderArgs, ctx: ToolContext) -> Result:
        due = self._check(args, ctx)
        rid = reminders.add(ctx.conn, ctx.clock, args.text, due, ctx.action_id)
        # Say back what was understood, so a wrong message or time is caught at once.
        return Result(
            f'I\'ll remind you {reminders.when(due, ctx.clock.now())}: "{args.text}"',
            undo={"tool": "cancel_reminder", "args": {"reminder_id": rid}},
        )


class UpdateArgs(Args):
    which: str = Field("", max_length=80, description="words from its text; empty = the latest")
    reminder_id: str = Field("", max_length=40)
    text: str | None = Field(None, min_length=1, max_length=300)
    minutes: int | None = Field(None, ge=1, le=60 * 24 * 7, description="due N minutes from now")
    at: str | None = Field(None, max_length=12)
    delay_minutes: int | None = Field(None, ge=1, le=60 * 24, description="push back by N minutes")
    due_iso: str | None = Field(None, max_length=40)  # undo only: the exact time it had


class UpdateReminder(Tool):
    name = "update_reminder"
    summary = (
        "change a reminder that hasn't gone off: its text and/or its time; by default the one "
        "set most recently, or the one whose text matches `which`"
    )
    tier = Tier.LOW
    reversible = True
    args_model = UpdateArgs

    def _target(self, args: UpdateArgs, ctx: ToolContext) -> reminders.Pending:
        found = reminders.find(ctx.conn, args.which, args.reminder_id)
        if found is None:
            if args.which:
                raise ActionError(f'there\'s no reminder coming up about "{args.which}"')
            raise ActionError("you have no reminder coming up to change")
        return found

    def _new_due(self, args: UpdateArgs, current: datetime, ctx: ToolContext) -> datetime | None:
        if args.due_iso:
            return datetime.fromisoformat(args.due_iso)
        if args.delay_minutes:
            return current + timedelta(minutes=args.delay_minutes)
        if args.minutes is not None or args.at:
            return _due(args.minutes, args.at, ctx)
        return None

    def preview(self, args: UpdateArgs, ctx: ToolContext) -> str:
        r = self._target(args, ctx)
        due = self._new_due(args, r.due, ctx) or r.due
        when = reminders.when(due, ctx.clock.now())
        return f'change reminder "{r.text}" to: "{args.text or r.text}" {when}'

    def run(self, args: UpdateArgs, ctx: ToolContext) -> Result:
        r = self._target(args, ctx)
        if args.text is not None and not reminders.is_real_message(args.text):
            raise ActionError("what should the reminder say?")
        due = self._new_due(args, r.due, ctx)
        if args.text is None and due is None:
            raise ActionError("say what to change: the text, or the time")
        if not reminders.update(ctx.conn, r.id, text=args.text, due=due):
            raise ActionError("that reminder already went off or was cancelled")
        text, when = args.text or r.text, reminders.when(due or r.due, ctx.clock.now())
        return Result(
            f'Changed. I\'ll remind you {when}: "{text}"',
            undo={
                "tool": "update_reminder",
                "args": {"reminder_id": r.id, "text": r.text, "due_iso": r.due.isoformat()},
            },
        )


class CancelArgs(Args):
    which: str = Field("", max_length=80, description="words from its text; empty = the latest")
    reminder_id: str = Field("", max_length=40)
    all: bool = False


class CancelReminder(Tool):
    name = "cancel_reminder"
    summary = "cancel a reminder that hasn't gone off: the latest, the one matching `which`, or all"
    tier = Tier.LOW
    args_model = CancelArgs

    def _targets(self, args: CancelArgs, ctx: ToolContext) -> list[reminders.Pending]:
        if args.all:
            found = reminders.pending_full(ctx.conn)
        else:
            one = reminders.find(ctx.conn, args.which, args.reminder_id)
            found = [one] if one is not None else []
        if not found:
            raise ActionError("there's no reminder coming up to cancel")
        return found

    def preview(self, args: CancelArgs, ctx: ToolContext) -> str:
        items = self._targets(args, ctx)
        return "cancel " + ", ".join(f'"{r.text}"' for r in items)

    def run(self, args: CancelArgs, ctx: ToolContext) -> Result:
        items = self._targets(args, ctx)
        done = [r for r in items if reminders.cancel(ctx.conn, r.id)]
        if not done:
            raise ActionError("that reminder already went off or was cancelled")
        if len(done) == 1:
            return Result(f'Cancelled the reminder "{done[0].text}".')
        return Result(f"Cancelled {len(done)} reminders.")


class BrightnessArgs(Args):
    percent: int = Field(ge=0, le=100)


class SetBrightness(Tool):
    name = "set_brightness"
    summary = "screen brightness only (how bright the display is, never sound), as a percentage"
    tier = Tier.LOW
    cues = ("bright", "dim", "screen", "display")  # "turn the volume up" isn't brightness
    reversible = True
    args_model = BrightnessArgs

    def preview(self, args: BrightnessArgs, ctx: ToolContext) -> str:
        return f"set screen brightness to {args.percent}%"

    def run(self, args: BrightnessArgs, ctx: ToolContext) -> Result:
        before = ctx.desktop.get_brightness()
        ctx.desktop.set_brightness(args.percent)
        undo = None if before is None else {"tool": "set_brightness", "args": {"percent": before}}
        return Result(f"brightness {args.percent}%", undo=undo)


class VolumeArgs(Args):
    percent: int = Field(ge=0, le=100)


class SetVolume(Tool):
    name = "set_volume"
    summary = "set the sound volume to a level from 0 to 100 (also unmutes)"
    tier = Tier.LOW
    cues = ("volume", "sound", "loud", "quiet", "audio")
    reversible = True
    args_model = VolumeArgs

    def preview(self, args: VolumeArgs, ctx: ToolContext) -> str:
        return f"set the volume to {args.percent}%"

    def run(self, args: VolumeArgs, ctx: ToolContext) -> Result:
        before = ctx.desktop.get_volume()
        ctx.desktop.set_volume(args.percent)
        undo = None if before is None else {"tool": "set_volume", "args": {"percent": before}}
        return Result(f"volume {args.percent}%", undo=undo)


class NoArgs(Args):
    pass


class LockScreen(Tool):
    name = "lock_screen"
    summary = "lock the computer (the Windows lock screen; nothing is closed)"
    tier = Tier.LOW
    cues = ("lock",)
    args_model = NoArgs

    def preview(self, args: NoArgs, ctx: ToolContext) -> str:
        return "lock the screen"

    def run(self, args: NoArgs, ctx: ToolContext) -> Result:
        ctx.desktop.lock_screen()
        return Result("Locked.")


class RevealArgs(Args):
    path: str


class RevealPath(Tool):
    name = "reveal_path"
    summary = "show a file or folder selected in File Explorer (doesn't open it)"
    tier = Tier.LOW
    reversible = True
    args_model = RevealArgs

    def preview(self, args: RevealArgs, ctx: ToolContext) -> str:
        return f"show {ctx.paths.check(ctx.paths.locate(args.path), must_exist=True)} in Explorer"

    def run(self, args: RevealArgs, ctx: ToolContext) -> Result:
        path = ctx.paths.check(ctx.paths.locate(args.path), must_exist=True)
        ctx.desktop.reveal(path)
        return Result(f"Showing {path.name}")
