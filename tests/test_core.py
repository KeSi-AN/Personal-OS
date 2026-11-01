from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from personal_os.core.bus import EventBus
from personal_os.core.clock import SimClock, from_ms, to_ms
from personal_os.core.events import (
    Event,
    FsChange,
    PageExtract,
    PageVisit,
    TerminalCommand,
    WindowFocus,
)
from personal_os.core.ids import uuid7, uuid7_ms
from tests.conftest import T0


def focus(title: str = "x", at=T0) -> Event:
    return Event.point(WindowFocus(app="Code", title=title), at, observer_version="t")


def fs(at=T0) -> Event:
    return Event.point(FsChange(path="a.py", change="modified"), at, observer_version="t")


# --- ids / clock ---------------------------------------------------------------------------


def test_uuid7_is_version_7_and_carries_timestamp():
    u = uuid7(T0)
    assert u.version == 7
    assert uuid7_ms(u) == to_ms(T0)


def test_uuid7_monotonic_without_timestamp():
    ids = [uuid7() for _ in range(5000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)


def test_ms_roundtrip():
    assert from_ms(to_ms(T0)) == T0


async def test_sim_clock_sleep_advances_instantly():
    clock = SimClock(T0)
    await clock.sleep(90)
    assert clock.now() == T0 + timedelta(seconds=90)
    assert clock.monotonic() == 90
    with pytest.raises(ValueError):
        clock.advance(-1)


# --- events --------------------------------------------------------------------------------


def test_event_normalises_to_utc_and_keeps_local_offset():
    e = focus()
    assert e.ts_start.utcoffset() == timedelta(0)
    assert e.tz_offset_min == 330
    assert e.source == "window" and e.type == "focus" and e.kind == "window.focus"


def test_event_rejects_kind_mismatch_and_negative_interval():
    payload = WindowFocus(app="Code")
    with pytest.raises(ValidationError):
        Event(id=uuid7(), ts_start=T0, source="fs", type="change", payload=payload,
              observer_version="t")  # fmt: skip
    with pytest.raises(ValidationError):
        Event.interval(payload, T0, T0 - timedelta(seconds=1), observer_version="t")


def test_event_json_roundtrip_with_nested_extract():
    e = Event.interval(
        PageVisit(url="https://x.dev/a", title="A", extract=PageExtract(main_text="hello")),
        T0,
        T0 + timedelta(minutes=2),
        observer_version="t",
    )
    back = Event.model_validate_json(e.model_dump_json())
    assert back == e
    assert isinstance(back.payload, PageVisit)
    assert back.payload.extract is not None and back.payload.extract.main_text == "hello"


def test_naive_datetimes_rejected():
    with pytest.raises(ValidationError):
        Event.point(WindowFocus(app="x"), T0.replace(tzinfo=None), observer_version="t")


def test_search_text_covers_text_url_and_path_fields():
    e = Event.point(TerminalCommand(command="pytest -q", cwd="C:/proj"), T0, observer_version="t")
    assert "pytest -q" in e.payload.search_text()
    assert "C:/proj" in e.payload.search_text()


# --- bus -----------------------------------------------------------------------------------


async def test_bus_batches_in_order():
    bus = EventBus()
    events = [focus(str(i)) for i in range(5)]
    for e in events:
        assert bus.publish(e)
    assert await bus.get_batch(3) == events[:3]
    assert await bus.get_batch(10) == events[3:]
    assert await bus.get_batch(10, timeout=0.01) == []


async def test_bus_wakes_waiting_consumer():
    bus = EventBus()
    waiter = asyncio.create_task(bus.get_batch(10, timeout=1))
    await asyncio.sleep(0)
    bus.publish(focus())
    assert len(await waiter) == 1


def test_bus_drops_low_priority_first_and_counts():
    bus = EventBus(maxsize=3)
    bus.publish(fs())
    bus.publish(focus("a"))
    bus.publish(focus("b"))
    assert bus.publish(focus("c"))  # evicts the fs event
    assert bus.stats.dropped == {"fs": 1}
    assert not bus.publish(fs())  # everything queued is more important
    assert bus.stats.dropped == {"fs": 2}
    assert [e.payload.title for e in bus.drain()] == ["a", "b", "c"]  # type: ignore[union-attr]


def test_bus_purge():
    bus = EventBus()
    bus.publish(focus("keep"))
    bus.publish(fs())
    assert bus.purge(lambda e: e.source == "fs") == 1
    assert len(bus) == 1
