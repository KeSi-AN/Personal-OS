"""bus → privacy filter → project hint → writer (DESIGN.md §4.2).

Live capture, the simulator and replays all go through this same pipeline.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from personal_os.core.bus import EventBus
from personal_os.core.clock import Clock
from personal_os.core.events import Event
from personal_os.privacy.filter import PrivacyFilter
from personal_os.storage import repo
from personal_os.storage.writer import EventWriter

HintFn = Callable[[Event], Event]


def rule_project_hint(event: Event) -> Event:
    """Cheap, rules-only hint at ingest: the repo root or workspace the event names.

    The real project resolver (phase 3) runs later in the background and may override it.
    """
    if event.project_hint:
        return event
    p = event.payload
    hint = getattr(p, "repo_root", None) or getattr(p, "workspace", None)
    return event.model_copy(update={"project_hint": hint}) if hint else event


@dataclass
class PipelineStats:
    received: int = 0
    stored: int = 0


class Pipeline:
    def __init__(
        self,
        privacy: PrivacyFilter,
        writer: EventWriter,
        clock: Clock,
        *,
        hint: HintFn = rule_project_hint,
        bus: EventBus | None = None,
    ) -> None:
        self.privacy = privacy
        self.writer = writer
        self.clock = clock
        self.hint = hint
        self.bus = bus
        self.stats = PipelineStats()
        self._flushed: dict[str, Counter[str]] = {}
        self.forgotten: list[tuple[datetime, datetime]] = []
        self.busy = False  # a batch is being written (the daemon waits on this when pausing)
        self.forget_dropped = 0

    @property
    def conn(self) -> sqlite3.Connection:
        return self.writer.conn

    def add_forgotten(self, start: datetime, end: datetime) -> None:
        """After a forget, drop late arrivals inside the window (spooled commands, in-flight
        checkpoints). Windows older than a day are discarded."""
        horizon = self.clock.now() - timedelta(days=1)
        self.forgotten = [(s, e) for s, e in self.forgotten if e >= horizon] + [(start, end)]

    def _is_forgotten(self, event: Event) -> bool:
        if event.source == "meta" or not self.forgotten:
            return False
        last = event.ts_end or event.last_seen or event.ts_start
        return any(event.ts_start < end and last >= start for start, end in self.forgotten)

    def process(self, events: Iterable[Event]) -> int:
        batch = list(events)
        self.stats.received += len(batch)
        if self.forgotten:
            before = len(batch)
            batch = [e for e in batch if not self._is_forgotten(e)]
            self.forget_dropped += before - len(batch)
        kept = [self.hint(e) for e in self.privacy.apply_many(batch)]
        kept_ids = {e.id for e in kept}
        dropped = [e.id for e in batch if e.id not in kept_ids and e.source != "meta"]
        stored = self.writer.write(kept, retract=dropped)
        self.stats.stored += stored
        self.flush_counters()
        return stored

    def flush_counters(self) -> None:
        """Persist counter deltas (counts only) so `doctor`/`inspect` survive restarts."""
        c = self.privacy.counters
        current: dict[str, Counter[str]] = {
            "filter.blocked": Counter(c.blocked),
            "filter.sensitive": Counter({str(k): v for k, v in c.sensitive.items()}),
            "filter.errors": Counter({str(k): v for k, v in c.errors.items()}),
            "filter.redactions": Counter(c.redactions),
            "filter.paused": Counter({"events": c.paused}),
        }
        if self.bus is not None:
            current["bus.dropped"] = Counter(self.bus.stats.dropped)
        day = self.clock.now().astimezone().date().isoformat()
        for scope, values in current.items():
            delta = values - self._flushed.get(scope, Counter())
            if delta:
                repo.add_counters(self.conn, day, scope, delta)
            self._flushed[scope] = values

    async def run(
        self,
        bus: EventBus,
        stop: asyncio.Event,
        *,
        max_batch: int = 200,
        timeout_s: float = 1.0,
        linger_s: float = 0.5,
    ) -> None:
        """Consume until `stop` is set and the bus is empty."""
        self.bus = bus
        while not (stop.is_set() and len(bus) == 0):
            batch = await bus.get_batch(max_batch, timeout=timeout_s, linger=linger_s)
            if batch:
                self.busy = True
                try:
                    await asyncio.to_thread(self.process, batch)
                finally:
                    self.busy = False
