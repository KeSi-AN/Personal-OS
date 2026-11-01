"""JSON-lines logging.

Rule: logs never contain captured content (titles, URLs, commands, text). Log event
references (`event_ref`) and counts only, so a log file can be shared when debugging.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from personal_os.core.events import Event


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            entry.update(extra)
        if record.exc_info and record.exc_info[0] is not None:
            # Type and message only; tracebacks can carry local values from event payloads.
            entry["exc_type"] = record.exc_info[0].__name__
        return json.dumps(entry, default=str)


def setup_logging(logs_dir: Path | None, level: int = logging.INFO) -> None:
    root = logging.getLogger("personal_os")
    root.setLevel(level)
    root.handlers.clear()
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            logs_dir / "personal_os.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"personal_os.{name}")


def event_ref(event: Event) -> dict[str, str]:
    """The only event fields allowed in a log line."""
    return {"event_id": str(event.id), "source": event.source, "type": event.type}
