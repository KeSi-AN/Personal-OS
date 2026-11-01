"""Lock/unlock, sleep/wake, logon/logoff and the global pause hotkey (DESIGN.md §4.4, §4.10)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

from personal_os.core.events import Event, SystemEvent
from personal_os.core.observer import BaseObserver, ObserverContext
from personal_os.platform.base import SessionSignal, SessionWatcher

VERSION = "1.0.0"


@dataclass
class SessionState:
    """Shared with the window observer so a locked screen counts as away immediately."""

    locked: bool = False


class SessionObserver(BaseObserver):
    name = "session"
    version = VERSION

    def __init__(
        self,
        watcher: SessionWatcher,
        state: SessionState,
        *,
        on_hotkey: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.watcher = watcher
        self.state = state
        self.on_hotkey = on_hotkey
        self._stopped = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def run(self, ctx: ObserverContext) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopped.clear()
        await asyncio.to_thread(self.watcher.start, self._from_thread)
        try:
            await self._stopped.wait()
        finally:
            await asyncio.to_thread(self.watcher.stop)

    async def stop(self) -> None:
        self._stopped.set()

    def _from_thread(self, signal: SessionSignal) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.handle, signal)

    def handle(self, signal: SessionSignal) -> None:
        """Runs on the event loop."""
        if signal == "hotkey":
            if self.on_hotkey is not None:
                self.on_hotkey()
            return
        if signal == "lock":
            self.state.locked = True
        elif signal in ("unlock", "logon"):
            self.state.locked = False
        if self._ctx is None:
            return
        kind = f"system.{signal}"
        self.emit(
            Event.point(
                SystemEvent(kind=kind),
                self._ctx.clock.now().astimezone(),
                observer_version=VERSION,
            )
        )
