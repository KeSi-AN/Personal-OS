"""Recompute active time of stored browser visits from the stored window record.

Live ingestion already does this (observers/browser.py); this brings visits stored earlier,
with the extension's own estimate, in line. Safe to run more than once.
"""

from __future__ import annotations

import json
import sqlite3

from personal_os.core.clock import from_ms
from personal_os.observers.browser import (
    _BROWSER_EXES,
    MIN_TRUSTED_WINDOW_MS,
    window_figure_usable,
)
from personal_os.observers.window import Interval, overlap_ms
from personal_os.storage.db import transaction


def recompute_dwell(
    conn: sqlite3.Connection, min_trusted_ms: int = MIN_TRUSTED_WINDOW_MS
) -> tuple[int, int]:
    """Returns (visits updated, visits checked)."""
    windows: list[Interval] = []
    for start, end, payload in conn.execute(
        "SELECT ts_start, ts_end, payload FROM events "
        "WHERE source = 'window' AND type = 'focus' AND ts_end IS NOT NULL"
    ):
        p = json.loads(payload)
        windows.append(
            ((p.get("exe") or "").lower(), p.get("title", ""), from_ms(start), from_ms(end))
        )
    visits = conn.execute(
        "SELECT e.id, e.ts_start, coalesce(e.ts_end, e.last_seen, e.ts_start), e.payload, x.title "
        "FROM events e LEFT JOIN page_extracts x ON x.event_id = e.id "
        "WHERE e.source = 'browser' AND e.type = 'page_visit'"
    ).fetchall()
    updated = 0
    with transaction(conn):
        for event_id, start, end, payload, extract_title in visits:
            p = json.loads(payload)
            enrichment = p.get("enrichment") or {}
            if enrichment.get("dwell_source") == "window":
                # An earlier run may have stored a partial title match as a near-zero figure.
                reported = int(enrichment.get("extension_active_ms") or 0)
                if window_figure_usable(p.get("active_dwell_ms", 0), reported, min_trusted_ms):
                    continue
                enrichment["dwell_source"] = "extension"
                p["active_dwell_ms"] = reported
                p["enrichment"] = enrichment
                conn.execute(
                    "UPDATE events SET payload = ? WHERE id = ?",
                    (json.dumps(p, separators=(",", ":")), event_id),
                )
                updated += 1
                continue
            exes = _BROWSER_EXES.get(
                str(enrichment.get("browser")), {"chrome.exe", "brave.exe", "msedge.exe"}
            )
            titles = {t for t in (p.get("title"), extract_title) if t}
            measured = overlap_ms(windows, exes, titles, from_ms(start), from_ms(end))
            if not window_figure_usable(measured, p.get("active_dwell_ms", 0), min_trusted_ms):
                continue
            enrichment["extension_active_ms"] = p.get("active_dwell_ms", 0)
            enrichment["dwell_source"] = "window"
            p["enrichment"] = enrichment
            p["active_dwell_ms"] = measured
            conn.execute(
                "UPDATE events SET payload = ? WHERE id = ?",
                (json.dumps(p, separators=(",", ":")), event_id),
            )
            updated += 1
    return updated, len(visits)
