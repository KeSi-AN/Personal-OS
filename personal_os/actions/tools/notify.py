"""Tier 1 tools added in phase 6: reminders and screen brightness."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from personal_os.actions import reminders
from personal_os.actions.base import ActionError, Args, Result, Tier, Tool, ToolContext


class ReminderArgs(Args):
    text: str = Field(min_length=1, max_length=300)
    minutes: int | None = Field(None, ge=1, le=60 * 24 * 7)
    at: str | None = Field(None, max_length=10)  # "17:30", "5pm"


class SetReminder(Tool):
    name = "set_reminder"
    summary = "remind you of something in N minutes, or at a time like 17:30 or 5pm"
    tier = Tier.LOW
    reversible = True
    args_model = ReminderArgs

    def _due(self, args: ReminderArgs, ctx: ToolContext) -> datetime:
        if args.minutes is None and not args.at:
            raise ActionError("say when: in how many minutes, or at what time")
        try:
            return reminders.due_time(ctx.clock.now(), args.minutes, args.at)
        except ValueError as exc:
            raise ActionError(str(exc)) from None

    def preview(self, args: ReminderArgs, ctx: ToolContext) -> str:
        return f"remind you at {self._due(args, ctx).astimezone():%a %H:%M}: {args.text}"

    def run(self, args: ReminderArgs, ctx: ToolContext) -> Result:
        due = self._due(args, ctx)
        rid = reminders.add(ctx.conn, ctx.clock, args.text, due, ctx.action_id)
        return Result(
            f"I'll remind you at {due.astimezone():%a %H:%M}",
            undo={"tool": "cancel_reminder", "args": {"reminder_id": rid}},
        )


class CancelArgs(Args):
    reminder_id: str


class CancelReminder(Tool):
    name = "cancel_reminder"
    summary = "(undo) cancel a reminder that hasn't fired yet"
    tier = Tier.LOW
    args_model = CancelArgs
    internal = True

    def preview(self, args: CancelArgs, ctx: ToolContext) -> str:
        return f"cancel reminder {args.reminder_id[-8:]}"

    def run(self, args: CancelArgs, ctx: ToolContext) -> Result:
        if not reminders.cancel(ctx.conn, args.reminder_id):
            raise ActionError("that reminder already fired or was cancelled")
        return Result("reminder cancelled")


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
