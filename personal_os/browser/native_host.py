"""Native messaging host: the browser extension's only way to reach Personal OS (ADR-029).

Chrome/Brave/Edge start this process when the extension connects, and talk to it over
stdin/stdout with 4-byte little-endian length-prefixed JSON (the native messaging protocol).
Each visit/signal is appended as one line to spool/browser/<browser>-<pid>.jsonl, which the
daemon ingests exactly like the terminal spool. No network port is opened anywhere.

The host answers every message with {"ok": bool, "paused": bool}, so the extension can stop
sending while capture is paused (the daemon would drop those events anyway).
"""

from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path
from typing import IO, Any

from personal_os.config.loader import default_home

HOST_NAME = "com.personalos.host"
HOST_VERSION = "1.0.0"
MAX_MESSAGE_BYTES = 1_000_000  # we never accept huge messages, whatever the protocol allows
_ACCEPTED_TYPES = {"visit", "signal", "tabs"}


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


class Host:
    def __init__(self, home: Path, browser: str = "chromium") -> None:
        self.spool = home / "spool" / "browser"
        self.marker = home / "runtime" / "paused"
        safe = "".join(c for c in browser if c.isalnum())[:20] or "chromium"
        self.browser = safe
        self.file = self.spool / f"{safe}-{os.getpid()}.jsonl"

    def paused(self) -> bool:
        return self.marker.exists()

    def handle(self, msg: dict[str, Any]) -> dict[str, Any]:
        kind = msg.get("type")
        if kind == "hello":
            return {"ok": True, "paused": self.paused(), "host_version": HOST_VERSION}
        if kind not in _ACCEPTED_TYPES:
            return {"ok": False, "error": "unsupported message", "paused": self.paused()}
        if self.paused():
            return {"ok": True, "paused": True, "dropped": True}
        if msg.get("incognito"):
            return {"ok": True, "paused": False, "dropped": True}  # never written, not even once
        if kind == "tabs":
            msg = {**msg, "browser": self.browser}  # Brave reports itself as Chrome
        self.spool.mkdir(parents=True, exist_ok=True)
        with self.file.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n")
        return {"ok": True, "paused": False}


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
    host = Host(home, _browser_from_args(argv))
    stdin, stdout = _binary_stdio()
    while True:
        msg = read_message(stdin)
        if msg is None:
            return 0
        try:
            reply = host.handle(msg)
        except Exception as exc:  # never crash the port; report and continue
            reply = {"ok": False, "error": type(exc).__name__, "paused": host.paused()}
        write_message(stdout, reply)


if __name__ == "__main__":
    sys.exit(main())
