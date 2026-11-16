"""The island's decisions, without a window: what it should be (hidden, a pill, open on a page)
and how big, from what voice is doing, the mouse, a page asked for, and full-screen apps.
interfaces/island.py animates the real window to whatever this says. ADR-082.

    pill  ──hover (a moment)──▶  open on the last page        ──mouse leaves──▶  pill
    pill  ──"Donna"──────────▶  open on Donna (listening…)    ──turn over + read──▶  pill
    pill  ──"show my projects"▶  open on Projects (a while)    ──time / mouse leaves──▶  pill
    any   ──a full-screen app─▶  hidden (unless you called her or a reminder is due)

A pill over an app (a browser's tabs, a title bar) is a *ghost* (ADR-083): see-through and
click-through, so what's under it still works. It fades further while the pointer is on it,
so you can see what you're clicking, and opens only when you hold the pointer against the top
edge of the screen there for a moment, which clicking a tab never does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PAGES = ("donna", "today", "notes", "music", "projects")
PAGE_TITLES = {
    "donna": "Donna", "today": "Today", "notes": "Notes", "music": "Music",
    "projects": "Projects",
}  # fmt: skip

# Sizes in logical pixels (scaled by the monitor's DPI when placed).
PILL = (196, 34)
PILL_WIDE = (300, 34)  # with something to show: music playing, a reminder due
OPEN_W = 560
CARD_H = 290  # Today / Notes / Music / Projects (room for the box to add one)
VOICE_MIN_H, VOICE_MAX_H = 150, 560  # Donna: as tall as what's on it
PILL_RADIUS, OPEN_RADIUS = 17, 30
TUCKED_H = 8  # hidden: shrunk into the top edge this far before it disappears

HOVER_OPEN_S = 0.18  # resting on the pill this long opens it (passing over doesn't)
LEAVE_CLOSE_S = 0.45  # after the mouse leaves an island it opened on hover
REQUEST_S = 14.0  # "show my projects" keeps it open this long (longer while the mouse is on it)

# The ghost pill (over an app): opacity out of 255, and how it opens.
GHOST_ALPHA = 105  # see-through enough to read the tab under it
NEAR_ALPHA = 40  # the pointer is on it: nearly gone, so you see what you're clicking
EDGE_OPEN_S = 0.45  # held against the top edge on it this long opens it
EDGE_PX = 3  # "against the top edge": within this many logical pixels of it
# Once you've used it (a click, a key, a scroll), it closes this long after you last did, or
# after the pointer left, whichever is later, unless the pointer is on it (ADR-085).
USED_CLOSE_S = 5.0


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
    # A ghost pill (over an app): hovered_s counts only time held against the top edge,
    # and opening takes EDGE_OPEN_S of it.
    ghost: bool = False
    # Since you last used the open island (or the pointer left, if later); None if unused.
    used_s: float | None = None
    voice_busy: bool = False  # listening, thinking or speaking right now


def done_with(i: Inputs) -> bool:
    """You used it and moved on: close it now, whatever opened it (a turn being read, a page
    you asked for, a box you typed in). Never while the pointer is on it, it's pinned, or
    she's listening, thinking or speaking."""
    return (
        i.used_s is not None and i.used_s >= USED_CLOSE_S and i.hovered_s is None
        and not i.pinned and not i.voice_busy
    )  # fmt: skip


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
    if i.hovered_s is not None and i.hovered_s >= (EDGE_OPEN_S if i.ghost else HOVER_OPEN_S):
        return Target("open", page, "held at the top edge" if i.ghost else "hovered"), "hover"
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


def ghosted(target: Target, app_under: bool) -> bool:
    """A pill with an app under it is a ghost: see-through, and clicks go to the app."""
    return target.mode == "pill" and app_under


def alpha(ghost: bool, pointer_on: bool) -> int:
    """The window's opacity (0-255): solid, a ghost, or a ghost with the pointer on it."""
    if not ghost:
        return 255
    return NEAR_ALPHA if pointer_on else GHOST_ALPHA


def overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    """Do two (left, top, right, bottom) rectangles share any area?"""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


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
    "notes": "notes", "my notes": "notes", "note": "notes", "the notes": "notes",
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
    "notes": "Here are your notes.",
    "donna": "Here.",
    "hide": "Okay.",
}
