"""The single writer: batched, transactional upserts of filtered events (DESIGN.md §4.2)."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import timedelta
from typing import Any
from uuid import UUID

from personal_os.config.schema import RetentionConfig
from personal_os.core.clock import Clock, to_ms
from personal_os.core.events import Event, PageVisit, PrivacyClass, RetentionClass
from personal_os.core.ids import uuid7
from personal_os.storage.db import transaction

_UPSERT_EVENT = """
INSERT INTO events (
    id, source, type, ts_start, ts_end, last_seen, tz_offset_min, project_hint, confidence,
    privacy_class, retention, expires_at, observer_version, truncated, payload, search_text,
    inserted_at, updated_at
) VALUES (
    :id, :source, :type, :ts_start, :ts_end, :last_seen, :tz_offset_min, :project_hint,
    :confidence, :privacy_class, :retention, :expires_at, :observer_version, :truncated,
    :payload, :search_text, :now, :now
)
ON CONFLICT (id) DO UPDATE SET
    -- never move ts_end backwards, never reopen a closed interval
    ts_end = CASE
        WHEN events.ts_end IS NULL THEN excluded.ts_end
        WHEN excluded.ts_end IS NULL THEN events.ts_end
        ELSE max(events.ts_end, excluded.ts_end) END,
    last_seen = CASE
        WHEN excluded.last_seen IS NULL THEN events.last_seen
        WHEN events.last_seen IS NULL THEN excluded.last_seen
        ELSE max(events.last_seen, excluded.last_seen) END,
    -- privacy class only goes up; a sensitive row never receives free text again
    payload = CASE
        WHEN events.privacy_class = 'sensitive' AND excluded.privacy_class = 'normal'
        THEN events.payload ELSE excluded.payload END,
    search_text = CASE
        WHEN events.privacy_class = 'sensitive' AND excluded.privacy_class = 'normal'
        THEN events.search_text ELSE excluded.search_text END,
    privacy_class = CASE
        WHEN events.privacy_class = 'sensitive' THEN 'sensitive' ELSE excluded.privacy_class END,
    project_hint = coalesce(excluded.project_hint, events.project_hint),
    confidence = excluded.confidence,
    expires_at = min(coalesce(events.expires_at, excluded.expires_at), excluded.expires_at),
    truncated = max(events.truncated, excluded.truncated),
    updated_at = excluded.updated_at
WHERE events.source = excluded.source AND events.type = excluded.type
"""

_UPSERT_EXTRACT = """
INSERT INTO page_extracts (
    id, event_id, ts, content_hash, title, meta_description, headings, main_text, word_count,
    lang, expires_at
) VALUES (
    :id, :event_id, :ts, :content_hash, :title, :meta_description, :headings, :main_text,
    :word_count, :lang, :expires_at
)
ON CONFLICT (event_id) DO UPDATE SET
    content_hash = excluded.content_hash, title = excluded.title,
    meta_description = excluded.meta_description, headings = excluded.headings,
    main_text = excluded.main_text, word_count = excluded.word_count, lang = excluded.lang
"""


class RetentionPolicy:
    def __init__(self, cfg: RetentionConfig) -> None:
        self.cfg = cfg

    def event_expiry(self, event: Event) -> int | None:
        if event.privacy_class == PrivacyClass.SENSITIVE:
            delta: timedelta | None = timedelta(days=self.cfg.sensitive_days)
        else:
            delta = {
                RetentionClass.RAW: timedelta(days=self.cfg.raw_days),
                RetentionClass.PAGE_TEXT: timedelta(days=self.cfg.page_text_days),
                RetentionClass.TERMINAL_OUTPUT: timedelta(days=self.cfg.terminal_output_days),
                RetentionClass.CLIPBOARD: timedelta(hours=self.cfg.clipboard_hours),
                RetentionClass.SUMMARY: None,
            }[event.retention]
        return None if delta is None else to_ms(event.ts_start + delta)

    def extract_expiry(self, event: Event) -> int:
        return to_ms(event.ts_start + timedelta(days=self.cfg.page_text_days))


class EventWriter:
    def __init__(self, conn: sqlite3.Connection, retention: RetentionConfig, clock: Clock) -> None:
        self.conn = conn
        self.retention = RetentionPolicy(retention)
        self.clock = clock

    def write(self, events: Sequence[Event], retract: Sequence[UUID] = ()) -> int:
        """Upsert a batch in one transaction. Blocked events must never reach this point.

        `retract`: ids whose newest version the privacy filter dropped (e.g. a page that turned
        out to have a password field, or a visit that ended as a bounce after an open
        checkpoint was stored). Any stored row with such an id is deleted.
        """
        if not events and not retract:
            return 0
        now = to_ms(self.clock.now())
        with transaction(self.conn) as conn:
            if retract:
                conn.executemany(
                    "DELETE FROM events WHERE id = ? AND source != 'meta'",
                    [(str(i),) for i in retract],
                )
            for event in events:
                if event.privacy_class == PrivacyClass.BLOCKED:
                    raise ValueError("blocked event reached the writer")  # filter bug: fail loudly
                conn.execute(_UPSERT_EVENT, self._event_row(event, now))
                self._write_extract(conn, event)
        return len(events)

    def _event_row(self, event: Event, now: int) -> dict[str, Any]:
        payload = event.payload
        exclude = {"extract"} if isinstance(payload, PageVisit) else None
        if isinstance(payload, PageVisit) and payload.extract is not None:
            payload = payload.model_copy(update={"extract": None})
        return {
            "id": str(event.id),
            "source": str(event.source),
            "type": event.type,
            "ts_start": to_ms(event.ts_start),
            "ts_end": to_ms(event.ts_end) if event.ts_end else None,
            "last_seen": to_ms(event.last_seen) if event.last_seen else None,
            "tz_offset_min": event.tz_offset_min,
            "project_hint": event.project_hint,
            "confidence": event.confidence,
            "privacy_class": str(event.privacy_class),
            "retention": str(event.retention),
            "expires_at": self.retention.event_expiry(event),
            "observer_version": event.observer_version,
            "truncated": int(event.truncated),
            "payload": json.dumps(
                event.payload.model_dump(mode="json", exclude=exclude), separators=(",", ":")
            ),
            "search_text": payload.search_text(),
            "now": now,
        }

    def _write_extract(self, conn: sqlite3.Connection, event: Event) -> None:
        payload = event.payload
        if not isinstance(payload, PageVisit):
            return
        if event.privacy_class != PrivacyClass.NORMAL or payload.extract is None:
            if event.privacy_class != PrivacyClass.NORMAL:
                conn.execute("DELETE FROM page_extracts WHERE event_id = ?", (str(event.id),))
            return
        ex = payload.extract
        conn.execute(
            _UPSERT_EXTRACT,
            {
                "id": str(uuid7(event.ts_start)),
                "event_id": str(event.id),
                "ts": to_ms(event.ts_start),
                "content_hash": ex.content_hash,
                "title": ex.title,
                "meta_description": ex.meta_description,
                "headings": json.dumps(ex.headings),
                "main_text": ex.main_text,
                "word_count": ex.word_count,
                "lang": ex.lang,
                "expires_at": self.retention.extract_expiry(event),
            },
        )
