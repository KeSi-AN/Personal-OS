"""Shared spool reader for producers outside the daemon (shell hooks, browser native host).

Writers append one JSON object per line to <spool>/<name>.jsonl. To read without racing a
writer, a file is first renamed (atomic; the writer's next append simply creates a new file),
then read in full and deleted. Files left half-claimed by a crashed daemon are read next time.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from personal_os.core.logging import get_logger

_log = get_logger("observers.spool")


@dataclass
class SpoolStats:
    ingested: int = 0
    malformed: int = 0
    private_skipped: int = 0


class Skip(Exception):
    """Raised by a record parser for records deliberately not ingested (counted, not an error)."""


def drain[T](
    spool: Path, parse: Callable[[dict[str, Any]], T | None], stats: SpoolStats
) -> list[T]:
    """Claim and parse every spool file. `parse` returns None for malformed records and raises
    Skip for records that must be dropped on purpose (e.g. private commands)."""
    out: list[T] = []
    if not spool.is_dir():
        return out
    for entry in sorted(spool.glob("*.jsonl")):
        claimed = entry.with_name(f"{entry.name}.{os.getpid()}.ingesting")
        try:
            entry.replace(claimed)
        except OSError:
            continue  # being written right now; next round
        out.extend(_read(claimed, parse, stats))
    for leftover in spool.glob("*.ingesting"):
        out.extend(_read(leftover, parse, stats))
    return out


def _read[T](path: Path, parse: Callable[[dict[str, Any]], T | None], stats: SpoolStats) -> list[T]:
    out: list[T] = []
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    stats.malformed += 1
                    continue
                if not isinstance(rec, dict):
                    stats.malformed += 1
                    continue
                try:
                    item = parse(rec)
                except Skip:
                    stats.private_skipped += 1
                    continue
                except (ValueError, TypeError, KeyError):
                    item = None
                if item is None:
                    stats.malformed += 1
                    continue
                out.append(item)
        path.unlink(missing_ok=True)
    except OSError:
        _log.warning("could not read spool file", extra={"fields": {"file": path.name}})
    stats.ingested += len(out)
    return out
