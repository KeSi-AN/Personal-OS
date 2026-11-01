"""UUIDv7 (RFC 9562): 48-bit Unix ms timestamp, then random bits. Sorts by creation time."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from uuid import UUID

_lock = threading.Lock()
_last_ms = 0
_last_seq = 0


def uuid7(at: datetime | None = None) -> UUID:
    """New UUIDv7. With `at`, the timestamp part is taken from that time (simulator, replays).

    Without `at`, ids from this process are strictly increasing even within one millisecond
    (the 12-bit rand_a field is used as a counter, RFC 9562 method 1).
    """
    rand = int.from_bytes(os.urandom(10), "big")
    rand_b = rand & ((1 << 62) - 1)
    if at is not None:
        ms = int(at.timestamp() * 1000)
        rand_a = (rand >> 62) & 0xFFF
    else:
        global _last_ms, _last_seq
        with _lock:
            ms = time.time_ns() // 1_000_000
            if ms <= _last_ms:
                ms = _last_ms
                _last_seq += 1
                if _last_seq > 0xFFF:
                    ms += 1
                    _last_seq = 0
            else:
                _last_seq = (rand >> 62) & 0x7FF  # random start, leaves headroom for the counter
            _last_ms = ms
            rand_a = _last_seq
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= rand_a << 64
    value |= 0b10 << 62
    value |= rand_b
    return UUID(int=value)


def uuid7_ms(u: UUID) -> int:
    """The millisecond timestamp stored in a UUIDv7."""
    return u.int >> 80
