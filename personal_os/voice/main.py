"""The voice process (DESIGN.md §6.5): push-to-talk, optional wake word, spoken replies.

Started by the daemon when `voice.enabled` is on. One request at a time:
beep → record until a pause → transcribe → route (briefing / remember / do / ask / stop) →
speak the reply. Anything that needs confirming is read out and needs a spoken yes.
The last exchange is written to runtime/voice_last.json for the dashboard; nothing else said
is kept.
"""

from __future__ import annotations

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
from personal_os.voice import tts
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
        self.cfg = cfg
        self.clock = SystemClock()
        self.speaker = tts.Speaker(cfg.voice, cfg.data_dir)
        self.stt = Transcriber(cfg.data_dir, cfg.voice.stt_model)
        self.triggers: queue.Queue[str] = queue.Queue()
        self.previous: Any = None
        self.wake: Any = None
        self.wake_thread: threading.Thread | None = None
        self.stop = threading.Event()

    # --- status ---------------------------------------------------------------------------

    def write_status(self, state: str, **extra: Any) -> None:
        from personal_os.voice.wake import wake_models

        data = {
            "state": state,
            "pid": __import__("os").getpid(),
            "wake_enabled": self.cfg.voice.wake_word_enabled,
            "wake_model": bool(wake_models(self.cfg.data_dir)),
            "tts": self.cfg.voice.tts,
            "last_voice": self.speaker.last_voice,
            "stt_ready": self.stt.available,
            **extra,
        }
        runtime.write_json_atomic(self.cfg.runtime_dir / STATUS, data)

    # --- listening ------------------------------------------------------------------------------

    def hotkey_loop(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if not user32.RegisterHotKey(
            None, PTT_ID, MOD_CONTROL | MOD_ALT | MOD_SHIFT | MOD_NOREPEAT, VK_V
        ):
            self.write_status("error", error="Ctrl+Alt+Shift+V is taken by another app")
            return
        msg = wintypes.MSG()
        try:
            while not self.stop.is_set() and user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam == PTT_ID:
                    self.triggers.put("hotkey")
        finally:
            user32.UnregisterHotKey(None, PTT_ID)

    def sync_wake(self) -> None:
        """Start or stop the wake-word listener to match the (re-read) config."""
        from personal_os.voice.wake import WakeListener, wake_models

        want = self.cfg.voice.wake_word_enabled and bool(wake_models(self.cfg.data_dir))
        running = self.wake_thread is not None and self.wake_thread.is_alive()
        if want and not running:
            self.wake = WakeListener(
                wake_models(self.cfg.data_dir),
                self.cfg.voice.wake_threshold,
                lambda: self.triggers.put("wake"),
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

    def listen(self) -> str:
        from personal_os.voice.audio import record_utterance

        if self.wake is not None:
            self.wake.paused.set()
        try:
            audio = record_utterance(self.stop.is_set)
        finally:
            if self.wake is not None:
                self.wake.paused.clear()
        if audio is None:
            return ""
        return self.stt.transcribe(audio, vocabulary=self.vocabulary())

    def vocabulary(self) -> str:
        from personal_os.projects.registry import load_projects
        from personal_os.storage.db import connect

        conn = connect(self.cfg.db_path)
        try:
            names = [p.id for p in load_projects(conn) if p.id != "none"][:30]
        finally:
            conn.close()
        return "Donna. Projects: " + ", ".join(names) + "."

    # --- handling --------------------------------------------------------------------------------

    def say(self, text: str) -> None:
        try:
            self.speaker.say(text)
        except tts.TTSError:
            _log.warning("no voice could speak")

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
        ok = is_yes(answer)
        self.say("Okay." if ok else "Not doing it.")
        return ok

    def handle(self, text: str) -> str:
        from personal_os.context.route import evening, route, strip_wake
        from personal_os.storage.db import open_db

        kind = route(text)
        request = strip_wake(text)
        if kind == "stop":
            tts.stop()
            return ""
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
                if is_yes(self.listen()):
                    memory.add(conn, self.clock, fact, "voice")
                    return "Remembered."
                return "I won't remember it."
            if kind == "do":
                return self.do(conn, request)
            from personal_os.context.ask import ask

            answer = ask(conn, self.provider(), request, self.clock.now(), previous=self.previous)
            self.previous = answer.question
            self.save_last(request, answer.text, [x.render() for x in answer.cited()])
            return " ".join(s for s, _ in answer.sentences)
        finally:
            conn.close()

    def do(self, conn: Any, request: str) -> str:
        from personal_os.actions.desktop import default_desktop
        from personal_os.actions.executor import Executor
        from personal_os.actions.planner import run_request
        from personal_os.actions.tools import build_registry

        provider = self.provider()
        if provider is None:
            return "The local model is turned off, so I can't plan actions."
        ex = Executor(
            self.cfg,
            conn,
            self.clock,
            build_registry(self.cfg),
            default_desktop(),
            self.confirm,
            provider=provider,
        )
        report = run_request(request, ex, provider)
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
        threading.Thread(target=self.hotkey_loop, name="pos-ptt", daemon=True).start()
        self.sync_wake()
        self.write_status("ready")
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
                    self.write_status("ready")
                continue
            self.respond(trigger)

    def respond(self, trigger: str) -> None:
        import winsound

        from personal_os.context.route import route

        entry: dict[str, Any] = {"at": self.clock.now().isoformat(), "trigger": trigger}
        t0 = time.monotonic()
        self.write_status("listening", trigger=trigger)
        winsound.Beep(880, 120)
        try:
            text = self.listen()
            entry["heard"] = text
            entry["listen_s"] = round(time.monotonic() - t0, 1)
            if not text:
                entry["route"] = "nothing heard"
                return
            entry["route"] = route(text)
            self.write_status("thinking", heard=text)
            t1 = time.monotonic()
            reply = self.handle(text)
            entry["reply"] = reply
            entry["handle_s"] = round(time.monotonic() - t1, 1)
            if reply:
                self.write_status("speaking")
                t2 = time.monotonic()
                self.say(reply)
                entry["speak_s"] = round(time.monotonic() - t2, 1)
        except Exception as exc:  # never let one request kill the voice process
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
            _log.warning("voice request failed", extra={"fields": {"exc_type": type(exc).__name__}})
            self.say("Sorry, something went wrong.")
        finally:
            entry["total_s"] = round(time.monotonic() - t0, 1)
            record_history(self.cfg.runtime_dir, entry)
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
