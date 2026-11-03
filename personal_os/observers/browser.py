"""Browser observer: ingests what the extension sent through the native host (DESIGN.md §4.5).

Records arrive in spool/browser/*.jsonl. A visit record becomes a browser.page_visit interval
(the extension re-sends an open visit about once a minute with the same id: ADR-007 upserts),
plus a browser.search event when the page is a search results page. Selection, copy and
bookmark records become browser signals. Enrichment (content type, GitHub/Stack Overflow/docs/
YouTube details, search queries) happens here, so the extension stays small.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from personal_os.browser.enrich import (
    classify,
    clean_search_url,
    clean_title,
    host_of,
    map_transition,
    search_query,
)
from personal_os.core.events import BrowserSignal, Event, PageExtract, PageVisit
from personal_os.core.observer import BaseObserver, ObserverContext, StateStore
from personal_os.observers.spool import SpoolStats, drain

VERSION = "1.0.0"
RETURNS_KEY = "browser.returns"
MAX_TEXT = 3000
MAX_HEADINGS = 20
_SIGNALS = {"selection", "copy", "bookmark_added"}
_SEARCH_NS = uuid.UUID("6f1c3a52-6d1e-4c8e-9a51-2b7e0c9d4f10")


def _when(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        raise ValueError("naive timestamp")
    return dt.astimezone()


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _int(value: Any, default: int | None = 0) -> int | None:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else default


def _extract(raw: Any) -> PageExtract | None:
    if not isinstance(raw, dict):
        return None
    text = str(raw.get("text") or "")[:MAX_TEXT]
    headings = [str(h)[:200] for h in (raw.get("headings") or []) if str(h).strip()][:MAX_HEADINGS]
    return PageExtract(
        title=str(raw.get("title") or "")[:500],
        meta_description=str(raw.get("metaDescription") or "")[:500],
        headings=headings,
        main_text=text,
        word_count=_int(raw.get("wordCount"), None) or len(text.split()),
        content_hash=hashlib.sha256(text.encode()).hexdigest()[:32],
        lang=str(raw["lang"])[:16] if raw.get("lang") else None,
    )


class Returns:
    """How often a URL was visited in the last 7 days. Keyed by a hash of the URL: the counter
    never holds a URL, so it carries no browsing content of its own."""

    def __init__(self, store: StateStore | None) -> None:
        self.store = store
        self.data: dict[str, list[int]] = (store.get(RETURNS_KEY, {}) if store else {}) or {}
        self.by_visit: dict[str, int] = {}  # re-sends of an open visit get the same answer

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha256(url.split("#", 1)[0].encode()).hexdigest()[:16]

    def count_and_record(self, url: str, visit_id: str, day: int) -> int:
        if visit_id in self.by_visit:
            return self.by_visit[visit_id]
        k = self.key(url)
        days = [d for d in self.data.get(k, []) if d > day - 7]
        previous = len(days)
        days.append(day)
        self.data[k] = days
        if len(self.by_visit) > 20_000:
            self.by_visit.clear()
        self.by_visit[visit_id] = previous
        if len(self.data) > 5000:  # drop the stalest entries
            for stale in sorted(self.data, key=lambda x: max(self.data[x] or [0]))[:1000]:
                del self.data[stale]
        return previous

    def save(self) -> None:
        if self.store is not None:
            self.store.set(RETURNS_KEY, self.data)


def parse_visit(rec: dict[str, Any], returns: Returns | None = None) -> list[Event]:
    url = clean_search_url(str(rec["url"]))
    visit_id = _uuid(rec.get("id"))
    if visit_id is None:
        return []
    start = _when(rec["start"])
    end = _when(rec["end"]) if rec.get("end") else None
    last_seen = _when(rec["last_seen"]) if rec.get("last_seen") else end or start
    hints = rec.get("hints") if isinstance(rec.get("hints"), dict) else {}
    extract = _extract(rec.get("extract"))
    content_type, enrichment = classify(
        url, word_count=extract.word_count if extract else 0, hints=hints
    )
    referrer = str(rec["referrer"]) if rec.get("referrer") else None
    transition = map_transition(rec.get("transition"), rec.get("qualifiers"), referrer)
    sq = search_query(url)
    returned = 0
    if returns is not None:
        returned = returns.count_and_record(url, str(visit_id), int(start.timestamp() // 86400))
    payload = PageVisit(
        url=url,
        domain=host_of(url),
        title=clean_title(str(rec.get("title") or ""))[:500],
        tab_id=_int(rec.get("tabId"), None),
        window_id=_int(rec.get("windowId"), None),
        transition=transition,
        referrer=referrer,
        opener_visit_id=_uuid(rec.get("openerVisitId")),
        chain_id=_uuid(rec.get("chainId")),
        active_dwell_ms=max(0, _int(rec.get("activeMs")) or 0),
        scroll_max_pct=min(100, max(0, v))
        if (v := _int(rec.get("scrollMax"), None)) is not None
        else None,
        video_watched_s=max(0, _int(rec.get("videoS")) or 0),
        bookmarked=bool(rec.get("bookmarked")),
        searched=transition == "search",
        returned_count_7d=returned,
        search_query=sq[1] if sq else None,
        content_type=content_type,
        lang=extract.lang if extract else None,
        incognito=bool(rec.get("incognito")),
        has_password_field=bool(rec.get("hasPassword")),
        extract=extract,
        enrichment={**enrichment, "browser": str(rec.get("browser") or "chromium")[:20]},
    )
    events = [
        Event.interval(
            payload, start, end, id=visit_id, last_seen=last_seen, observer_version=VERSION
        )
    ]
    if sq:
        signal = BrowserSignal(
            kind="browser.search",
            url=url,
            domain=host_of(url),
            engine=sq[0],
            text=sq[1],
            incognito=bool(rec.get("incognito")),
        )
        # Same id for every re-send of this visit, so the search is stored once.
        events.append(
            Event.point(
                signal, start, id=uuid.uuid5(_SEARCH_NS, str(visit_id)), observer_version=VERSION
            )
        )
    return events


def parse_signal(rec: dict[str, Any]) -> list[Event]:
    kind = rec.get("kind")
    if kind not in _SIGNALS:
        return []
    url = str(rec["url"]) if rec.get("url") else None
    payload = BrowserSignal(
        kind=f"browser.{kind}",
        url=url,
        domain=host_of(url) if url else "",
        text=str(rec.get("text") or "")[:500],
        incognito=bool(rec.get("incognito")),
    )
    return [Event.point(payload, _when(rec["at"]), observer_version=VERSION)]


def parse_record(rec: dict[str, Any], returns: Returns | None = None) -> list[Event] | None:
    kind = rec.get("type")
    if kind == "visit":
        events = parse_visit(rec, returns)
    elif kind == "signal":
        events = parse_signal(rec)
    else:
        return None
    return events or None


def ingest(spool: Path, stats: SpoolStats, returns: Returns | None = None) -> list[Event]:
    batches = drain(spool, lambda r: parse_record(r, returns), stats)
    return [e for batch in batches for e in batch]


class BrowserObserver(BaseObserver):
    name = "browser"
    version = VERSION

    def __init__(self, spool_dir: Path, *, poll_s: float = 1.0) -> None:
        super().__init__()
        self.spool_dir = spool_dir
        self.poll_s = poll_s
        self.stats = SpoolStats()
        self.returns: Returns | None = None
        self.last_record_at: float | None = None

    async def run(self, ctx: ObserverContext) -> None:
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.returns = Returns(ctx.state)
        while True:
            if not ctx.paused.is_set():
                events = await asyncio.to_thread(ingest, self.spool_dir, self.stats, self.returns)
                if events:
                    self.last_record_at = time.time()
                    self.returns.save()
                for event in events:
                    self.emit(event)
            await ctx.clock.sleep(self.poll_s)

    def describe(self) -> dict[str, object]:
        return {"ingested": self.stats.ingested, "malformed": self.stats.malformed}
