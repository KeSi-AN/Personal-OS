"""Who may use the dashboard (DESIGN.md §6.4).

- On the laptop: `personal-os dashboard` (or the tray) stores a one-time login code; opening
  the link turns it into a session cookie. Sessions live in the daemon's memory.
- From a phone: pairing turns a 6-digit code (shown on the laptop, valid 5 minutes) into a
  long-lived device token. Only a hash of the token is stored; a device can be revoked.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from personal_os.core.clock import Clock, from_ms, to_ms
from personal_os.core.ids import uuid7
from personal_os.storage import repo

LOGIN_KEY = "web.login_codes"
LOGIN_TTL = timedelta(minutes=2)
SESSION_TTL = timedelta(hours=12)
PAIRING_TTL = timedelta(minutes=5)
PAIRING_TRIES = 5


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- laptop login ------------------------------------------------------------------------------


def new_login_code(conn: sqlite3.Connection, clock: Clock) -> str:
    """A one-time code for a login link; stored hashed in the shared database."""
    code = secrets.token_urlsafe(24)
    now = clock.now()
    codes = {
        h: exp
        for h, exp in (repo.get_setting(conn, LOGIN_KEY, {}) or {}).items()
        if exp > to_ms(now)
    }
    codes[_hash(code)] = to_ms(now + LOGIN_TTL)
    repo.set_setting(conn, LOGIN_KEY, codes, clock)
    return code


def use_login_code(conn: sqlite3.Connection, clock: Clock, code: str) -> bool:
    codes = repo.get_setting(conn, LOGIN_KEY, {}) or {}
    h = _hash(code)
    expires = codes.pop(h, None)
    repo.set_setting(conn, LOGIN_KEY, codes, clock)
    return expires is not None and expires > to_ms(clock.now())


class Sessions:
    """Session cookies of the laptop dashboard (in memory: a restart asks you to log in again)."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[str, datetime] = {}

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[_hash(token)] = self.clock.now() + SESSION_TTL
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            expires = self._sessions.get(_hash(token))
            return expires is not None and expires > self.clock.now()


# --- phones -----------------------------------------------------------------------------------


@dataclass
class Device:
    id: str
    name: str
    created: datetime
    last_seen: datetime | None
    revoked: bool


class Pairing:
    """One pairing code at a time, valid for 5 minutes and a few tries."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._code: str | None = None
        self._expires: datetime | None = None
        self._tries = 0

    def start(self) -> str:
        with self._lock:
            self._code = f"{secrets.randbelow(1_000_000):06d}"
            self._expires = self.clock.now() + PAIRING_TTL
            self._tries = 0
            return self._code

    def active(self) -> tuple[str, datetime] | None:
        with self._lock:
            if self._code and self._expires and self._expires > self.clock.now():
                return self._code, self._expires
            return None

    def redeem(self, code: str) -> bool:
        with self._lock:
            if not self._code or not self._expires or self._expires <= self.clock.now():
                return False
            self._tries += 1
            ok = hmac.compare_digest(code.strip(), self._code)
            if ok or self._tries >= PAIRING_TRIES:
                self._code = None  # one use, and guessing burns the code
            return ok


def add_device(conn: sqlite3.Connection, clock: Clock, name: str) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO devices (id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
        (str(uuid7(clock.now())), name[:60] or "phone", _hash(token), to_ms(clock.now())),
    )
    return token


def device_for(conn: sqlite3.Connection, clock: Clock, token: str | None) -> str | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT id FROM devices WHERE token_hash = ? AND revoked = 0", (_hash(token),)
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (to_ms(clock.now()), row[0]))
    return str(row[0])


def devices(conn: sqlite3.Connection) -> list[Device]:
    return [
        Device(r[0], r[1], from_ms(r[2]), from_ms(r[3]) if r[3] else None, bool(r[4]))
        for r in conn.execute(
            "SELECT id, name, created_at, last_seen, revoked FROM devices ORDER BY created_at"
        )
    ]


def revoke(conn: sqlite3.Connection, device_id: str) -> bool:
    cur = conn.execute("UPDATE devices SET revoked = 1 WHERE id = ?", (device_id,))
    return cur.rowcount == 1
