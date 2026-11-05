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
from personal_os.voice.stt import Transcriber

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
        self.stt = Transcriber(cfg.data_dir, cfg.voice.stt_model)
        fast = Transcriber(cfg.data_dir, cfg.voice.stt_fast_model or cfg.voice.stt_model)
        # Heard first; the accurate model re-hears only what the fast path can't place.
        self.stt_fast = fast if cfg.voice.stt_fast_model and fast.available else self.stt
        self.index = LiveIndex(cfg, cfg.db_path)
        self.last_audio: Any = None  # the last utterance, for a biased second decode
        self._accurate: tuple[int, str] | None = None  # (id of the audio, small.en's text)
        self.barge_frames: list[Any] = []  # what was heard while interrupting a reply
        self.turn_trigger = ""  # what started the current turn: hotkey, wake or barge
        self.asked: dict[str, str] = {}  # a "Did you mean ...?" and its answer, for the log
        self.triggers: queue.Queue[str] = queue.Queue()
        self.previous: Any = None
        self.wake: Any = None
        self.wake_thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.hotkey_error: str | None = None  # kept, so "ready" writes don't hide it
        self.state = "starting"
        self.turn: dict[str, Any] | None = None  # the current or last exchange, for the popup
        self.convo = dialogue.Conversation()  # the last few exchanges, in memory only
        # A request to run next, without listening: "no, at 5 pm instead" or "I wanted X".
        self.next_request: str | None = None
        # Set by a hotkey press while thinking or speaking: stop talking, listen again.
        self.interrupt = threading.Event()

    # --- status ---------------------------------------------------------------------------

    def write_status(self, state: str, **extra: Any) -> None:
        from personal_os.voice.wake import wake_models

        self.state = state
        data = {
            "state": state,
            "pid": __import__("os").getpid(),
            "wake_enabled": self.cfg.voice.wake_word_enabled,
            "wake_model": bool(wake_models(self.cfg.data_dir)),
            "tts": self.cfg.voice.tts,
            "last_voice": self.speaker.last_voice,
            "stt_ready": self.stt.available,
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

    def sync_wake(self) -> None:
        """Start or stop the wake-word listener to match the (re-read) config."""
        from personal_os.voice.wake import WakeListener, wake_models

        want = self.cfg.voice.wake_word_enabled and bool(wake_models(self.cfg.data_dir))
        running = self.wake_thread is not None and self.wake_thread.is_alive()
        if want and not running:
            self.wake = WakeListener(
                wake_models(self.cfg.data_dir),
                self.cfg.voice.wake_threshold,
                lambda: self.pressed("wake"),
            )
            self.wake_thread = threading.Thread(target=self._run_wake, name="pos-wake", daemon=True)
            self.wake_thread.start()
        elif not want and running and self.wake is not None:
            self.wake.stop.set()

    def _run_wake(self) -> None:
        try:
            assert self.wake is not None
            self.wake.run()
        except Exception as exc:
            _log.warning("wake word stopped", extra={"fields": {"exc_type": type(exc).__name__}})

    def listen(self, preroll: list[Any] | None = None, start_timeout_s: float | None = None) -> str:
        """Record one turn and transcribe it. If what was said is clearly unfinished ("open
        ..."), keep listening a moment for the rest instead of acting on half a sentence."""
        import numpy as np

        from personal_os.context.route import incomplete
        from personal_os.voice.audio import START_TIMEOUT_S, record_utterance

        v = self.cfg.voice
        if self.wake is not None:
            self.wake.paused.set()
        try:
            audio = record_utterance(
                self.stop.is_set, floor=v.mic_floor, silence_ms=v.vad_silence_ms,
                preroll=preroll or (), start_timeout_s=start_timeout_s or START_TIMEOUT_S,
            )  # fmt: skip
            if audio is None:
                self.last_audio = None
                return ""
            text = self.transcribe(audio)
            if incomplete(text):
                more = record_utterance(
                    self.stop.is_set, floor=v.mic_floor, silence_ms=v.vad_silence_ms,
                    start_timeout_s=2.0,
                )  # fmt: skip
                if more is not None:
                    audio = np.concatenate([audio, more])
                    text = self.transcribe(audio)
        finally:
            if self.wake is not None:
                self.wake.paused.clear()
        self.last_audio = audio
        return text

    def transcribe(self, audio: Any, *, accurate: bool = False) -> str:
        names = self.index.current().names_for_hints(40)
        model = self.stt if accurate else self.stt_fast
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
        """Speak, stoppable: by the hotkey, or by talking over it (voice.barge_in = voice).
        A reply to a turn that itself began by barge-in isn't voice-interruptible, so stray
        sound can never keep the assistant answering itself."""
        try:
            if self.cfg.voice.barge_in == "voice" and self.turn_trigger != "barge":
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

    def ask_aloud(self, question: str) -> str:
        """Ask, then beep, so it's clear it's now waiting for your answer."""
        self.say(question)
        if self.turn is not None:
            self.turn.update(question=question, state="listening")
        self.write_status("listening", question=question)
        self.speaker.earcon("listen")
        return self.listen()

    def classifier(self) -> Any:
        """The local model, for answers the rules can't place (None if it's off)."""
        provider = self.provider()
        if provider is None:
            return None
        return dialogue.model_classifier(
            provider, self.index.current(), threshold=self.cfg.voice.router_threshold
        )

    def run_intent(self, intent: Any, text: str) -> str:
        from personal_os.actions.base import Origin
        from personal_os.storage.db import open_db

        if not intent.calls:
            return str(intent.reply)
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

        return Executor(
            self.cfg, conn, self.clock, build_registry(self.cfg), default_desktop(),
            self.confirm, provider=provider,
        )  # fmt: skip

    def confirm(self, prompt: str) -> bool:
        first = prompt.splitlines()[0].split(" (tier")[0]
        detail = next(
            (
                ln.strip()
                for ln in prompt.splitlines()[1:]
                if ln.strip() and not ln.startswith("Proceed")
            ),
            "",
        )
        self.say(f"Shall I {first.replace('_', ' ')}: {detail[:160]}? Say yes or no.")
        answer = self.listen()
        verdict, change = dialogue.yes_no(answer)
        if verdict == "amend":
            self.next_request = change
            self.say("Okay, changing it.")
            return False
        self.say("Okay." if verdict == "yes" else "Not doing it.")
        return verdict == "yes"

    def handle(self, text: str) -> str:
        from personal_os.context.route import chat_reply, evening, route, strip_polite, strip_wake
        from personal_os.storage.db import open_db

        kind = route(text)
        request = strip_wake(text)
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

    def do(self, conn: Any, request: str) -> str:
        from personal_os.actions.planner import run_request

        provider = self.provider()
        if provider is None:
            return "The local model is turned off, so I can't plan actions."
        ex = self.executor(conn, provider)
        report = run_request(request, ex, provider, context=self.convo.for_planner())
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

        return make_provider(self.cfg.llm)

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
        threading.Thread(target=self.warm, name="pos-warm", daemon=True).start()
        self.sync_wake()
        self.write_status("ready")
        _log.info("voice ready", extra={"fields": {"hotkey": self.hotkey_error is None}})
        last_reload = time.monotonic()
        while not self.stop.is_set():
            try:
                trigger = self.triggers.get(timeout=1.0)
            except queue.Empty:
                if time.monotonic() - last_reload > 10:
                    last_reload = time.monotonic()
                    self.cfg = load_config()
                    if not self.cfg.voice.enabled or runtime.stop_requested(self.cfg.runtime_dir):
                        break
                    self.sync_wake()
                    self.stt.unload_if_idle()
                    self.stt_fast.unload_if_idle()
                    self.write_status("ready")
                continue
            while not self.triggers.empty():  # presses made while busy count once
                self.triggers.get_nowait()
            self.converse(trigger)

    def converse(self, trigger: str) -> None:
        """One turn, then any follow-ups: a request it was told to make next ("no, at 5 pm
        instead"), and whatever you say in the few seconds after a reply, without the hotkey.
        "Thanks" or silence ends it; so does a press (that starts a fresh turn)."""
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
            elif trigger == "followup":
                self.speaker.earcon("more")  # softer: "anything else?"
                text = self.listen(start_timeout_s=self.cfg.voice.followup_s)
            else:
                if trigger != "barge":  # when you interrupted, you're already talking
                    self.speaker.earcon("listen")
                text = self.listen(preroll) if trigger == "barge" and preroll else self.listen()
            assert text is not None
            entry["heard"] = text
            entry["listen_s"] = round(time.monotonic() - t0, 1)
            if not text:
                entry["route"] = "nothing heard"
                if trigger == "followup":
                    entry["quiet"] = True  # the window closed unused: not worth a log line
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
            reply = None if given else self.fast(text)
            if self.asked:
                entry["asked"] = self.asked
            if reply is not None:
                entry["route"] = "fast"
            else:
                # Not a quick command: questions and requests get the accurate transcript.
                if not given:
                    text = self.accurate_text() or text
                entry["heard"] = text
                self.turn_update(heard=text)
                entry["route"] = route(text)
                reply = self.handle(text)
            entry["reply"] = reply
            entry["handle_s"] = round(time.monotonic() - t1, 1)
            if self.interrupt.is_set():
                entry["route"] = f"{entry['route']} (interrupted)"
                return False
            if reply:
                self.turn.update(reply=reply, state="speaking")
                self.write_status("speaking")
                t2 = time.monotonic()
                self.say(spoken(reply))
                entry["speak_s"] = round(time.monotonic() - t2, 1)
                if self.interrupt.is_set():
                    entry["route"] = f"{entry['route']} (interrupted)"
                else:
                    spoke = True
        except Exception as exc:  # never let one request kill the voice process
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
            _log.warning("voice request failed", extra={"fields": {"exc_type": type(exc).__name__}})
            self.say("Sorry, something went wrong.")
        finally:
            entry["total_s"] = round(time.monotonic() - t0, 1)
            if self.turn is not None:
                self.turn.update(
                    state="done", done_at=self.clock.now().isoformat(),
                    heard=entry.get("heard") or self.turn["heard"],
                    reply=entry.get("reply") or self.turn["reply"],
                    **({"error": "Something went wrong."} if "error" in entry else {}),
                )  # fmt: skip
            if not entry.get("quiet"):
                record_history(self.cfg.runtime_dir, entry)
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
    """What's said out loud: the first two sentences (or a list's heading and first items),
    pointing to the dashboard for the rest. The full answer is still saved for the dashboard."""
    lines = [ln.strip() for ln in reply.splitlines() if ln.strip()]
    if len(lines) > 1:  # a list: heading plus the first three items
        items = [ln.lstrip("- ").strip() for ln in lines[1:4]]
        more = " There's more on the dashboard." if len(lines) > 4 else ""
        return lines[0] + " " + "; ".join(items) + "." + more
    parts = re.split(r"(?<=[.!?])\s+", reply.strip())
    if len(parts) <= SPOKEN_SENTENCES:
        return reply
    return " ".join(parts[:SPOKEN_SENTENCES]) + " The rest is on the dashboard."


HISTORY = "voice_history.jsonl"
HISTORY_KEEP = 30


def record_history(runtime_dir: Path, entry: dict[str, Any]) -> None:
    """The last few exchanges, on this laptop only (runtime folder), for `voice log` and the
    dashboard. Older lines are dropped."""
    path = runtime_dir / HISTORY
    lines = path.read_text("utf-8").splitlines() if path.exists() else []
    lines = [*lines, json.dumps(entry, ensure_ascii=False)][-HISTORY_KEEP:]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
