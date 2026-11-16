"""The island's decisions, without a window: what it should be (hidden, a pill, open on a page)
and how big, from what voice is doing, the mouse, a page asked for, and full-screen apps.
interfaces/island.py animates the real window to whatever this says. ADR-082.

    pill  ──hover (a moment)──▶  open on the last page        ──mouse leaves──▶  pill
    pill  ──"Donna"──────────▶  open on Donna (listening…)    ──turn over + read──▶  pill
    pill  ──"show my projects"▶  open on Projects (a while)    ──time / mouse leaves──▶  pill
    any   ──a full-screen app─▶  hidden (unless you called her or a reminder is due)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PAGES = ("donna", "today", "music", "projects")
PAGE_TITLES = {"donna": "Donna", "today": "Today", "music": "Music", "projects": "Projects"}

# Sizes in logical pixels (scaled by the monitor's DPI when placed).
PILL = (196, 34)
PILL_WIDE = (300, 34)  # with something to show: music playing, a reminder due
OPEN_W = 560
CARD_H = 250  # Today / Music / Projects
VOICE_MIN_H, VOICE_MAX_H = 150, 560  # Donna: as tall as what's on it
PILL_RADIUS, OPEN_RADIUS = 17, 30
TUCKED_H = 8  # hidden: shrunk into the top edge this far before it disappears

HOVER_OPEN_S = 0.18  # resting on the pill this long opens it (passing over doesn't)
LEAVE_CLOSE_S = 0.45  # after the mouse leaves an island it opened on hover
REQUEST_S = 14.0  # "show my projects" keeps it open this long (longer while the mouse is on it)


@dataclass(frozen=True)
class Inputs:
    turn_active: bool  # voice is listening, thinking or speaking (or the turn is still up)
    alerts: int  # reminders due and not dismissed
    hovered_s: float | None  # how long the mouse has been on the island (None: it isn't)
    left_s: float | None  # how long since the mouse left (None: it hasn't, or never came)
    request: str | None  # a page asked for ("show my projects"), while it's fresh
    request_age_s: float  # how long ago it was asked for
    pinned: bool
    typing: bool  # the prompt box has focus or text in it
    fullscreen: bool  # a full-screen app or game on the island's screen
    music: bool  # something is playing in another app
    voice_running: bool
    idle_pill: bool = False  # show the pill while idle (else hidden until she's active)


@dataclass(frozen=True)
class Target:
    mode: str  # "hidden" | "pill" | "open"
    page: str  # what the open island shows
    why: str  # for logs and tests


def decide(i: Inputs, page: str, opened_by: str | None) -> tuple[Target, str | None]:
    """Where the island should be now, and what opened it ("hover", "turn", "request",
    "alert", or None when it's a pill). `page` is the page shown last; `opened_by` is what
    opened it before this step (an island opened on hover closes when the mouse leaves; one
    you called stays while the turn needs it)."""
    held = i.hovered_s is not None or i.typing or i.pinned
    if i.request and (i.request_age_s < REQUEST_S or held):
        return Target("open", i.request, "asked for a page"), "request"
    if i.turn_active:
        # A new turn shows Donna; while it runs you can swipe to another page and stay there.
        keep = page if opened_by == "turn" else "donna"
        return Target("open", keep, "voice turn"), "turn"
    if i.alerts:
        return Target("open", "donna", "a reminder is due"), "alert"
    if opened_by and held:
        return Target("open", page, "held open"), opened_by
    if i.hovered_s is not None and i.hovered_s >= HOVER_OPEN_S:
        return Target("open", page, "hovered"), "hover"
    if opened_by == "hover" and i.left_s is not None and i.left_s < LEAVE_CLOSE_S:
        return Target("open", page, "mouse just left"), "hover"
    if i.fullscreen:
        return Target("hidden", page, "full-screen app"), None
    if not i.idle_pill:
        return Target("hidden", page, "idle"), None
    return Target("pill", page, "idle"), None


def size(target: Target, i: Inputs, voice_h: int) -> tuple[int, int]:
    """The island's size for a target, in logical pixels. `voice_h`: the Donna page's content
    height, as the page measured it."""
    if target.mode == "open":
        if target.page == "donna":
            return OPEN_W, max(VOICE_MIN_H, min(VOICE_MAX_H, voice_h))
        return OPEN_W, CARD_H
    if i.music or i.alerts:
        return PILL_WIDE
    return PILL


def radius(target: Target, h: int) -> int:
    return OPEN_RADIUS if target.mode == "open" else min(PILL_RADIUS, h // 2)


def place(
    monitor: tuple[int, int, int, int], scale: float, w: int, h: int
) -> tuple[int, int, int, int]:
    """Physical-pixel bounds (x, y, w, h) at the top centre of a monitor (left, top, right,
    bottom in physical pixels), flush with its top edge, for a logical size."""
    left, top, right, _bottom = monitor
    pw, ph = round(w * scale), round(h * scale)
    return left + (right - left - pw) // 2, top, pw, ph


# --- voice: "show my projects" ---------------------------------------------------------------

_SHOW = re.compile(
    r"^(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?(?:show|open|bring\s+up|go\s+to|switch\s+to)"
    r"\s+(?:me\s+)?(?:the\s+|my\s+)?(.+?)(?:\s+(?:page|card|in\s+the\s+island))?(?:\s+please)?$",
    re.I,
)
_WHICH = {
    "today": "today", "my day": "today", "day": "today", "today's": "today",
    "the day": "today", "agenda": "today", "to-dos": "today", "todos": "today",
    "to dos": "today", "reminders": "today",
    "music": "music", "the music": "music", "now playing": "music", "player": "music",
    "what's playing": "music", "song": "music",
    "projects": "projects", "project": "projects", "my projects": "projects",
    "donna": "donna", "conversation": "donna", "chat": "donna",
}  # fmt: skip
_HIDE = re.compile(r"^(?:hide|close|dismiss)\s+(?:the\s+)?(?:island|dashboard|panel)$", re.I)


def page_request(text: str) -> str | None:
    """The island page a spoken request asks for ("show my projects" -> "projects"),
    "hide" for "hide the island", or None."""
    t = re.sub(r"[.!?,]+$", "", text.strip())
    t = re.sub(r"^(?:hey\s+|ok(?:ay)?\s+)?donna\W*\s*", "", t, flags=re.I).strip()
    if _HIDE.match(t):
        return "hide"
    m = _SHOW.match(t)
    if not m:
        return None
    return _WHICH.get(m[1].lower().strip())


PAGE_REPLIES = {
    "today": "Here's today.",
    "music": "Here's what's playing.",
    "projects": "Here are your projects.",
    "donna": "Here.",
    "hide": "Okay.",
}
