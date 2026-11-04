"""Google Calendar and Gmail, read-only (DESIGN.md §6.5, ADR-019).

- Sign-in: the installed-app loopback flow with PKCE, from an OAuth client you create
  (docs/GOOGLE.md). Your browser talks to Google; this code only sees the one-time code.
- Scopes: calendar.readonly and gmail.metadata (headers only: Google won't give this client
  message bodies at all).
- The refresh token lives in Windows Credential Manager (keyring), never in a file.
- Nothing fetched is stored in the database: it's read when a briefing or question needs it.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

KEYRING_SERVICE = "personal-os"
TOKEN_KEY = "google-refresh-token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/gmail.metadata",
]
CAL_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GMAIL_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"


class GoogleError(RuntimeError):
    pass


@dataclass
class CalendarEvent:
    title: str
    start: datetime | None
    end: datetime | None
    all_day: bool
    location: str = ""


@dataclass
class MailHeader:
    sender: str
    subject: str


@dataclass
class Inbox:
    unread: int
    top: list[MailHeader]


def secret_path(data_dir: Path) -> Path:
    return data_dir / "google" / "client_secret.json"


def _client(data_dir: Path) -> tuple[str, str]:
    path = secret_path(data_dir)
    if not path.exists():
        raise GoogleError("not set up: run `personal-os google connect CLIENT_SECRET.json`")
    raw = json.loads(path.read_text("utf-8"))
    info = raw.get("installed") or raw.get("web") or {}
    if not info.get("client_id"):
        raise GoogleError("that file isn't a Google OAuth client (Desktop app) secret")
    return info["client_id"], info.get("client_secret", "")


def _keyring() -> Any:
    import keyring

    return keyring


def connected(data_dir: Path) -> bool:
    try:
        return secret_path(data_dir).exists() and bool(
            _keyring().get_password(KEYRING_SERVICE, TOKEN_KEY)
        )
    except Exception:
        return False


def _post(url: str, form: dict[str, str]) -> dict[str, Any]:
    data = urllib.parse.urlencode(form).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data), timeout=20) as resp:
            result: dict[str, Any] = json.load(resp)
            return result
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("error", "")
        except ValueError:
            detail = ""
        raise GoogleError(f"Google answered {exc.code} {detail}".strip()) from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise GoogleError(f"Google unreachable: {exc}") from None


# --- connecting ------------------------------------------------------------------------------


def connect(
    data_dir: Path, client_secret_file: Path, open_browser: Any, timeout_s: int = 300
) -> None:
    """Copy the client secret, sign in through the browser, keep the refresh token."""
    dest = secret_path(data_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if client_secret_file.resolve() != dest.resolve():
        shutil.copyfile(client_secret_file, dest)
    client_id, client_secret = _client(data_dir)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    state = secrets.token_urlsafe(16)
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if q.get("state", [""])[0] == state:
                result.update({k: v[0] for k, v in q.items()})
                done.set()
            ok = "code" in result
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Connected. You can close this tab." if ok else "Sign-in didn't complete."
            self.wfile.write(f"<p style='font:16px system-ui'>Personal OS: {msg}</p>".encode())

        def log_message(self, *_args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    open_browser(f"{AUTH_URL}?{urllib.parse.urlencode(params)}")
    try:
        if not done.wait(timeout_s):
            raise GoogleError("sign-in timed out")
    finally:
        server.shutdown()
    if "code" not in result:
        raise GoogleError(f"sign-in refused: {result.get('error', 'unknown')}")
    tokens = _post(
        TOKEN_URL,
        {
            "code": result["code"],
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
    )
    refresh = tokens.get("refresh_token")
    if not refresh:
        raise GoogleError("Google didn't return a refresh token; disconnect and connect again")
    _keyring().set_password(KEYRING_SERVICE, TOKEN_KEY, refresh)


def disconnect(data_dir: Path) -> bool:
    """Revoke at Google and forget the token here. Returns whether Google confirmed."""
    kr = _keyring()
    token = kr.get_password(KEYRING_SERVICE, TOKEN_KEY)
    revoked = False
    if token:
        try:
            _post(REVOKE_URL, {"token": token})
            revoked = True
        except GoogleError:
            revoked = False
        kr.delete_password(KEYRING_SERVICE, TOKEN_KEY)
    return revoked


# --- reading ----------------------------------------------------------------------------------


class Google:
    """Short-lived access tokens from the stored refresh token; read-only calls."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._token: str | None = None
        self._expires = 0.0

    def _access_token(self) -> str:
        if self._token and time.monotonic() < self._expires - 60:
            return self._token
        client_id, client_secret = _client(self.data_dir)
        refresh = _keyring().get_password(KEYRING_SERVICE, TOKEN_KEY)
        if not refresh:
            raise GoogleError("not connected: run `personal-os google connect`")
        tokens = _post(
            TOKEN_URL,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
        )
        self._token = tokens["access_token"]
        self._expires = time.monotonic() + int(tokens.get("expires_in", 3600))
        return self._token

    def _get(self, url: str, params: list[tuple[str, str]]) -> dict[str, Any]:
        full = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            full, headers={"Authorization": f"Bearer {self._access_token()}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result: dict[str, Any] = json.load(resp)
                return result
        except urllib.error.HTTPError as exc:
            raise GoogleError(f"Google answered {exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GoogleError(f"Google unreachable: {exc}") from None

    def calendar(self, start: datetime, end: datetime, limit: int = 20) -> list[CalendarEvent]:
        data = self._get(
            CAL_URL,
            [
                ("timeMin", start.isoformat()),
                ("timeMax", end.isoformat()),
                ("singleEvents", "true"),
                ("orderBy", "startTime"),
                ("maxResults", str(limit)),
            ],
        )
        out = []
        for item in data.get("items", []):
            s, e = item.get("start", {}), item.get("end", {})
            all_day = "date" in s and "dateTime" not in s
            out.append(
                CalendarEvent(
                    title=item.get("summary") or "(no title)",
                    start=_parse(s.get("dateTime") or s.get("date")),
                    end=_parse(e.get("dateTime") or e.get("date")),
                    all_day=all_day,
                    location=item.get("location") or "",
                )
            )
        return out

    def inbox(self, top: int = 3) -> Inbox:
        """Unread mail in the primary inbox: a count and the newest senders and subjects."""
        listing = self._get(
            GMAIL_URL,
            [
                ("labelIds", "INBOX"),
                ("labelIds", "UNREAD"),
                ("labelIds", "CATEGORY_PERSONAL"),
                ("maxResults", "50"),
            ],
        )
        ids = [m["id"] for m in listing.get("messages", [])]
        headers = []
        for mid in ids[:top]:
            msg = self._get(
                f"{GMAIL_URL}/{mid}",
                [
                    ("format", "metadata"),
                    ("metadataHeaders", "From"),
                    ("metadataHeaders", "Subject"),
                ],
            )
            h = {x["name"].lower(): x["value"] for x in msg.get("payload", {}).get("headers", [])}
            headers.append(
                MailHeader(_sender_name(h.get("from", "")), h.get("subject", "(no subject)"))
            )
        count = int(listing.get("resultSizeEstimate", len(ids)))
        return Inbox(max(count, len(ids)), headers)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    if len(value) == 10:
        return datetime.fromisoformat(value).astimezone()
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _sender_name(raw: str) -> str:
    """'Asha Rao <asha@example.com>' -> 'Asha Rao' (the address isn't needed)."""
    name = raw.split("<", 1)[0].strip().strip('"')
    return name or raw.strip("<>").split("@", 1)[0]


def today_range(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone()
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)
