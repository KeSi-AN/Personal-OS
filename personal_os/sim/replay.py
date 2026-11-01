"""Record/replay event streams as JSON lines, and feed streams through the real pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator
from pathlib import Path

from personal_os.core.bus import EventBus
from personal_os.core.events import Event
from personal_os.core.pipeline import Pipeline


def dump_jsonl(events: Iterable[Event], path: Path) -> int:
    n = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for event in events:
            fh.write(event.model_dump_json())
            fh.write("\n")
            n += 1
    return n


def load_jsonl(path: Path) -> Iterator[Event]:
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield Event.model_validate_json(line)
            except ValueError as exc:
                raise ValueError(f"{path}:{lineno}: invalid event: {exc}") from exc


async def feed_through_bus(
    events: Iterable[Event], pipeline: Pipeline, bus: EventBus | None = None
) -> int:
    """Publish events onto a bus while the pipeline consumes it, respecting backpressure.

    Exercises exactly the live path (bus → filter → writer). Returns events stored.
    """
    bus = bus or EventBus()
    stop = asyncio.Event()
    consumer = asyncio.create_task(pipeline.run(bus, stop, timeout_s=0.05, linger_s=0.0))
    for event in events:
        while len(bus) >= bus.maxsize:
            await asyncio.sleep(0.001)
        bus.publish(event)
        if len(bus) % 100 == 0:
            await asyncio.sleep(0)
    stop.set()
    await consumer
    return pipeline.stats.stored
