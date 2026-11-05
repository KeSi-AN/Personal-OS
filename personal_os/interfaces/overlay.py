"""The hotkey overlay (Ctrl+Alt+Shift+Space): ask, do, remember, from anywhere (DESIGN.md §6.4).

A small always-on-top tkinter window in its own process. Opening it loads the chat model in the
background, so the first answer skips the cold start. Typing:
- "do …" / "open …" / "remind me …" → the action planner; anything that needs confirming asks
  here with Yes/No buttons (a click in this window counts as you confirming);
- "remember that …" → shown back to you, stored only if you click Remember;
- anything else → ask, with follow-ups keeping the last question's project and time range.
"""

from __future__ import annotations

import sys
import threading
import tkinter as tk
from tkinter import messagebox
from typing import Any

from personal_os.config.loader import load_config
from personal_os.core import runtime
from personal_os.core.clock import SystemClock

LOCK = "overlay.lock"


class Overlay:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.clock = SystemClock()
        self.previous: Any = None
        self.root = tk.Tk()
        self.root.title("Personal OS")
        self.root.attributes("-topmost", True)
        self.root.geometry(self._geometry())
        self.root.bind("<Escape>", lambda _e: self.root.destroy())
        self.root.configure(padx=10, pady=10)
        self.entry = tk.Entry(self.root, font=("Segoe UI", 12))
        self.entry.pack(fill="x")
        self.entry.bind("<Return>", lambda _e: self.submit())
        self.hint = tk.Label(
            self.root,
            fg="#6b6b70",
            anchor="w",
            font=("Segoe UI", 9),
            text="Ask, or start with do/open/remind… · “remember that …” · Esc closes",
        )
        self.hint.pack(fill="x", pady=(4, 6))
        self.out = tk.Text(self.root, height=14, wrap="word", font=("Segoe UI", 10), relief="flat")
        self.out.pack(fill="both", expand=True)
        self.buttons = tk.Frame(self.root)
        self.buttons.pack(fill="x")
        self.entry.focus_force()
        threading.Thread(target=self._warm, daemon=True).start()

    def _geometry(self) -> str:
        w, h = 640, 380
        x = (self.root.winfo_screenwidth() - w) // 2
        return f"{w}x{h}+{x}+{max(40, self.root.winfo_screenheight() // 6)}"

    # --- background work ----------------------------------------------------------------------

    def _provider(self) -> Any:
        if not self.cfg.llm.enabled:
            return None
        from personal_os.llm import make_provider

        return make_provider(self.cfg.llm)

    def _warm(self) -> None:
        try:
            p = self._provider()
            if p is not None:
                p.warm("15m")
                self.root.after(
                    0, lambda: self.hint.configure(text=self.hint.cget("text") + " · model ready")
                )
        except Exception:
            pass

    def show(self, text: str) -> None:
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.insert("end", text)
        self.out.configure(state="disabled")

    def clear_buttons(self) -> None:
        for w in self.buttons.winfo_children():
            w.destroy()

    def submit(self) -> None:
        text = self.entry.get().strip()
        if not text:
            return
        self.clear_buttons()
        from personal_os.context import memory

        fact = memory.parse_remember(text)
        if fact:
            self.show(f"Remember this?\n\n{fact}")
            tk.Button(self.buttons, text="Remember", command=lambda: self._remember(fact)).pack(
                side="left"
            )
            return
        self.show("Working…")
        from personal_os.context.route import route

        # The same router as voice: "do I have ...?" is a question, "actually, cancel it" isn't.
        target = self._do if route(text) == "do" else self._ask
        threading.Thread(target=target, args=(text,), daemon=True).start()

    def _remember(self, fact: str) -> None:
        from personal_os.context import memory
        from personal_os.storage.db import open_db

        conn = open_db(self.cfg.db_path)
        try:
            memory.add(conn, self.clock, fact, "overlay")
        finally:
            conn.close()
        self.clear_buttons()
        self.show(f"Remembered: {fact}")

    def _ask(self, question: str) -> None:
        from personal_os.context.ask import ask
        from personal_os.storage.db import open_db

        conn = open_db(self.cfg.db_path)
        try:
            a = ask(conn, self._provider(), question, self.clock.now(), previous=self.previous)
            self.previous = a.question
            lines = [a.text, ""]
            if a.answered:
                lines.append(f"Confidence: {a.confidence}")
                for x in a.cited() or a.gathered.pack.lines[:6]:
                    lines.append(f"[{x.ref}] {x.ts.astimezone():%d %b %H:%M} {x.kind}: {x.text}")
            text = "\n".join(lines)
        except Exception as exc:
            text = f"Something went wrong: {exc}"
        finally:
            conn.close()
        self.root.after(0, lambda: self.show(text))

    def _do(self, request: str) -> None:
        from personal_os.actions.desktop import default_desktop
        from personal_os.actions.executor import Executor
        from personal_os.actions.planner import run_request
        from personal_os.actions.tools import build_registry
        from personal_os.storage.db import open_db

        provider = self._provider()
        if provider is None:
            self.root.after(0, lambda: self.show("The local model is turned off (llm.enabled)."))
            return
        conn = open_db(self.cfg.db_path)
        try:
            ex = Executor(
                self.cfg,
                conn,
                self.clock,
                build_registry(self.cfg),
                default_desktop(),
                self._confirm,
                provider=provider,
            )
            report = run_request(request, ex, provider)
            lines = [report.reply] if report.reply else []
            lines += [f"{o.tool}: {o.status} · {o.message}" for o in report.outcomes]
            if report.error:
                lines.append(f"Stopped: {report.error}")
            text = "\n".join(lines) or "Nothing to do."
        except Exception as exc:
            text = f"Something went wrong: {exc}"
        finally:
            conn.close()
        self.root.after(0, lambda: self.show(text))

    def _confirm(self, prompt: str) -> bool:
        """Ask on the UI thread and wait for your click (called from the worker thread)."""
        answer: list[bool] = []
        done = threading.Event()

        def ask_user() -> None:
            answer.append(
                messagebox.askyesno(
                    "Personal OS: confirm",
                    prompt.replace("Proceed? [y/N] ", "Proceed?"),
                    parent=self.root,
                    icon="warning",
                    default="no",
                )
            )
            done.set()

        self.root.after(0, ask_user)
        done.wait()
        return bool(answer and answer[0])

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    cfg = load_config()
    lock = runtime.InstanceLock(cfg.runtime_dir / LOCK)
    try:
        lock.acquire()
    except runtime.AlreadyRunningError:
        return 0  # already open
    try:
        Overlay().run()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
