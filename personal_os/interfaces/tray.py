"""The tray icon (DESIGN.md §6.4): state at a glance, the controls you need, and notifications.

It runs on its own thread inside the daemon; menu actions are handed to the daemon's event loop.
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass

from PIL import Image, ImageDraw

from personal_os.core.logging import get_logger

_log = get_logger("tray")

COLORS = {"capturing": (31, 138, 76), "paused": (178, 107, 0), "killed": (192, 57, 43)}


def icon_image(state: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((6, 6, 58, 58), fill=COLORS.get(state, (110, 110, 110)))
    d.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 230))
    if state == "paused":
        d.rectangle((26, 25, 30, 39), fill=COLORS["paused"])
        d.rectangle((34, 25, 38, 39), fill=COLORS["paused"])
    return img


@dataclass
class TrayActions:
    """What the menu does; each callable is safe to call from the tray thread."""

    state: Callable[[], str]  # "capturing" | "paused" | "killed"
    toggle_pause: Callable[[], None]
    open_dashboard: Callable[[], None]
    open_overlay: Callable[[], None]
    toggle_kill: Callable[[], None]
    restart: Callable[[], None]
    stop: Callable[[], None]
    voice_state: Callable[[], tuple[bool, bool]] = lambda: (False, False)  # voice on, wake on
    toggle_voice: Callable[[], None] = lambda: None
    toggle_wake: Callable[[], None] = lambda: None


class Tray:
    def __init__(self, actions: TrayActions) -> None:
        import pystray

        self.actions = actions
        self._pystray = pystray
        item = pystray.MenuItem
        self.icon = pystray.Icon(
            "personal-os",
            icon_image(actions.state()),
            "Personal OS",
            menu=pystray.Menu(
                item(lambda _i: self._label(), None, enabled=False),
                pystray.Menu.SEPARATOR,
                item("Open dashboard", self._wrap(actions.open_dashboard), default=True),
                item("Ask… (Ctrl+Alt+Shift+Space)", self._wrap(actions.open_overlay)),
                item(
                    lambda _i: "Resume capture" if self._is("paused") else "Pause capture",
                    self._wrap(actions.toggle_pause),
                ),
                item(
                    lambda _i: (
                        "Resume actions" if self._is("killed") else "Kill switch (actions off)"
                    ),
                    self._wrap(actions.toggle_kill),
                ),
                pystray.Menu.SEPARATOR,
                item(
                    lambda _i: (
                        "Turn voice off"
                        if actions.voice_state()[0]
                        else "Turn voice on (Ctrl+Alt+Shift+V)"
                    ),
                    self._wrap(actions.toggle_voice),
                ),
                item(
                    lambda _i: (
                        "Wake word “hey Donna”: on"
                        if actions.voice_state()[1]
                        else "Wake word “hey Donna”: off"
                    ),
                    self._wrap(actions.toggle_wake),
                ),
                pystray.Menu.SEPARATOR,
                item("Restart capture", self._wrap(actions.restart)),
                item(
                    "Stop capture and voice (until you start them again)", self._wrap(actions.stop)
                ),
            ),
        )
        self._thread: threading.Thread | None = None

    def _is(self, state: str) -> bool:
        try:
            return self.actions.state() == state
        except Exception:
            return False

    def _label(self) -> str:
        return {
            "capturing": "Capturing",
            "paused": "Paused",
            "killed": "Actions off (kill switch)",
        }.get(self.actions.state(), "Personal OS")

    def _wrap(self, fn: Callable[[], None]) -> Callable[..., None]:
        def run(*_args: object) -> None:
            try:
                fn()
            except Exception:
                _log.warning("tray action failed", exc_info=True)
            self.refresh()

        return run

    def start(self) -> None:
        self._thread = threading.Thread(target=self.icon.run, name="pos-tray", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.icon.stop()

    def refresh(self) -> None:
        try:
            self.icon.icon = icon_image(self.actions.state())
            self.icon.title = f"Personal OS: {self._label()}"
            self.icon.update_menu()
        except Exception:
            pass

    def notify(self, message: str, title: str = "Personal OS") -> None:
        try:
            self.icon.notify(message[:250], title)
        except Exception:
            _log.warning("notification failed")


def open_url(url: str) -> None:
    import os

    os.startfile(url)  # type: ignore[attr-defined,unused-ignore]


def spawn_module(module: str) -> subprocess.Popen[bytes]:
    """Start one of our modules windowless (overlay, voice)."""
    from personal_os.core import spawn

    return subprocess.Popen(
        spawn.module_command(module),
        env=spawn.child_env(),
        creationflags=spawn.creation_flags(detached=True),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def spawn_overlay() -> None:
    from personal_os.core import spawn

    subprocess.Popen(
        spawn.module_command("personal_os.interfaces.overlay"),
        env=spawn.child_env(),
        creationflags=spawn.creation_flags(detached=True),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
