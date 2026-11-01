"""Injectable clocks. Nothing in the system calls time.time() or datetime.now() directly."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...

    def monotonic(self) -> float:
        """Seconds on a clock that never goes backwards (for intervals and backoff)."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class SimClock:
    """Manually advanced clock for the simulator and tests. sleep() advances time instantly."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimClock needs a timezone-aware start time")
        self._now = start.astimezone(UTC)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("SimClock cannot go backwards")
        self._now += timedelta(seconds=seconds)
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(max(0.0, seconds))
        await asyncio.sleep(0)


def to_ms(dt: datetime) -> int:
    """Aware datetime → Unix epoch milliseconds (the storage format)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)
