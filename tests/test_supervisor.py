from __future__ import annotations

import asyncio

from personal_os.core.bus import EventBus
from personal_os.core.clock import SimClock
from personal_os.core.events import Event, WindowFocus
from personal_os.core.logging import get_logger
from personal_os.core.observer import BaseObserver, ObserverContext, ObserverState, Supervisor
from tests.conftest import T0


class Flaky(BaseObserver):
    name = "flaky"
    version = "1"

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures
        self.starts = 0
        self.stopped = asyncio.Event()

    async def run(self, ctx: ObserverContext) -> None:
        self.starts += 1
        if self.starts <= self.failures:
            raise RuntimeError(f"boom {self.starts}")
        self.emit(Event.point(WindowFocus(app="x"), ctx.clock.now(), observer_version="1"))
        await self.stopped.wait()

    async def stop(self) -> None:
        self.stopped.set()


def make(clock: SimClock, bus: EventBus, **kw) -> Supervisor:
    def ctx(_obs: BaseObserver) -> ObserverContext:
        return ObserverContext(bus=bus, clock=clock, config=None, log=get_logger("test"))

    return Supervisor(ctx, clock, jitter=lambda: 1.0, **kw)


async def _settle() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


async def test_restarts_with_exponential_backoff_then_runs():
    clock = SimClock(T0)
    bus = EventBus()
    sup = make(clock, bus)
    obs = Flaky(failures=3)
    sup.add(obs)
    sup.start()
    await _settle()
    assert obs.starts == 4
    assert clock.monotonic() == 1 + 2 + 4  # backoff 1s, 2s, 4s (jitter pinned to 100%)
    health = sup.health()["flaky"]
    assert health.state == ObserverState.RUNNING
    assert health.restarts == 3
    assert health.last_error == "RuntimeError: boom 3"
    assert health.events_last_hour == 1
    await sup.stop()
    assert sup.health()["flaky"].state == ObserverState.STOPPED


async def test_marks_degraded_after_repeated_failures_and_caps_backoff():
    clock = SimClock(T0)
    sup = make(clock, EventBus(), degraded_after=3, max_backoff_s=5)
    obs = Flaky(failures=10**6)
    sup.add(obs)
    sup.start()
    await _settle()
    assert sup.health()["flaky"].state == ObserverState.DEGRADED
    gaps = [1, 2, 4] + [5] * (obs.starts - 4)
    assert clock.monotonic() >= sum(gaps[: obs.starts - 1])
    await sup.stop()


async def test_one_crashing_observer_does_not_affect_another():
    clock = SimClock(T0)
    bus = EventBus()
    sup = make(clock, bus)
    bad = Flaky(failures=10**6)
    bad.name = "bad"  # type: ignore[misc]
    good = Flaky(failures=0)
    good.name = "good"  # type: ignore[misc]
    sup.add(bad)
    sup.add(good)
    sup.start()
    await _settle()
    assert sup.health()["good"].state == ObserverState.RUNNING
    assert len(bus) >= 1
    await sup.stop()


async def test_disabled_observer_never_starts_and_pause_suppresses_emit():
    clock = SimClock(T0)
    bus = EventBus()
    sup = make(clock, bus)
    off = Flaky(failures=0)
    sup.add(off, enabled=False)
    sup.start()
    await _settle()
    assert off.starts == 0
    assert sup.health()["flaky"].state == ObserverState.DISABLED

    obs = Flaky(failures=0)
    ctx = ObserverContext(bus=bus, clock=clock, config=None, log=get_logger("t"))
    ctx.paused.set()
    obs._ctx = ctx
    assert not obs.emit(Event.point(WindowFocus(app="x"), T0, observer_version="1"))
    assert obs.health().state == ObserverState.PAUSED
