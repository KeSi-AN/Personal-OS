"""bus → privacy filter → project hint → writer (DESIGN.md §4.2).

Live capture, the simulator and replays all go through this same pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from personal_os.core.bus import EventBus
from personal_os.core.clock import Clock
from personal_os.core.events import Event
from personal_os.privacy.filter import PrivacyFilter
from personal_os.storage import repo
from personal_os.storage.writer import EventWriter

HintFn = Callable[[Event], Event]
_log = logging.getLogger("personal_os.pipeline")
RETRY_FIRST_S = 0.5
RETRY_MAX_S = 30.0
STOP_TRIES = 3  # once stopping, a batch that still can't be written is given up after this


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
    failed: int = 0  # events in batches that could never be written (a filter bug)
    retries: int = 0  # writes tried again (the database was locked, the disk full...)
    last_error: str = ""
    last_error_at: float = 0.0  # time.time() of last_error


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
        kept, dropped = self.prepare(events)
        return self.store(kept, dropped)

    def prepare(self, events: Iterable[Event]) -> tuple[list[Event], list[Any]]:
        """Filter and hint a batch (counted once): what to write, and ids to retract."""
        batch = list(events)
        self.stats.received += len(batch)
        if self.forgotten:
            before = len(batch)
            batch = [e for e in batch if not self._is_forgotten(e)]
            self.forget_dropped += before - len(batch)
        kept = [self.hint(e) for e in self.privacy.apply_many(batch)]
        kept_ids = {e.id for e in kept}
        dropped = [e.id for e in batch if e.id not in kept_ids and e.source != "meta"]
        return kept, dropped

    def store(self, kept: list[Event], dropped: list[Any]) -> int:
        """Write a prepared batch in one transaction (safe to try again: rows are upserts)."""
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
        """Consume until `stop` is set and the bus is empty. A write that fails is tried
        again with backoff, keeping the batch, while the bus buffers (DESIGN.md §3.6); before,
        one error ("database is locked" while voice held the write lock) ended this task and
        capture stopped silently while every observer still showed "running" (audit B1)."""
        self.bus = bus
        while not (stop.is_set() and len(bus) == 0):
            batch = await bus.get_batch(max_batch, timeout=timeout_s, linger=linger_s)
            if not batch:
                continue
            self.busy = True
            try:
                await self._write_with_retry(batch, stop)
            finally:
                self.busy = False

    async def _write_with_retry(self, batch: list[Event], stop: asyncio.Event) -> None:
        try:
            kept, dropped = await asyncio.to_thread(self.prepare, batch)
        except Exception as exc:  # the filter itself failed: nothing to retry
            self._failed(len(batch), exc)
            return
        wait, tries = RETRY_FIRST_S, 0
        while True:
            try:
                await asyncio.to_thread(self.store, kept, dropped)
                return
            except ValueError as exc:  # a blocked event reached the writer: never retried
                self._failed(len(kept), exc)
                return
            except Exception as exc:  # locked, busy, disk full: keep the batch and wait
                tries += 1
                self.stats.retries += 1
                self._note(exc)
                _log.warning(
                    "write failed, retrying",
                    extra={"fields": {"exc_type": type(exc).__name__, "wait_s": wait}},
                )
                if stop.is_set() and tries >= STOP_TRIES:
                    self._failed(len(kept), exc)
                    return
                await asyncio.sleep(wait)
                wait = min(wait * 2, RETRY_MAX_S)

    def _note(self, exc: BaseException) -> None:
        self.stats.last_error = f"{type(exc).__name__}: {exc}"[:200]
        self.stats.last_error_at = time.time()

    def _failed(self, n: int, exc: BaseException) -> None:
        self.stats.failed += n
        self._note(exc)
        _log.error(
            "batch not written", extra={"fields": {"events": n, "exc_type": type(exc).__name__}}
        )
