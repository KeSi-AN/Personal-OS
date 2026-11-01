"""Observer contract and the supervisor that keeps observers running (DESIGN.md §4.0)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel

from personal_os.core.bus import EventBus
from personal_os.core.clock import Clock
from personal_os.core.events import Event
from personal_os.core.logging import get_logger


class ObserverState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    DEGRADED = "degraded"
    CRASHED = "crashed"
    DISABLED = "disabled"
    STOPPED = "stopped"


class ObserverHealth(BaseModel):
    state: ObserverState
    last_event_at: datetime | None = None
    events_last_hour: int = 0
    restarts: int = 0
    last_error: str | None = None  # exception type + message, never event content


@dataclass
class ObserverContext:
    bus: EventBus
    clock: Clock
    config: BaseModel | None
    log: logging.Logger
    paused: asyncio.Event = field(default_factory=asyncio.Event)  # set = paused


class BaseObserver:
    """Convenience base: tracks event counts for health() and honours pause in emit()."""

    name: ClassVar[str] = "base"
    version: ClassVar[str] = "0.0.0"

    def __init__(self) -> None:
        self._ctx: ObserverContext | None = None
        self._last_event_at: datetime | None = None
        self._minute_buckets: deque[tuple[int, int]] = deque()  # (minute index, count)

    async def start(self, ctx: ObserverContext) -> None:
        self._ctx = ctx
        await self.run(ctx)

    async def run(self, ctx: ObserverContext) -> None:
        raise NotImplementedError

    async def stop(self) -> None:
        return None

    def emit(self, event: Event) -> bool:
        ctx = self._ctx
        if ctx is None or ctx.paused.is_set():
            return False
        now = ctx.clock.now()
        self._last_event_at = now
        minute = int(now.timestamp() // 60)
        if self._minute_buckets and self._minute_buckets[-1][0] == minute:
            m, c = self._minute_buckets[-1]
            self._minute_buckets[-1] = (m, c + 1)
        else:
            self._minute_buckets.append((minute, 1))
        while self._minute_buckets and self._minute_buckets[0][0] <= minute - 60:
            self._minute_buckets.popleft()
        return ctx.bus.publish(event)

    def health(self) -> ObserverHealth:
        if self._ctx is None:
            return ObserverHealth(state=ObserverState.STARTING)
        minute = int(self._ctx.clock.now().timestamp() // 60)
        count = sum(c for m, c in self._minute_buckets if m > minute - 60)
        state = ObserverState.PAUSED if self._ctx.paused.is_set() else ObserverState.RUNNING
        return ObserverHealth(
            state=state, last_event_at=self._last_event_at, events_last_hour=count
        )


@dataclass
class _Slot:
    observer: BaseObserver
    enabled: bool
    state: ObserverState = ObserverState.STARTING
    restarts: int = 0
    last_error: str | None = None
    failures: deque[float] = field(default_factory=deque)
    task: asyncio.Task[None] | None = None


class Supervisor:
    """Runs each observer in its own task and restarts it with exponential backoff + jitter."""

    def __init__(
        self,
        make_context: Callable[[BaseObserver], ObserverContext],
        clock: Clock,
        *,
        base_backoff_s: float = 1.0,
        max_backoff_s: float = 300.0,
        degraded_after: int = 5,
        degraded_window_s: float = 600.0,
        healthy_run_s: float = 60.0,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._make_context = make_context
        self._clock = clock
        self._base = base_backoff_s
        self._max = max_backoff_s
        self._degraded_after = degraded_after
        self._degraded_window = degraded_window_s
        self._healthy_run = healthy_run_s
        self._jitter = jitter
        self._slots: dict[str, _Slot] = {}
        self._stopping = False
        self._log = get_logger("supervisor")

    def add(self, observer: BaseObserver, *, enabled: bool = True) -> None:
        if observer.name in self._slots:
            raise ValueError(f"observer {observer.name!r} already registered")
        state = ObserverState.STARTING if enabled else ObserverState.DISABLED
        self._slots[observer.name] = _Slot(observer, enabled, state)

    def start(self) -> None:
        for slot in self._slots.values():
            if slot.enabled and slot.task is None:
                slot.task = asyncio.create_task(
                    self._run(slot), name=f"observer:{slot.observer.name}"
                )

    async def stop(self, timeout_s: float = 5.0) -> None:
        self._stopping = True
        for slot in self._slots.values():
            if slot.task is None:
                continue
            try:
                await asyncio.wait_for(slot.observer.stop(), timeout_s)
            except Exception as exc:
                self._log.warning("observer stop failed", extra=_fields(slot, exc))
            slot.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await slot.task
            slot.state = ObserverState.STOPPED

    def health(self) -> dict[str, ObserverHealth]:
        out: dict[str, ObserverHealth] = {}
        for name, slot in self._slots.items():
            base = slot.observer.health()
            state = slot.state
            if (
                state == ObserverState.DEGRADED
                and slot.failures
                and self._clock.monotonic() - slot.failures[-1] > self._degraded_window
            ):
                state = slot.state = ObserverState.RUNNING  # stable again for a full window
            if state == ObserverState.RUNNING and base.state == ObserverState.PAUSED:
                state = ObserverState.PAUSED
            out[name] = base.model_copy(
                update={"state": state, "restarts": slot.restarts, "last_error": slot.last_error}
            )
        return out

    async def _run(self, slot: _Slot) -> None:
        attempt = 0
        while not self._stopping:
            started = self._clock.monotonic()
            if slot.state != ObserverState.DEGRADED:
                slot.state = ObserverState.RUNNING
            try:
                await slot.observer.start(self._make_context(slot.observer))
                if self._stopping:
                    return
                slot.last_error = "observer returned without being stopped"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                slot.last_error = f"{type(exc).__name__}: {exc}"[:300]
                self._log.warning("observer crashed", extra=_fields(slot, exc))

            now = self._clock.monotonic()
            if now - started >= self._healthy_run:
                attempt = 0
            slot.failures.append(now)
            while slot.failures and slot.failures[0] < now - self._degraded_window:
                slot.failures.popleft()
            slot.state = (
                ObserverState.DEGRADED
                if len(slot.failures) >= self._degraded_after
                else ObserverState.CRASHED
            )
            delay = min(self._max, self._base * (2**attempt))
            delay *= 0.5 + self._jitter() / 2  # jitter in [50%, 100%]
            attempt += 1
            slot.restarts += 1
            await self._clock.sleep(delay)


def _fields(slot: _Slot, exc: BaseException) -> dict[str, Any]:
    return {
        "fields": {
            "observer": slot.observer.name,
            "restarts": slot.restarts,
            "exc_type": type(exc).__name__,
        }
    }
