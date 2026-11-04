"""Tier 0 Google tools (read-only). What they return is captured content: it taints the request."""

from __future__ import annotations

from datetime import timedelta

from pydantic import Field

from personal_os.actions.base import ActionError, Args, Result, Tier, Tool, ToolContext


def _google(ctx: ToolContext):  # type: ignore[no-untyped-def]
    from personal_os.integrations import google

    if not google.connected(ctx.config.data_dir):
        raise ActionError("Google isn't connected (personal-os google connect)")
    return google.Google(ctx.config.data_dir)


class CalendarArgs(Args):
    days: int = Field(1, ge=1, le=14)


class CalendarToday(Tool):
    name = "calendar_today"
    summary = "your Google Calendar events for today (or the next few days)"
    tier = Tier.READ
    args_model = CalendarArgs

    def preview(self, args: CalendarArgs, ctx: ToolContext) -> str:
        _google(ctx)
        return f"read your calendar for {'today' if args.days == 1 else f'{args.days} days'}"

    def run(self, args: CalendarArgs, ctx: ToolContext) -> Result:
        from personal_os.integrations.google import GoogleError, today_range

        start, end = today_range(ctx.clock.now())
        try:
            events = _google(ctx).calendar(start, end + timedelta(days=args.days - 1))
        except GoogleError as exc:
            raise ActionError(str(exc)) from None
        lines = [
            ("all day" if e.all_day else f"{e.start.astimezone():%a %H:%M}" if e.start else "?")
            + f"  {e.title}" + (f" ({e.location})" if e.location else "")
            for e in events
        ]  # fmt: skip
        return Result(
            f"{len(lines)} event(s)", data="\n".join(lines) or "(no events)", captured=True
        )


class MailArgs(Args):
    top: int = Field(5, ge=1, le=20)


class MailUnread(Tool):
    name = "mail_unread"
    summary = "how many unread emails in your primary inbox, with the newest senders and subjects"
    tier = Tier.READ
    args_model = MailArgs

    def preview(self, args: MailArgs, ctx: ToolContext) -> str:
        _google(ctx)
        return f"read the senders and subjects of up to {args.top} unread emails (never the bodies)"

    def run(self, args: MailArgs, ctx: ToolContext) -> Result:
        from personal_os.integrations.google import GoogleError

        try:
            inbox = _google(ctx).inbox(top=args.top)
        except GoogleError as exc:
            raise ActionError(str(exc)) from None
        lines = [f"{m.sender}: {m.subject}" for m in inbox.top]
        return Result(f"{inbox.unread} unread", data="\n".join([f"{inbox.unread} unread", *lines]),
                      captured=True)  # fmt: skip
