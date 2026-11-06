"""Tier 0 tools that make Donna an assistant rather than a set of commands (docs/AGENT.md):
what's open right now, your history answered with evidence, your reminders and your facts."""

from __future__ import annotations

from pydantic import ConfigDict, Field

from personal_os.actions.base import Args, Result, Tier, Tool, ToolContext


class NoArgs(Args):
    model_config = ConfigDict(extra="ignore")  # a stray argument from the model is harmless


class Now(Tool):
    name = "now"
    summary = (
        "what's happening on this computer right now: the time, open windows and which is in "
        "front, open browser tabs, whether the user is idle, the next reminders"
    )
    tier = Tier.READ
    args_model = NoArgs

    def preview(self, args: NoArgs, ctx: ToolContext) -> str:
        return "look at what's open right now"

    def run(self, args: NoArgs, ctx: ToolContext) -> Result:
        from personal_os.context.now import snapshot

        snap = snapshot(ctx.config, ctx.conn)
        n = f"{len(snap.windows)} windows, {len(snap.tabs)} tabs"
        # Window and tab titles come from pages and apps: captured content (taints what follows).
        return Result(f"what's open now ({n})", data=snap.render(), captured=True)


class RecallArgs(Args):
    question: str = Field(min_length=3, max_length=300)


class Recall(Tool):
    name = "recall"
    summary = (
        "answer a question about the user's own past activity (what they worked on, pages, "
        "time spent, where they left off) from their history, with evidence"
    )
    tier = Tier.READ
    args_model = RecallArgs

    def preview(self, args: RecallArgs, ctx: ToolContext) -> str:
        return f"look through your history: {args.question}"

    def run(self, args: RecallArgs, ctx: ToolContext) -> Result:
        from personal_os.context.ask import ask

        answer = ask(ctx.conn, ctx.provider, args.question, ctx.clock.now())
        text = " ".join(s for s, _ in answer.sentences)
        evidence = "\n".join(x.render() for x in answer.cited())
        data = text + (f"\n\nEvidence:\n{evidence}" if evidence else "")
        return Result(text, data=data, captured=True)


class ListReminders(Tool):
    name = "list_reminders"
    summary = "the user's reminders: what's coming up and what went off in the last day"
    tier = Tier.READ
    args_model = NoArgs

    def preview(self, args: NoArgs, ctx: ToolContext) -> str:
        return "list your reminders"

    def run(self, args: NoArgs, ctx: ToolContext) -> Result:
        from personal_os.actions import reminders

        lines = reminders.answer(ctx.conn, ctx.clock.now())
        return Result(" ".join(lines), data="\n".join(lines))  # your own words: not captured


class Memory(Tool):
    name = "remembered_facts"
    summary = (
        "the facts the user explicitly asked you to remember about them (preferences, people); "
        "only when those matter to the request"
    )
    tier = Tier.READ
    args_model = NoArgs

    def preview(self, args: NoArgs, ctx: ToolContext) -> str:
        return "read what you asked me to remember"

    def run(self, args: NoArgs, ctx: ToolContext) -> Result:
        from personal_os.context import memory

        lines = memory.prompt_lines(ctx.conn)
        return Result(f"{len(lines)} fact(s)", data="\n".join(lines) or "(nothing remembered yet)")


class AboutMe(Tool):
    name = "about_me"
    summary = (
        "Donna's own state and settings: is the wake word on and which words, push-to-talk, "
        "her voice, the follow-up window, popup, which browser she controls, the local model, "
        "whether capture is running or paused"
    )
    tier = Tier.READ
    args_model = NoArgs

    def preview(self, args: NoArgs, ctx: ToolContext) -> str:
        return "check my own settings and status"

    def run(self, args: NoArgs, ctx: ToolContext) -> Result:
        from personal_os.browser import control
        from personal_os.core import runtime

        cfg = ctx.config
        v = cfg.voice
        lines = [f"name: {cfg.agent.name}"]
        try:
            from personal_os.voice import wake

            model = wake.available(cfg.data_dir)
        except Exception:
            model = False
        words = ", ".join(getattr(v, "wake_words", []) or [])
        if v.wake_word_enabled and model:
            lines.append(f"wake word: on ({words})")
        elif v.wake_word_enabled:
            lines.append(f"wake word: on in settings ({words}) but its model isn't installed")
        else:
            lines.append("wake word: off")
        lines.append("push-to-talk: Ctrl+Alt+Shift+V")
        voice = (
            v.piper_voice if v.tts == "piper" else v.windows_voice if v.tts == "windows" else v.tts
        )
        lines.append(f"voice: {v.tts} ({voice}); voice {'on' if v.enabled else 'off'}")
        lines.append(f"follow-up listening after a reply: {v.followup_s:g} s")
        lines.append(f"popup: {'on' if v.popup else 'off'}")
        browsers = control.connected(cfg.runtime_dir)
        lines.append(
            f"browser control: {', '.join(browsers)} connected" if browsers
            else "browser control: no browser connected"
        )  # fmt: skip
        lines.append(f"local model: {cfg.llm.fast_model if cfg.llm.enabled else 'off'}")
        info = runtime.daemon_info(cfg.runtime_dir)
        paused = (cfg.runtime_dir / "paused").exists()
        lines.append(f"capture: {'paused' if paused else 'running' if info.running else 'stopped'}")
        keys = (info.health or {}).get("hotkeys") or {}
        if keys:
            lines.append(
                "hotkeys: " + "; ".join(f"{k} {'ok' if ok else 'taken'}" for k, ok in keys.items())
            )
        return Result("my settings", data="\n".join(lines))


class ConvoArgs(Args):
    last: int = Field(10, ge=1, le=30)


class Conversation(Tool):
    name = "conversation"
    summary = (
        "your recent conversations with Donna (what you asked, what she answered, when): "
        "for 'what did we talk about' or 'what did you say earlier' (not the user's activity)"
    )
    tier = Tier.READ
    args_model = ConvoArgs

    def preview(self, args: ConvoArgs, ctx: ToolContext) -> str:
        return f"look at our last {args.last} exchanges"

    def run(self, args: ConvoArgs, ctx: ToolContext) -> Result:
        from personal_os.voice.main import history, local_time

        items = [e for e in history(ctx.config.runtime_dir, 60) if e.get("heard")][-args.last :]
        lines = [
            f"{local_time(str(e.get('at', '')), '%d %b %H:%M')} you: {e.get('heard')}"
            + (f" | Donna: {str(e.get('reply'))[:200]}" if e.get("reply") else "")
            for e in items
        ]
        # Her replies can quote pages and tabs: captured content, like any read.
        return Result(
            f"{len(lines)} exchange(s)", data="\n".join(lines) or "(none yet)", captured=True
        )
