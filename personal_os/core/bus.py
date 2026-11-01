"""Bounded in-process event bus with a priority drop policy (DESIGN.md §4.2).

publish() never blocks. When the queue is full, the oldest event of the lowest priority
level that is not above the incoming event's level is evicted; if everything queued is more
important than the incoming event, the incoming event is dropped. Every drop is counted.
"""

from __future__ import annotations

import asyncio
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from personal_os.core.events import Event

# Higher = kept longer under pressure.
_LOW_FS = 0
_LOW_BROWSER_SIGNAL = 1
_NORMAL = 2
_ESSENTIAL = 3

_ESSENTIAL_SOURCES = {"window", "idle", "system", "meta", "git", "terminal"}


def priority(event: Event) -> int:
    if event.source == "fs":
        return _LOW_FS
    if event.source == "browser" and event.type != "page_visit":
        return _LOW_BROWSER_SIGNAL
    if event.source in _ESSENTIAL_SOURCES:
        return _ESSENTIAL
    return _NORMAL


@dataclass
class BusStats:
    published: int = 0
    dropped: Counter[str] = field(default_factory=Counter)
    high_water: int = 0


class EventBus:
    def __init__(self, maxsize: int = 10_000) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be positive")
        self.maxsize = maxsize
        self._items: deque[Event] = deque()
        self._not_empty = asyncio.Event()
        self.stats = BusStats()

    def __len__(self) -> int:
        return len(self._items)

    def publish(self, event: Event) -> bool:
        """Queue an event. Returns False if this event was the one dropped."""
        if len(self._items) >= self.maxsize:
            victim = self._find_victim(priority(event))
            if victim is None:
                self.stats.dropped[event.source] += 1
                return False
            dropped = self._items[victim]
            del self._items[victim]
            self.stats.dropped[dropped.source] += 1
        self._items.append(event)
        self.stats.published += 1
        self.stats.high_water = max(self.stats.high_water, len(self._items))
        self._not_empty.set()
        return True

    def publish_threadsafe(self, loop: asyncio.AbstractEventLoop, event: Event) -> None:
        """For observers running in worker threads."""
        loop.call_soon_threadsafe(self.publish, event)

    def _find_victim(self, incoming_priority: int) -> int | None:
        best_idx: int | None = None
        best_pri = incoming_priority + 1
        for idx, queued in enumerate(self._items):
            p = priority(queued)
            if p < best_pri:
                best_idx, best_pri = idx, p
                if p == _LOW_FS:
                    break  # oldest event of the lowest possible level
        return best_idx

    async def get_batch(
        self, max_items: int = 200, timeout: float = 1.0, linger: float = 0.0
    ) -> list[Event]:
        """Wait up to `timeout` for the first event, then optionally `linger` to fill the batch."""
        if not self._items:
            self._not_empty.clear()
            try:
                await asyncio.wait_for(self._not_empty.wait(), timeout)
            except TimeoutError:
                return []
        if linger > 0 and len(self._items) < max_items:
            await asyncio.sleep(linger)
        return self._take(max_items)

    def drain(self) -> list[Event]:
        return self._take(len(self._items))

    def purge(self, predicate: Callable[[Event], bool]) -> int:
        """Remove queued events matching `predicate` (used by forget). Returns the count."""
        kept = deque(e for e in self._items if not predicate(e))
        removed = len(self._items) - len(kept)
        self._items = kept
        return removed

    def _take(self, n: int) -> list[Event]:
        batch = [self._items.popleft() for _ in range(min(n, len(self._items)))]
        if not self._items:
            self._not_empty.clear()
        return batch
