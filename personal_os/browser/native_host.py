"""Native messaging host: the browser extension's only way to reach Personal OS (ADR-029).

Chrome/Brave/Edge start this process when the extension connects, and talk to it over
stdin/stdout with 4-byte little-endian length-prefixed JSON (the native messaging protocol).
Each visit/signal is appended as one line to spool/browser/<browser>-<pid>.jsonl, which the
daemon ingests exactly like the terminal spool. No network port is opened anywhere.

The host answers every message with {"ok": bool, "paused": bool}, so the extension can stop
sending while capture is paused (the daemon would drop those events anyway).

Browser control (ADR-076) goes the other way over the same connection: Donna writes a command
file to runtime/browser/cmd/, this host (the only process the extension listens to) claims it
and passes it to the extension, and the extension's result comes back as a message that is
written to runtime/browser/res/. Web pages can't reach this channel; nothing listens on a port.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import struct
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import IO, Any

from personal_os.config.loader import default_home

HOST_NAME = "com.personalos.host"
HOST_VERSION = "1.1.0"
MAX_MESSAGE_BYTES = 1_000_000  # we never accept huge messages, whatever the protocol allows
_ACCEPTED_TYPES = {"visit", "signal", "tabs", "result", "inbox", "projects"}
_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_BROWSERS = {"brave.exe": "brave", "chrome.exe": "chrome", "msedge.exe": "edge"}
CMD_POLL_S = 0.15
CMD_MAX_AGE_S = 30  # a command nobody picked up in time is stale: dropped, never run late


def read_message(stream: IO[bytes]) -> dict[str, Any] | None:
    header = stream.read(4)
    if len(header) < 4:
        return None  # browser closed the port
    (length,) = struct.unpack("<I", header)
    if length > MAX_MESSAGE_BYTES:
        stream.read(length)  # drain and refuse
        return {"type": "_too_large"}
    body = stream.read(length)
    try:
        msg = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"type": "_malformed"}
    return msg if isinstance(msg, dict) else {"type": "_malformed"}


def write_message(stream: IO[bytes], msg: dict[str, Any]) -> None:
    data = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    stream.write(struct.pack("<I", len(data)) + data)
    stream.flush()


def detect_browser(fallback: str) -> str:
    """Which browser started us, from the process tree. Brave also reads Chrome's registration,
    so the launcher's --browser argument can say "chrome" when it's really Brave."""
    try:
        import psutil

        proc = psutil.Process(os.getpid()).parent()
        for _ in range(4):
            if proc is None:
                break
            name = proc.name().lower()
            if name in _BROWSERS:
                return _BROWSERS[name]
            proc = proc.parent()
    except Exception:
        pass
    return fallback


class Host:
    def __init__(self, home: Path, browser: str = "chromium") -> None:
        self.spool = home / "spool" / "browser"
        self.runtime = home / "runtime"
        self.marker = self.runtime / "paused"
        safe = "".join(c for c in browser if c.isalnum())[:20] or "chromium"
        self.browser = safe
        self.file = self.spool / f"{safe}-{os.getpid()}.jsonl"
        self.cmd_dir = self.runtime / "browser" / "cmd"
        self.res_dir = self.runtime / "browser" / "res"
        self.hosts_dir = self.runtime / "browser" / "hosts"

    def paused(self) -> bool:
        return self.marker.exists()

    def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        kind = msg.get("type")
        if kind == "hello":
            return {"ok": True, "paused": self.paused(), "host_version": HOST_VERSION}
        if kind not in _ACCEPTED_TYPES:
            return {"ok": False, "error": "unsupported message", "paused": self.paused()}
        if kind == "projects":  # the list for the right-click "Add to project" menu
            data: dict[str, Any] = {}
            with contextlib.suppress(OSError, ValueError):
                data = json.loads((self.runtime / "browser" / "projects.json").read_text("utf-8"))
            return {"ok": True, "paused": self.paused(), "projects": data.get("projects", [])}
        if kind == "result":
            self.write_result(msg)  # an answer to a command Donna sent: not capture
            return {"ok": True, "paused": self.paused()}
        if self.paused():
            return {"ok": True, "paused": True, "dropped": True}
        if msg.get("incognito"):
            return {"ok": True, "paused": False, "dropped": True}  # never written, not even once
        if kind == "inbox":  # "Add to Personal OS project": for the daemon to file
            url = str(msg.get("url", ""))
            if url.startswith(("http://", "https://")):
                d = self.runtime / "browser" / "inbox"
                d.mkdir(parents=True, exist_ok=True)
                rec = {"url": url[:2000], "title": str(msg.get("title", ""))[:200],
                       "project": str(msg.get("project", ""))[:80]}  # fmt: skip
                # Time first (filed in order), then a random part: Windows' clock is coarse, and two
                # quick right-clicks must not share a name and overwrite each other.
                name = f"{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
                (d / name).write_text(json.dumps(rec), "utf-8")
            return {"ok": True, "paused": False}
        if kind == "tabs":
            msg = {**msg, "browser": self.browser}  # Brave reports itself as Chrome
        self.spool.mkdir(parents=True, exist_ok=True)
        with self.file.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n")
        return {"ok": True, "paused": False}

    # --- browser control ------------------------------------------------------------------

    def write_result(self, msg: dict[str, Any]) -> None:
        rid = str(msg.get("id", ""))
        if not _ID.match(rid):
            return
        self.res_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.res_dir / f"{rid}.{os.getpid()}.tmp"
        tmp.write_text(json.dumps(msg, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.res_dir / f"{rid}.json")

    def take_commands(self, now: float | None = None) -> list[dict[str, Any]]:
        """Commands for this browser (or any), claimed so no other host runs them too."""
        if not self.cmd_dir.is_dir():
            return []
        now = time.time() if now is None else now
        out = []
        for f in sorted(self.cmd_dir.glob("*.json")):
            try:
                cmd = json.loads(f.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            target = str(cmd.get("browser") or "any")
            if target not in ("any", self.browser):
                continue
            claimed = f.with_name(f"{f.stem}.taken-{os.getpid()}")
            try:
                os.replace(f, claimed)  # atomic: only one host wins
            except OSError:
                continue
            claimed.unlink(missing_ok=True)
            if now - float(cmd.get("at", 0)) > CMD_MAX_AGE_S or not _ID.match(str(cmd.get("id"))):
                continue
            out.append(cmd)
        return out

    def register(self) -> Path:
        """Say this browser can take commands (removed when the browser closes the port)."""
        self.hosts_dir.mkdir(parents=True, exist_ok=True)
        f = self.hosts_dir / f"{self.browser}-{os.getpid()}.json"
        f.write_text(json.dumps({"browser": self.browser, "pid": os.getpid()}), encoding="utf-8")
        return f


def _binary_stdio() -> tuple[IO[bytes], IO[bytes]]:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    return sys.stdin.buffer, sys.stdout.buffer


def _browser_from_args(argv: list[str]) -> str:
    # Chrome passes the caller's origin; we only learn the browser from our own launcher arg.
    for arg in argv:
        if arg.startswith("--browser="):
            return arg.split("=", 1)[1]
    return "chromium"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    home = default_home()
    for arg in argv:
        if arg.startswith("--home="):
            home = Path(arg.split("=", 1)[1])
    host = Host(home, detect_browser(_browser_from_args(argv)))
    stdin, stdout = _binary_stdio()
    lock = threading.Lock()  # replies and commands share stdout
    done = threading.Event()

    def send(msg: dict[str, Any]) -> None:
        with lock:
            write_message(stdout, msg)

    def relay() -> None:
        while not done.is_set():
            try:
                for cmd in host.take_commands():
                    send(
                        {
                            "type": "cmd",
                            "id": cmd["id"],
                            "op": cmd.get("op"),
                            "args": cmd.get("args") or {},
                        }
                    )
            except Exception:
                pass  # a bad command file never takes the connection down
            done.wait(CMD_POLL_S)

    marker = host.register()
    threading.Thread(target=relay, name="pos-browser-cmd", daemon=True).start()
    try:
        while True:
            msg = read_message(stdin)
            if msg is None:
                return 0
            try:
                reply = host.handle(msg)
            except Exception as exc:  # never crash the port; report and continue
                reply = {"ok": False, "error": type(exc).__name__, "paused": host.paused()}
            send(reply)
    finally:
        done.set()
        marker.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
