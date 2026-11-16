"""Windows' media sessions: the players the volume flyout shows (Spotify's app, an installed web
app such as YouTube Music, a browser), each with its app id, what's playing, and play, pause,
next and previous (Windows.Media.Control, through pywinrt). ADR-084.

A session's id is its app's: "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify" for Spotify's app,
"Brave._crx_cinhimbnkkghhklpknlkffjgod" for YouTube Music installed from Brave, plain "Brave"
for a browser tab. That's how a player is matched to the music source you chose.

Every call runs on its own short-lived thread with a time limit, so a stuck Windows call can't
hang voice or the island. Without pywinrt (another OS), there are no sessions.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

PLAYING = 4  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING
TIMEOUT_S = 3.0
ACTIONS = ("play", "pause", "toggle", "next", "previous")


@dataclass(frozen=True)
class Session:
    app_id: str
    title: str
    artist: str
    playing: bool


def available() -> bool:
    try:
        import winrt.windows.media.control  # noqa: F401
    except ImportError:
        return False
    return True


def _run[T](make: Callable[[], Awaitable[T]], timeout_s: float) -> T | None:
    """Run a coroutine to the end on a thread of its own; None if it fails or takes too long."""
    box: list[T] = []

    def go() -> None:
        with contextlib.suppress(Exception):  # a failed Windows call: no answer, not a crash
            box.append(asyncio.run(make()))  # type: ignore[arg-type]

    t = threading.Thread(target=go, name="pos-media-session", daemon=True)
    t.start()
    t.join(timeout_s)
    return box[0] if box else None


async def _manager() -> Any:
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as Manager,
    )

    return await Manager.request_async()


async def _describe(s: Any) -> Session:
    props = await s.try_get_media_properties_async()
    info = s.get_playback_info()
    return Session(
        app_id=str(s.source_app_user_model_id or ""),
        title=str(getattr(props, "title", "") or "")[:200],
        artist=str(getattr(props, "artist", "") or "")[:120],
        playing=int(getattr(info, "playback_status", 0) or 0) == PLAYING,
    )


def sessions(timeout_s: float = TIMEOUT_S) -> list[Session]:
    """Every media session now, as Windows lists them."""
    if not available():
        return []

    async def all_sessions() -> list[Session]:
        mgr = await _manager()
        return [await _describe(s) for s in mgr.get_sessions()]

    return _run(all_sessions, timeout_s) or []


def control(app_id: str, action: str, timeout_s: float = TIMEOUT_S) -> Session | None:
    """Play, pause, toggle, next or previous in the session with exactly this app id. Returns
    it as it is afterwards, or None if there's no such session or it didn't take."""
    if action not in ACTIONS or not available():
        return None

    async def act() -> Session | None:
        mgr = await _manager()
        for s in mgr.get_sessions():
            if str(s.source_app_user_model_id or "") != app_id:
                continue
            call = {
                "play": s.try_play_async, "pause": s.try_pause_async,
                "toggle": s.try_toggle_play_pause_async, "next": s.try_skip_next_async,
                "previous": s.try_skip_previous_async,
            }[action]  # fmt: skip
            if not await call():
                return None
            await asyncio.sleep(0.25)  # the player takes a moment to report its new state
            return await _describe(s)
        return None

    return _run(act, timeout_s)
