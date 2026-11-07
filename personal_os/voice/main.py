"""The voice process (DESIGN.md §6.5): push-to-talk, optional wake word, spoken replies.

Started by the daemon when `voice.enabled` is on. One request at a time:

    earcon -> record (Silero VAD ends the turn) -> transcribe, biased to known names
      -> fast path: rules + fuzzy names (voice/router.py) -> tool, no LLM
           near miss: decode again biased to the candidates, else "Did you mean Brave?"
      -> otherwise: briefing / remember / ask / do (the LLM, loaded on demand)
      -> short reply, spoken sentence by sentence (Piper); speaking over it stops it

Anything that needs confirming is read out and needs a spoken yes. Audio is never stored.
The last exchange is written to runtime/voice_last.json for the dashboard.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

from personal_os.config.loader import load_config
from personal_os.config.schema import Config
from personal_os.core import runtime
from personal_os.core.clock import SystemClock
from personal_os.core.logging import get_logger, setup_logging
from personal_os.voice import dialogue, tts
from personal_os.voice.audio import MIC_MUTED, mic_muted
from personal_os.voice.stt import make_transcriber

_log = get_logger("voice")
LOCK = "voice.lock"
STATUS = "voice.json"
LAST = "voice_last.json"
PTT_ID = 0x5056  # "PV"
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
VK_V = 0x56
WM_HOTKEY = 0x0312
YES = re.compile(r"\b(yes|yeah|yep|yup|sure|confirm(ed)?|go ahead|do it|ok(ay)?|please do)\b", re.I)
NO = re.compile(r"\b(no|nope|don't|do not|cancel|stop|wait)\b", re.I)


def is_yes(text: str) -> bool:
    return bool(YES.search(text)) and not NO.search(text)


class Voice:
    def __init__(self, cfg: Config) -> None:
        from personal_os.voice.index import LiveIndex
        from personal_os.voice.speech import Speaker

        self.cfg = cfg
        self.clock = SystemClock()
        self.speaker = Speaker(cfg.voice, cfg.data_dir)
        self.stt = make_transcriber(cfg.data_dir, cfg.voice.stt_model)
        fast = make_transcriber(cfg.data_dir, cfg.voice.stt_fast_model or cfg.voice.stt_model)
        # Heard first; the accurate model re-hears only what the fast path can't place.
        self.stt_fast = fast if cfg.voice.stt_fast_model and fast.available else self.stt
        self.engine: Any = None  # the always-on, echo-cancelled mic (voice/engine.py)
        self.recording = threading.Event()  # set while a turn is being recorded
        self.index = LiveIndex(cfg, cfg.db_path)
        self.last_audio: Any = None  # the last utterance, for a biased second decode
        self._accurate: tuple[int, str] | None = None  # (id of the audio, small.en's text)
        self.barge_frames: list[Any] = []  # what was heard while interrupting a reply
        self.turn_trigger = ""  # what started the current turn: hotkey, wake or barge
        self.asked: dict[str, str] = {}  # a "Did you mean ...?" and its answer, for the log
        self.triggers: queue.Queue[str] = queue.Queue()
        self.previous: Any = None
        self.wake: Any = None  # the wake word (Vosk, as in JARVIS: voice/wake.py)
        self.stop = threading.Event()
        self.hotkey_error: str | None = None  # kept, so "ready" writes don't hide it
        self.state = "starting"
        self.turn: dict[str, Any] | None = None  # the current or last exchange, for the popup
        # The conversation so far (from the voice history, so it survives a restart).
        self.convo = dialogue.Conversation.from_history(cfg.runtime_dir)
        # A request to run next, without listening: "no, at 5 pm instead" or "I wanted X".
        self.next_request: str | None = None
        self.announced = ""  # said before an action ran: not said again after
        # The current turn's stop signal: set by the hotkey or "Donna" while thinking or
        # speaking (then a new turn listens), by "stop talking" and the popup's Stop / Esc
        # (then nothing more happens), and by talking over a reply. Listening, the model call
        # and playback all give up when it's set.
        self.interrupt = threading.Event()
        self.stopped_by_you = False  # this turn was stopped on purpose, not cut by a new one
        self.unanswered = False  # a yes-or-no question got no answer at all this turn
        self.said_last = ""  # the last thing she said aloud
        from personal_os.voice.duck import Ducker

        # Music and videos turned down for a conversation, so she hears you over them.
        self.ducker = Ducker(cfg.runtime_dir, cfg.voice.duck)

    # --- status ---------------------------------------------------------------------------

    def write_status(self, state: str, **extra: Any) -> None:
        from personal_os.voice import wake

        self.state = state
        data = {
            "state": state,
            "pid": __import__("os").getpid(),
            "wake_enabled": self.cfg.voice.wake_word_enabled,
            "wake_model": wake.available(self.cfg.data_dir),
            "tts": self.cfg.voice.tts,
            "last_voice": self.speaker.last_voice,
            "stt_ready": self.stt.available,
            "stt_first": self.stt_fast.name,
            "wake_listening": self.wake is not None,
            **({"audio": self.engine.describe()} if self.engine is not None else {}),
            **({"wake_last": self.wake.last} if self.wake is not None and self.wake.last else {}),
            **({"error": self.hotkey_error} if self.hotkey_error else {}),
            **extra,
        }
        if self.turn is not None:
            data["turn"] = dict(self.turn)
        try:
            runtime.write_json_atomic(self.cfg.runtime_dir / STATUS, data)
        except OSError as exc:  # a status that can't be written never stops a turn
            _log.warning("voice status not written", extra={"error": type(exc).__name__})

    def turn_update(self, **fields: Any) -> None:
        """Change the current turn and publish it at once (the popup polls the status file)."""
        if self.turn is None:
            return
        self.turn.update(fields)
        self.write_status(self.state)

    # --- listening ------------------------------------------------------------------------------

    def hotkey_loop(self, registered: threading.Event) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        ok = user32.RegisterHotKey(
            None, PTT_ID, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, VK_V
        )
        if not ok:
            self.hotkey_error = "Ctrl+Alt+Shift+V is taken by another app"
            _log.warning("push-to-talk hotkey taken")
        registered.set()
        if not ok:
            return
        msg = wintypes.MSG()
        try:
            while not self.stop.is_set() and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == PTT_ID:
                    self.pressed("hotkey")
        finally:
            user32.UnregisterHotKey(None, PTT_ID)

    def pressed(self, trigger: str) -> None:
        """A press (or the wake word). While listening it's ignored (you're already being
        heard). While thinking or speaking it cuts the reply short and starts a new listen.
        At most one press waits, so presses never pile up and fire later."""
        if self.state == "listening":
            return
        if self.state in ("thinking", "speaking"):
            self.interrupt.set()
        if self.triggers.empty():
            self.triggers.put(trigger)

    def start_engine(self) -> None:
        """The always-on mic with echo cancelling, for the wake word, barge-in and recording.
        If it can't start (no microphone), voice falls back to opening the mic per turn."""
        from personal_os.voice.engine import AudioEngine

        try:
            engine = AudioEngine(echo_cancel=self.cfg.voice.echo_cancel)
            engine.start()
            self.engine = engine
            _log.info("audio engine", extra={"fields": engine.describe()})
        except Exception as exc:
            self.engine = None
            _log.warning("audio engine not started", extra={"fields": {"error": str(exc)[:200]}})

    def _woke(self, preroll: list[Any]) -> None:
        """The wake word was heard: listen, starting from the beginning of that sentence, so
        "Donna, open Brave" in one breath keeps "open Brave". Mid-reply, it stops the reply."""
        self.barge_frames = preroll
        self.pressed("wake")

    def _stop_talking(self) -> None:
        """ "Stop talking" / "be quiet" while she's thinking or talking: stop, don't listen."""
        self.cancel_turn()

    def cancel_turn(self) -> None:
        """Stop this turn wherever it is (listening, thinking, speaking): the model call is
        hung up, playback stops, nothing queued runs, and no follow-up is listened for."""
        self.stopped_by_you = True
        self.next_request = None
        self.interrupt.set()

    def watch_stop(self) -> None:
        """The popup's Stop button and Esc: a request file in the runtime folder."""
        while not self.stop.is_set():
            if take_stop(self.cfg.runtime_dir) and self.state in ACTIVE:
                _log.info("stopped from the popup")
                self.cancel_turn()
            self.stop.wait(0.15)

    def sync_wake(self) -> None:
        """Start or stop the wake word (Vosk, as in JARVIS) to match the (re-read) config."""
        from personal_os.voice import wake

        v = self.cfg.voice
        want = v.wake_word_enabled and wake.available(self.cfg.data_dir)
        if want and self.wake is None:
            try:
                self.wake = wake.VoskWake(
                    self.cfg.data_dir, list(v.wake_words), on_wake=self._woke,
                    on_stop=self._stop_talking, busy=self.recording.is_set,
                    speaking=lambda: self.state in ("speaking", "thinking"), engine=self.engine,
                )  # fmt: skip
                self.wake.start()
            except Exception as exc:
                self.wake = None
                _log.warning("wake word not started", extra={"fields": {"error": str(exc)[:200]}})
        elif not want and self.wake is not None:
            self.wake.close()
            self.wake = None

    def listen(self, preroll: list[Any] | None = None, start_timeout_s: float | None = None) -> str:
        """Record one turn and transcribe it. If what was said is clearly unfinished ("open
        ..."), keep listening a moment for the rest instead of acting on half a sentence."""
        import numpy as np

        from personal_os.actions.reminder_parse import needs_more
        from personal_os.context.route import incomplete
        from personal_os.voice.audio import START_TIMEOUT_S, record_from_engine, record_utterance

        v = self.cfg.voice

        def stopped() -> bool:  # voice shutting down, or this turn stopped (Esc, "stop")
            return self.stop.is_set() or self.interrupt.is_set()

        def record(first: Any, timeout: float) -> Any:
            if self.engine is not None:  # echo-cancelled: a reply still fading isn't you
                return record_from_engine(
                    self.engine, stopped, floor=v.mic_floor,
                    silence_ms=v.vad_silence_ms, preroll=first or (), start_timeout_s=timeout,
                )  # fmt: skip
            return record_utterance(
                stopped, floor=v.mic_floor, silence_ms=v.vad_silence_ms,
                preroll=first or (), start_timeout_s=timeout,
            )  # fmt: skip

        self.recording.set()  # the wake word rests while a request is recorded
        try:
            audio = record(preroll, start_timeout_s or START_TIMEOUT_S)
            if audio is None:
                self.last_audio = None
                return ""
            text = self.transcribe(audio)
            # Cut off ("open the-"), or a reminder whose message hasn't come yet ("remind me
            # in a minute ... to stretch"): a pause isn't the end, so listen a moment more.
            if incomplete(text) or needs_more(text):
                more = record(None, 2.0)
                if more is not None:
                    audio = np.concatenate([audio, more])
                    text = self.transcribe(audio)
        finally:
            self.recording.clear()
        self.last_audio = audio
        return text

    def transcribe(self, audio: Any, *, accurate: bool = False) -> str:
        model = self.stt if accurate else self.stt_fast
        if not self.cfg.voice.stt_hints:  # as in JARVIS: no prompt, just English
            return model.transcribe(audio)
        names = self.index.current().names_for_hints(40)
        return model.transcribe(
            audio, vocabulary=self.vocabulary(names), hotwords=", ".join(names[:25])
        )

    def accurate_text(self) -> str | None:
        """The last utterance as the accurate model hears it (once per utterance), or None
        when there's no audio or the fast model already is the accurate one."""
        if self.last_audio is None or self.stt_fast is self.stt:
            return None
        key = id(self.last_audio)
        if self._accurate is None or self._accurate[0] != key:
            self._accurate = (key, self.transcribe(self.last_audio, accurate=True))
        return self._accurate[1]

    def vocabulary(self, names: list[str] | None = None) -> str:
        """A prompt of names that exist here, so "open brave" isn't heard as "open brat"."""
        if names is None:
            names = self.index.current().names_for_hints(40)
        return "Donna. Open " + ", ".join(names) + "."

    # --- handling --------------------------------------------------------------------------------

    def say(self, text: str) -> None:
        """Speak, stoppable: by the hotkey, "Donna", "stop talking", the popup, or by talking
        over it (voice.barge_in = voice). A reply to a turn that itself began by talking over
        a reply isn't stopped by just any speech (the wake word and the rest still work), so
        her own voice leaking past the echo canceller can never keep her answering herself
        (seen: "I'm here to help" heard back as "I'm here", answered, heard again...)."""
        self.said_last = text  # what a "barge-in" is compared with: was it her own voice?
        try:
            clean = self.engine is not None and self.engine.aec_active
            by_voice = self.cfg.voice.barge_in == "voice" and self.turn_trigger != "barge"
            if by_voice and clean:
                from personal_os.voice.bargein import CleanBargeIn

                with CleanBargeIn(
                    self.engine, self._barged, floor=self.cfg.voice.mic_floor,
                    playing=self.speaker.player.playing.is_set,
                ):  # fmt: skip
                    self.speaker.say(text, interrupt=self.interrupt)
            elif by_voice:
                from personal_os.voice.bargein import BargeIn

                with BargeIn(self.speaker.player, self._barged, floor=self.cfg.voice.mic_floor):
                    self.speaker.say(text, interrupt=self.interrupt)
            else:
                self.speaker.say(text, interrupt=self.interrupt)
        except tts.TTSError:
            _log.warning("no voice could speak")

    def _barged(self, frames: list[Any]) -> None:
        """You spoke over a reply: stop it and listen, starting from what was already heard."""
        self.barge_frames = frames
        self.interrupt.set()
        if self.triggers.empty():
            self.triggers.put("barge")

    # --- fast path ---------------------------------------------------------------------------

    def fast(self, text: str) -> str | None:
        """A command the router can run without the LLM; the reply to say, or None."""
        from personal_os.voice import router

        reminder = self.reminder(text)
        if reminder is not None:
            return reminder
        threshold = self.cfg.voice.router_threshold
        intent = router.route(text, self.index.current(), threshold=threshold)
        if intent is None or not intent.sure:
            # The fast model may have misheard ("follow you up"): the accurate one re-hears it.
            better = self.accurate_text()
            if better and better != text:
                again = router.route(better, self.index.current(), threshold=threshold)
                if again is not None and (again.sure or intent is None):
                    intent = again
        if intent is None:
            return None
        if not intent.sure and self.last_audio is not None:
            # A near miss ("open brat"): decode the same audio again, biased to the candidates.
            phrases = [intent.phrase(g.entry) for g in intent.guesses]
            rechecked = self.stt.recheck(self.last_audio, phrases)
            second = router.route(rechecked, self.index.current(), threshold=threshold)
            if second is not None and router.confirms(second, intent):
                intent = second
        asks = 0
        while not intent.sure:
            if asks == dialogue.MAX_ASKS:
                return "I still couldn't tell which one, so I left it."
            asks += 1
            answer = self.ask_aloud(intent.question)
            self.asked = {"question": intent.question, "answer": answer or "(nothing)"}
            if not answer:
                return "I didn't hear an answer, so I left it."
            reading = dialogue.understand(
                answer, intent, self.index.current(), threshold=threshold,
                classify=self.classifier(),
            )  # fmt: skip
            self.asked["understood"] = reading.kind + (" (model)" if reading.by_model else "")
            if reading.kind in ("choose", "correct") and reading.intent is not None:
                intent = reading.intent  # may itself be unsure: then it asks about that
            elif reading.kind == "new":
                self.next_request = reading.text  # handled right after, as its own request
                return ""
            elif reading.kind == "cancel":
                return "Okay, I'll leave it."
            else:
                intent = router.Intent(
                    [], "", guesses=intent.guesses, verb=intent.verb, build=intent.build,
                    question="Sorry, which one? " + intent.question,
                )  # fmt: skip
        return self.run_intent(intent, text)

    def ask_aloud(self, question: str, *, shown: str | None = None) -> str:
        """Ask (on screen as it's said), then beep, so it's clear it's waiting for you."""
        shown = shown or question
        self.show("speaking", question=shown)
        self.say(question)
        if self.interrupt.is_set():
            return ""
        self.show("listening", question=shown)
        self.speaker.earcon("listen")
        answer = self.listen()
        self.show("thinking", question="")
        return answer

    def show(self, state: str, **fields: Any) -> None:
        """Put the turn in `state` with these fields, on screen at once."""
        if self.turn is not None:
            self.turn.update(fields, state=state)
        self.write_status(state)

    def classifier(self) -> Any:
        """The local model, for answers the rules can't place (None if it's off)."""
        provider = self.provider()
        if provider is None:
            return None
        return dialogue.model_classifier(
            provider, self.index.current(), threshold=self.cfg.voice.router_threshold
        )

    def wait_for_popup(self, reply: str) -> None:
        """Speak only once the popup shows this reply (at most a moment: a slow or missing
        popup never holds voice up for long)."""
        if not self.cfg.voice.popup or self.turn is None:
            return
        from personal_os.interfaces import popup

        rt = self.cfg.runtime_dir
        if popup.is_open(rt):
            limit = 0.6
        elif popup.recently_launched(rt):
            limit = 2.0  # still starting up: worth a little wait on the first turn
        else:
            return
        key = popup.reply_key(str(self.turn.get("id", "")), reply)
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if (runtime.read_json(rt / popup.SEEN) or {}).get("key") == key:
                return
            time.sleep(0.04)

    def announce(self, text: str) -> None:
        """Say what's about to happen ("Playing Circles."), shown first, before doing it."""
        if not text or self.turn is None:
            return
        self.turn.update(reply=text, state="speaking")
        self.write_status("speaking")
        self.wait_for_popup(text)
        self.say(text)
        self.announced = text
        self.turn["state"] = "thinking"
        self.write_status("thinking")

    def media_in_browser(self, intent: Any, text: str) -> str | None:
        """Play, pause, stop, next, previous: the music in your browser tab when the browser
        is connected (the Windows media key toggles whichever player Windows picks). None to
        use the media key instead (no browser, or nothing playable in it)."""
        if len(intent.calls) != 1 or intent.calls[0][0] != "media":
            return None
        key = intent.calls[0][1].get("action")
        action = {"play_pause": intent.verb if intent.verb in ("play", "pause") else "toggle",
                  "next": "next", "previous": "previous"}.get(key)  # fmt: skip
        if action is None:
            return None
        from personal_os.browser import control

        if not control.connected(self.cfg.runtime_dir):
            return None
        said = {"play": "Playing.", "pause": "Pausing.", "toggle": "Okay.", "next": "Next.",
                "previous": "Previous."}[action]  # fmt: skip
        self.announce(said)
        reply = self.run_tool("browser_media", {"action": action}, text, ack=False)
        return None if reply.startswith("I couldn't") else said

    def run_intent(self, intent: Any, text: str) -> str:
        from personal_os.actions.base import Origin
        from personal_os.storage.db import open_db

        if not intent.calls:
            return str(intent.reply)
        tab_reply = self.media_in_browser(intent, text)
        if tab_reply is not None:
            return tab_reply
        if intent.reply and intent.reply != self.announced:
            self.announce(str(intent.reply))  # "Opening Brave." before it opens
        else:
            self.speaker.earcon("ack")  # heard and understood, before the tool runs
        conn = open_db(self.cfg.db_path)
        try:
            ex = self.executor(conn)
            outcomes = [
                ex.request(tool, args, origin=Origin.USER, request_text=text)
                for tool, args in intent.calls
            ]
        finally:
            conn.close()
        failed = [o for o in outcomes if not o.ok]
        if not failed:
            return str(intent.reply)
        why = str(failed[0].message).split(":")[-1].strip().rstrip(".")
        return f"I couldn't. {why}."

    def executor(self, conn: Any, provider: Any = None) -> Any:
        from personal_os.actions.desktop import default_desktop
        from personal_os.actions.executor import Executor
        from personal_os.actions.tools import build_registry

        made: list[Any] = []
        ex = Executor(
            self.cfg, conn, self.clock, build_registry(self.cfg), default_desktop(),
            lambda prompt: self.confirm(prompt, made[0] if made else None), provider=provider,
        )  # fmt: skip
        made.append(ex)
        ex.stop_signal = self.interrupt  # "stop" stops a running action too (a slow browser)
        return ex

    def confirm(self, prompt: str, ex: Any = None) -> bool:
        """A spoken yes or no before an action, asked in plain words ("Open VS Code?") and
        shown in the popup while it's asked."""
        if self.interrupt.is_set():
            return False
        pending = getattr(ex, "pending", None)
        question = confirm_question(pending) if pending else question_from_prompt(prompt)
        shown = question
        if pending and pending.get("tainted"):
            shown += " (This comes from earlier in the conversation, or something read.)"
        answer = self.ask_aloud(f"{question} Say yes or no.", shown=shown)
        if self.interrupt.is_set():
            return False
        if not answer.strip():
            # Silence isn't a "no": say so, rather than report "you declined" afterwards.
            self.unanswered = True
            return False
        verdict, change = dialogue.yes_no(answer)
        if verdict == "amend":
            self.next_request = change
            self.say("Okay, changing it.")
            return False
        self.say("Okay." if verdict == "yes" else "Not doing it.")
        return verdict == "yes"

    def reminder(self, text: str) -> str | None:
        """Set, change or cancel a reminder by rules (actions/reminder_parse.py), asking for
        a missing message or time instead of guessing. None if this isn't about reminders."""
        from personal_os.actions import reminder_parse

        recent = any(
            "remind" in x.heard.lower() or "remind" in x.reply.lower()
            for x in self.convo.recent()[-2:]
        )
        r = reminder_parse.parse(text, allow_pronoun=recent)
        if r is None:
            return None
        if r.missing == "what":
            answer = self.ask_aloud("What should I remind you about?")
            r.text = reminder_parse.message_from(answer)
            if not r.text:
                return "Okay, no reminder."
        if r.missing == "when":
            answer = self.ask_aloud(f"When should I remind you to {r.text}?")
            r.minutes, r.at = reminder_parse.time_from(answer)
            if r.missing:
                return "I didn't catch a time, so I didn't set it."
        return self.run_tool(r.tool, r.args(), text)

    def run_tool(self, tool: str, args: dict[str, Any], text: str, *, ack: bool = True) -> str:
        """One tool you asked for directly, through the executor (policy, audit log, undo);
        the reply is what it did, in its own words."""
        from personal_os.actions.base import Origin
        from personal_os.storage.db import open_db

        if ack:
            self.speaker.earcon("ack")
        conn = open_db(self.cfg.db_path)
        try:
            out = self.executor(conn).request(tool, args, origin=Origin.USER, request_text=text)
        finally:
            conn.close()
        if out.ok:
            return str(out.message).rstrip(".") + "."
        why = str(out.message).split(":")[-1].strip().rstrip(".?")
        return f"I couldn't: {why}."

    def handle(self, text: str) -> str:
        from personal_os.context.route import (
            about_my_past,
            chat_reply,
            evening,
            normalize,
            route,
            strip_polite,
        )
        from personal_os.storage.db import open_db

        kind = route(text)
        request = normalize(text)
        if kind == "stop":
            tts.stop()
            return ""
        if kind == "chat":
            return chat_reply(text)
        if kind == "do":
            request = strip_polite(request)
        conn = open_db(self.cfg.db_path)
        try:
            if kind == "briefing":
                from personal_os.context import briefing

                b = briefing.build(
                    conn,
                    self.clock,
                    "evening" if evening(request) else "morning",
                    google=google_client(self.cfg),
                )
                return f"{b.title}. " + " ".join(b.lines)
            if kind == "remember":
                from personal_os.context import memory

                fact = memory.parse_remember(request) or request
                self.say(f"Remember that {fact}? Say yes or no.")
                verdict, change = dialogue.yes_no(self.listen())
                if verdict == "yes":
                    memory.add(conn, self.clock, fact, "voice")
                    return "Remembered."
                if verdict == "amend":
                    self.next_request = change
                    return ""
                return "I won't remember it."
            if kind == "ask" and about_my_past(request):
                return self.recall(conn, request)  # the same answer `personal-os ask` gives
            if self.cfg.agent.enabled and self.provider() is not None:
                return self.agent(conn, request)  # conversation, questions and actions alike
            if kind == "do":
                return self.do(conn, request)
            from personal_os.context.ask import ask

            if not self.convo.active():
                self.previous = None  # "and yesterday?" means nothing after a long gap
            answer = ask(conn, self.provider(), request, self.clock.now(), previous=self.previous)
            self.previous = answer.question
            self.save_last(request, answer.text, [x.render() for x in answer.cited()])
            return " ".join(s for s, _ in answer.sentences)
        finally:
            conn.close()

    def recall(self, conn: Any, request: str) -> str:
        """A question about your own history ("what did I do today?"): answered from your
        activity with evidence, exactly as `personal-os ask` answers it, with the evidence in
        the popup. The agent once answered this from what's open now instead."""
        from personal_os.context.ask import ask

        if not self.convo.active():
            self.previous = None  # "and yesterday?" means nothing after a long gap
        answer = ask(conn, self.provider(), request, self.clock.now(), previous=self.previous)
        self.previous = answer.question
        evidence = [x.render() for x in answer.cited()]
        self.save_last(request, answer.text, evidence)
        if self.turn is not None and evidence:
            self.turn["details"] = "\n".join(evidence)[:4000]
        return " ".join(s for s, _ in answer.sentences)

    def agent(self, conn: Any, request: str) -> str:
        """Donna as one assistant (docs/AGENT.md): she decides whether to answer, look
        something up or act, using the same tools and policy as everything else."""
        from personal_os.actions import agent

        provider = self.provider()
        ex = self.executor(conn, provider)
        context = self.convo.for_planner()
        result = agent.respond(
            request, ex, provider, context=context, announce=self.announce,
            cancelled=self.interrupt.is_set, progress=lambda note: self.turn_update(progress=note),
            max_steps=min(self.cfg.agent.max_steps, VOICE_MAX_STEPS),
        )  # fmt: skip
        if self.turn is not None and result.details:
            self.turn["details"] = result.details  # the popup shows it in full
        acted = [o for o in result.outcomes if o.tool in agent.ANNOUNCED]
        if self.announced and acted and all(o.ok for o in acted):
            self.announced = result.reply  # said up front; the popup shows the full reply
            self.turn_update(reply=result.reply)
        return result.reply

    def do(self, conn: Any, request: str) -> str:
        from personal_os.actions.planner import run_request

        provider = self.provider()
        if provider is None:
            return "The local model is turned off, so I can't plan actions."
        ex = self.executor(conn, provider)
        # The conversation only when this request points back at it ("it", "instead"): a
        # self-contained request is planned on its own words alone.
        context = self.convo.for_planner()
        report = run_request(request, ex, provider, context=context)
        if report.error:
            return "I couldn't do that. " + (
                "The kill switch is on." if "kill" in report.error else ""
            )
        done = [o for o in report.outcomes if o.ok]
        if not report.outcomes:
            return report.reply or "There's nothing I can do for that."
        if len(done) == len(report.outcomes):
            # Say what actually happened ("I'll remind you at Wed 16:34"), not just "done".
            return " ".join(o.message.rstrip(".") + "." for o in done)
        return f"Done {len(done)} of {len(report.outcomes)} steps."

    def provider(self) -> Any:
        if not self.cfg.llm.enabled:
            return None
        from personal_os.llm import make_provider

        provider = make_provider(self.cfg.llm)
        with contextlib.suppress(AttributeError):
            # "stop" hangs up a model call mid-answer (Ollama's client streams then).
            provider.cancel = self.interrupt  # type: ignore[attr-defined]
        return provider

    def save_last(self, question: str, answer: str, evidence: list[str]) -> None:
        runtime.write_json_atomic(
            self.cfg.runtime_dir / LAST,
            {
                "at": self.clock.now().isoformat(),
                "question": question,
                "answer": answer,
                "evidence": evidence[:12],
            },
        )

    # --- main loop ---------------------------------------------------------------------------

    def run(self) -> None:
        registered = threading.Event()
        threading.Thread(
            target=self.hotkey_loop, args=(registered,), name="pos-ptt", daemon=True
        ).start()
        registered.wait(5)  # "ready" only once the hotkey is (or failed to be) registered
        self.start_engine()
        self.ducker.recover()  # volumes a crash left turned down
        threading.Thread(target=self.warm, name="pos-warm", daemon=True).start()
        self.sync_wake()
        threading.Thread(target=self.watch_stop, name="pos-stop", daemon=True).start()
        self.write_status("ready")
        _log.info("voice ready", extra={"fields": {"hotkey": self.hotkey_error is None}})
        last_reload = time.monotonic()
        while not self.stop.is_set():
            try:
                trigger = self.triggers.get(timeout=1.0)
            except queue.Empty:
                self.speak_queued()
                if time.monotonic() - last_reload > 10:
                    last_reload = time.monotonic()
                    self.cfg = load_config()
                    if not self.cfg.voice.enabled or runtime.stop_requested(self.cfg.runtime_dir):
                        break
                    if self.engine is not None and self.engine.output_changed():
                        self.restart_audio()  # TV to headphones: cancel the new device
                    self.sync_wake()
                    self.stt.unload_if_idle()
                    self.stt_fast.unload_if_idle()
                    self.write_status("ready")
                continue
            while not self.triggers.empty():  # presses made while busy count once
                self.triggers.get_nowait()
            self.converse(trigger)
        self.shutdown()

    def restart_audio(self) -> None:
        """The default output changed: start the engine (and the wake word on it) again."""
        _log.info("output device changed: restarting the audio engine")
        self.shutdown()
        self.start_engine()
        self.sync_wake()

    def shutdown(self) -> None:
        if self.wake is not None:
            with contextlib.suppress(Exception):
                self.wake.close()
            self.wake = None
        if self.engine is not None:
            with contextlib.suppress(Exception):
                self.engine.stop()
            self.engine = None

    def speak_queued(self) -> None:
        """Say what the daemon queued (a reminder, a briefing), while nothing else is going on."""
        for text in take_speech(self.cfg.runtime_dir):
            self.interrupt.clear()  # a stopped turn earlier mustn't silence a reminder now
            self.stopped_by_you = False
            self.turn_trigger = "queued"
            self.write_status("speaking")
            self.say(text)
            self.write_status("ready")

    def converse(self, trigger: str) -> None:
        """One turn, then any follow-ups: a request it was told to make next ("no, at 5 pm
        instead"), and whatever you say in the few seconds after a reply, without the hotkey.
        "Thanks" or silence ends it; so does a press (that starts a fresh turn). Other apps'
        sound is turned down for all of it."""
        self.ducker.level = self.cfg.voice.duck  # the config is re-read every 10 s
        with self.ducker.turn():
            self._converse(trigger)

    def _converse(self, trigger: str) -> None:
        more = self.respond(trigger)
        for _ in range(MAX_FOLLOWUPS):
            if self.stop.is_set() or not self.triggers.empty():
                return
            if self.next_request:
                text, self.next_request = self.next_request, None
                more = self.respond("followup", text=text)
                continue
            if not more or self.cfg.voice.followup_s <= 0:
                return
            more = self.respond("followup")

    def launch_popup(self) -> None:
        """Show the voice popup for this turn, off-thread: recording never waits on it, and a
        popup that fails to start never affects voice (the error is kept for `doctor`)."""
        if not self.cfg.voice.popup:
            return
        threading.Thread(
            target=_launch_popup, args=(self.cfg,), name="pos-popup", daemon=True
        ).start()

    def warm(self) -> None:
        """Load speech in and out and build the index now, so the first command is fast."""
        for step in (self.index.refresh, self.stt_fast.warm, self.speaker.warm, self.stt.warm):
            try:
                step()
            except Exception as exc:
                _log.warning(
                    "warm-up step failed", extra={"fields": {"exc_type": type(exc).__name__}}
                )

    def warm_stt(self) -> None:
        self.stt_fast.warm()
        self.stt.warm()

    def respond(self, trigger: str, text: str | None = None) -> bool:
        """One exchange. `text`: a request to handle without listening. Returns True when a
        reply was spoken in full, so a follow-up can be listened for."""
        from personal_os.context.route import incomplete, route

        entry: dict[str, Any] = {"at": self.clock.now().isoformat(), "trigger": trigger}
        t0 = time.monotonic()
        self.interrupt.clear()
        self.stopped_by_you = False
        self.unanswered = False
        self.turn_trigger = trigger
        preroll, self.barge_frames = self.barge_frames, []
        # If speech recognition was unloaded while idle, load it while you're still talking.
        threading.Thread(target=self.warm_stt, name="pos-stt-warm", daemon=True).start()
        self.turn = {
            "id": f"{time.time_ns():x}", "trigger": trigger, "state": "listening",
            "started_at": entry["at"], "heard": "", "reply": "", "question": "",
        }  # fmt: skip
        self.write_status("listening", trigger=trigger)
        self.launch_popup()
        spoke = ended = False
        given = text is not None
        try:
            if given:
                pass  # a request already made: "no, at 5 pm instead"
            elif trigger == "hotkey" and mic_muted():
                # A muted mic records silence: say so, rather than "nothing heard".
                entry["route"] = "mic muted"
                entry["reply"] = MIC_MUTED
                self.turn_update(reply=MIC_MUTED, state="speaking")
                self.say(MIC_MUTED)
                return False
            elif trigger == "followup":
                self.speaker.earcon("more")  # softer: "anything else?"
                text = self.listen(start_timeout_s=self.cfg.voice.followup_s)
            else:
                if trigger != "barge":  # when you interrupted, you're already talking
                    self.speaker.earcon("listen")  # the canceller keeps it out of the mic
                carry = trigger in ("barge", "wake") and bool(preroll)
                text = self.listen(preroll) if carry else self.listen()
                if trigger == "barge" and text and own_words(text, self.said_last):
                    # Her own voice, leaking past the echo canceller ("Did you mean?" while
                    # she asked "Did you mean vscode?"): not you, so nothing is done.
                    entry["heard"] = text
                    entry["route"] = "her own voice (ignored)"
                    return False
                if (
                    trigger == "wake" and carry and text
                    and not mentions_wake(text, list(self.cfg.voice.wake_words))
                ):  # fmt: skip
                    # The wake word fired, but there's no "Donna" in what was recorded: it
                    # was something else (a video, people talking), so it isn't acted on.
                    entry["heard"] = text
                    entry["route"] = "no wake word in it"
                    return False
                if trigger == "wake" and text and not _without_wake(text):
                    text = self.listen()  # just "Donna": now the request itself
                elif trigger == "wake" and text:
                    text = _without_wake(text)  # what came before "Donna" isn't the request
            assert text is not None
            entry["heard"] = text
            entry["listen_s"] = round(time.monotonic() - t0, 1)
            if not text:
                entry["route"] = "nothing heard"
                if trigger == "followup":
                    entry["quiet"] = True  # the window closed unused: not worth a log line
                return False
            if not given and _STOP_WORDS.match(_without_wake(text) or text):
                # "Stop" as the request: end it all here. Not "pause the music" (that's
                # "pause"), and nothing is said back; the popup closes.
                entry["route"] = "stopped by you"
                self.cancel_turn()
                return False
            if trigger == "followup" and not given and dialogue.closing(text):
                entry["route"] = "end of conversation"
                self.convo.clear()
                ended = True
                return False
            if incomplete(text):
                entry["route"] = "incomplete"
                self.say(f"Sorry, I only caught: {text.rstrip('-. ')}. Please say it again.")
                return False
            self.turn["heard"] = text
            self.turn["state"] = "thinking"
            self.write_status("thinking", heard=text)
            t1 = time.monotonic()
            self.asked = {}
            self.announced = ""
            reply = None if given else self.fast(text)
            if self.asked:
                entry["asked"] = self.asked
            if reply is not None:
                entry["route"] = "fast"
            elif not given and trigger in ("barge", "followup") and _fragment(text):
                # One stray word while talking over a reply or after one ("Not."): not a
                # request, so it isn't sent to the model to be answered.
                entry["route"] = "ignored (one word)"
                return False
            elif not given and _fragment(text):
                # One word that isn't a command ("code."): most of it was missed. Ask again
                # rather than have the model guess, with the old conversation, for 40 s.
                entry["route"] = "asked again (one word)"
                reply = f"Sorry, I only caught “{text.strip(' .,!?')}”. Please say it again."
            else:
                # Not a quick command: questions and requests get the accurate transcript.
                if not given:
                    text = self.accurate_text() or text
                entry["heard"] = text
                self.turn_update(heard=text)
                entry["route"] = route(text)
                reply = self.handle(text)
            if self.unanswered and reply and "declined" in reply:
                reply = "I didn't hear a yes or no, so I didn't do it."
            entry["reply"] = reply
            entry["handle_s"] = round(time.monotonic() - t1, 1)
            if self.interrupt.is_set():
                return False
            if reply and reply == self.announced:
                spoke = True  # already said before the action ran
            elif reply:
                self.turn.update(reply=reply, state="speaking", progress="")
                self.write_status("speaking")
                self.wait_for_popup(reply)
                t2 = time.monotonic()
                self.say(spoken(reply))
                entry["speak_s"] = round(time.monotonic() - t2, 1)
                spoke = not self.interrupt.is_set()
        except Exception as exc:  # never let one request kill the voice process
            if not self.interrupt.is_set():  # a stopped model call isn't a failure
                entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
                _log.warning(
                    "voice request failed", extra={"fields": {"exc_type": type(exc).__name__}}
                )
                self.say("Sorry, something went wrong.")
        finally:
            entry["total_s"] = round(time.monotonic() - t0, 1)
            stopped = self.interrupt.is_set() and "error" not in entry
            if stopped:
                cut = "stopped" if self.stopped_by_you else "interrupted"
                if not str(entry.get("route", "")).startswith("stopped"):
                    entry["route"] = f"{entry.get('route') or 'listening'} ({cut})"
            if self.turn is not None:
                if self.stopped_by_you:
                    self.turn["dismissed"] = True  # the popup closes now
                said = entry.get("reply") or self.turn["reply"]
                self.turn.update(
                    state="done", done_at=self.clock.now().isoformat(), progress="",
                    question="", heard=entry.get("heard") or self.turn["heard"],
                    reply="Stopped." if stopped and self.stopped_by_you else said,
                    **({"error": "Something went wrong."} if "error" in entry else {}),
                )  # fmt: skip
            if not entry.get("quiet"):
                record_history(self.cfg.runtime_dir, entry)
            if self.stopped_by_you:
                ended = True  # a stopped exchange isn't something to build on later
            if entry.get("heard") and "error" not in entry and not ended:
                self.convo.add(entry["heard"], entry.get("reply", ""), str(entry.get("route", "")))
            _log.info(
                "voice request",
                extra={
                    "fields": {
                        "route": entry.get("route"),
                        "total_s": entry["total_s"],
                        "failed": "error" in entry,
                    }
                },
            )
            self.write_status("ready")
        return spoke


_WAKE_ONLY = re.compile(
    r"^\W*(?:(?:hey|hi|hello|ok|okay|oh)\W+)?(?:donna|dona|donnah|donah|dawna|danna)\W*", re.I
)
# The wake word within the first few words: "so yeah, hey Donna, open Brave".
_WAKE_NEAR_START = re.compile(
    r"^(?:\W*[\w']+){0,6}?\W*(?:(?:hey|hi|hello|ok|okay|oh)\W+)?"
    r"(?:donna|dona|donnah|donah|dawna|danna)\b\W*",
    re.I,
)
# Said as the whole request: stop everything and close (media has "pause the music").
_STOP_WORDS = re.compile(
    r"^\W*(?:ok(?:ay)?\W+)?(?:stop(?:\s+(?:it|that|now|talking|speaking|listening))?|"
    r"cancel(?:\s+(?:it|that))?|never\s*mind|nevermind|enough|be\s+quiet|shut\s+up|"
    r"quiet|forget\s+it|that'?s\s+(?:all|it|enough))(?:\s+please)?\W*$",
    re.I,
)
VOICE_MAX_STEPS = 3  # model calls per spoken request (each 5-15 s on this laptop)
ACTIVE = ("listening", "thinking", "speaking")


def own_words(heard: str, said: str) -> bool:
    """Is what was heard just (part of) what she was saying? Then it was her own voice."""
    from rapidfuzz import fuzz

    h = re.sub(r"[^a-z0-9 ]", "", heard.lower()).strip()
    s = re.sub(r"[^a-z0-9 ]", "", said.lower()).strip()
    if len(h) < 4 or not s:
        return False
    return fuzz.partial_ratio(h, s) >= 85


def mentions_wake(text: str, words: list[str]) -> bool:
    """Is the wake word (or what speech recognition makes of it: "Dana", "Donner") in it?"""
    from rapidfuzz import fuzz

    wanted = [w.strip().lower() for w in words if w.strip()] or ["donna"]
    for token in re.findall(r"[a-z']+", text.lower()):
        if len(token) >= 4 and any(fuzz.ratio(token, w) >= 65 for w in wanted):
            return True
    return False


def confirm_question(pending: dict[str, Any]) -> str:
    """What to ask before an action, in plain words: "Open VS Code?", "Close Brave?"."""
    tool = str(pending.get("tool", ""))
    args = pending.get("args") or {}

    def short(path: Any) -> str:
        p = str(path).rstrip("/\\")
        return re.split(r"[/\\]", p)[-1] or p

    if tool == "open_app":
        q = f"Open {args.get('app')}"
        if args.get("target"):
            q += f" with {short(args['target'])}"
    elif tool == "close_app":
        q = f"Close {args.get('app')}"
    elif tool in ("open_path", "reveal_path"):
        q = f"{'Open' if tool == 'open_path' else 'Show'} {short(args.get('path', ''))}"
    elif tool == "open_url":
        from urllib.parse import urlsplit

        q = f"Open {urlsplit(str(args.get('url', ''))).hostname or 'that page'}"
    else:
        preview = str(pending.get("preview", ""))
        line = next((ln.strip() for ln in preview.splitlines() if ln.strip()), "")
        plain = bool(line) and len(line) <= 90 and not re.search(r"[\\/{}\[\]=]|\.exe\b", line)
        q = line if plain else tool.replace("_", " ")
        q = q[:1].upper() + q[1:]
    return q.rstrip(" .?") + "?"


def question_from_prompt(prompt: str) -> str:
    """The question from an executor prompt alone (no details of the action to hand)."""
    lines = prompt.splitlines()
    first = lines[0].split(" (tier")[0].replace("_", " ") if lines else "do that"
    detail = next(
        (
            ln.strip()
            for ln in lines[1:]
            if ln.strip() and not ln.startswith("Proceed") and not ln.strip().startswith("!")
        ),
        "",
    )
    return f"Shall I {first}: {detail[:160]}?" if detail else f"Shall I {first}?"


def _fragment(text: str) -> bool:
    """A single word that isn't something on its own ("Not.", "Big."), unlike "stop",
    "thanks" or "yes", which the router or the conversation handle."""
    words = re.findall(r"[a-z']+", text.lower())
    return len(words) <= 1 and (not words or words[0] not in _ONE_WORD_REQUESTS)


_ONE_WORDS = """stop cancel thanks thank yes no okay ok mute unmute louder quieter screenshot
lock pause play next previous hello hi bye goodbye help"""
_ONE_WORD_REQUESTS = frozenset(_ONE_WORDS.split())


def _without_wake(text: str) -> str:
    """What was said besides the wake word: "Hey Donna, open Brave" -> "open Brave", and
    "so yeah. Donna, open Brave" -> "open Brave" (what came before isn't the request)."""
    cut = _WAKE_ONLY.sub("", text, count=1)
    if cut == text:
        cut = _WAKE_NEAR_START.sub("", text, count=1)
    return cut.strip(" ,.!?")


def _launch_popup(cfg: Config) -> None:
    try:
        from personal_os.interfaces import popup

        popup.launch(cfg)
    except Exception as exc:
        _log.warning("voice popup not opened", extra={"error": type(exc).__name__})
        with contextlib.suppress(Exception):  # even the import may be what failed
            popup.record_error(cfg.runtime_dir, f"{type(exc).__name__}: {exc}")


SPOKEN_SENTENCES = 2
MAX_FOLLOWUPS = 8  # a conversation ends after this many follow-ups without a press


def spoken(reply: str) -> str:
    """What's said out loud: the first two sentences (or a list's heading and first items).
    The whole answer is in the popup, which says so."""
    lines = [ln.strip() for ln in reply.splitlines() if ln.strip()]
    if len(lines) > 1:  # a list: heading plus the first three items
        items = [ln.lstrip("- ").strip() for ln in lines[1:4]]
        more = " The rest is on screen." if len(lines) > 4 else ""
        return lines[0] + " " + "; ".join(items) + "." + more
    parts = re.split(r"(?<=[.!?])\s+", reply.strip())
    if len(parts) <= SPOKEN_SENTENCES:
        return reply
    return " ".join(parts[:SPOKEN_SENTENCES]) + " The rest is on screen."


HISTORY = "voice_history.jsonl"
HISTORY_KEEP = 30


def record_history(runtime_dir: Path, entry: dict[str, Any]) -> None:
    """The last few exchanges, on this laptop only (runtime folder), for `voice log` and the
    dashboard. Older lines are dropped."""
    path = runtime_dir / HISTORY
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text("utf-8").splitlines() if path.exists() else []
    lines = [*lines, json.dumps(entry, ensure_ascii=False)][-HISTORY_KEEP:]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


SAY_DIR = "say"
_said = 0
STOP_FILE = "voice_stop.json"


def request_stop(runtime_dir: Path) -> None:
    """Ask the running voice process to stop what it's doing now (the popup's Stop / Esc)."""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime.write_json_atomic(runtime_dir / STOP_FILE, {"at": time.time()})


def take_stop(runtime_dir: Path, max_age_s: float = 3.0) -> bool:
    """A fresh stop request, removed once read (an old one never stops a later turn)."""
    path = runtime_dir / STOP_FILE
    if not path.exists():
        return False
    data = runtime.read_json(path) or {}
    path.unlink(missing_ok=True)
    try:
        return time.time() - float(data.get("at", 0)) <= max_age_s
    except (TypeError, ValueError):
        return False


def queue_speech(runtime_dir: Path, text: str) -> bool:
    """Ask the running voice process to say `text` when it's free (False if voice isn't
    running: then the caller speaks it). Spoken between conversations, never over one."""
    if not text.strip() or not status(runtime_dir).get("running"):
        return False
    d = runtime_dir / SAY_DIR
    d.mkdir(parents=True, exist_ok=True)
    # Time first (they're said in order), then a counter: Windows' clock is coarse, and two
    # reminders due together must not share a name and overwrite each other.
    global _said
    _said += 1
    name = f"{time.time_ns():020d}-{os.getpid()}-{_said:06d}.json"
    runtime.write_json_atomic(d / name, {"text": text[:2000]})
    return True


def take_speech(runtime_dir: Path, max_age_s: float = 600) -> list[str]:
    """Queued announcements, oldest first; each file is removed once read. Old ones (voice
    was busy or stopped for ten minutes) are dropped rather than spoken late."""
    d = runtime_dir / SAY_DIR
    if not d.is_dir():
        return []
    out = []
    now_ns = time.time_ns()
    for f in sorted(d.glob("*.json")):
        data = runtime.read_json(f)
        f.unlink(missing_ok=True)
        try:
            age_s = (now_ns - int(f.stem.split("-")[0])) / 1e9
        except ValueError:
            continue
        if data and isinstance(data.get("text"), str) and age_s <= max_age_s:
            out.append(data["text"])
    return out


def local_time(iso: str, fmt: str = "%H:%M:%S") -> str:
    """A stored UTC timestamp as your local clock time, for display."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso).astimezone().strftime(fmt)
    except (TypeError, ValueError):
        return ""


def history(runtime_dir: Path, last: int = 10) -> list[dict[str, Any]]:
    path = runtime_dir / HISTORY
    if not path.exists():
        return []
    out = []
    for line in path.read_text("utf-8").splitlines()[-last:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def status(runtime_dir: Path) -> dict[str, Any]:
    """The voice process's status, checked against the real process (a killed process leaves
    its last status behind)."""
    import psutil

    path = runtime_dir / STATUS
    try:
        data: dict[str, Any] = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {"state": "not running", "running": False}
    pid = int(data.get("pid") or 0)
    alive = False
    if pid and psutil.pid_exists(pid):
        try:
            cmd = " ".join(psutil.Process(pid).cmdline())
            alive = "personal_os.voice" in cmd
        except psutil.Error:
            alive = False
    if not alive:
        data["state"] = "not running"
    data["running"] = alive
    return data


def google_client(cfg: Config) -> Any:
    from personal_os.integrations import google

    return google.Google(cfg.data_dir) if google.connected(cfg.data_dir) else None


def main() -> int:
    cfg = load_config()
    setup_logging(cfg.logs_dir)
    lock = runtime.InstanceLock(cfg.runtime_dir / LOCK)
    try:
        lock.acquire()
    except runtime.AlreadyRunningError:
        return 0
    try:
        Voice(cfg).run()
    finally:
        lock.release()
        Path(cfg.runtime_dir / STATUS).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
