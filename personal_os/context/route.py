"""What a typed or spoken request is: a briefing, a fact to remember, an action, or a question.

Shared by the overlay and voice so both behave the same way.
"""

from __future__ import annotations

import re
from typing import Literal

from personal_os.context.memory import parse_remember

Route = Literal["stop", "briefing", "remember", "do", "ask"]

_STOP = re.compile(r"^\s*(stop|cancel|never ?mind|be quiet|shut up|quiet)\W*$", re.I)
_BRIEFING = re.compile(
    r"^\s*(good morning|morning|brief me|briefing|daily briefing|morning briefing|"
    r"end of (the )?day|evening summary|wrap up( the day)?|how was my day)\W*$",
    re.I,
)
_DO = re.compile(
    r"^\s*(please\s+)?(do|open|start|launch|play|pause|resume|skip|mute|unmute|turn|set|"
    r"remind|take|save|note|create|move|rename|commit|switch|stash|run|copy|minimi[sz]e|"
    r"maximi[sz]e|focus|dim|brighten|volume|next|previous)\b",
    re.I,
)
_WAKE = re.compile(r"^\s*(hey\s+)?donna\W*\s*", re.I)


def strip_wake(text: str) -> str:
    """ "Hey Donna, what did I do yesterday?" -> "what did I do yesterday?"."""
    return _WAKE.sub("", text, count=1).strip()


def route(text: str) -> Route:
    text = strip_wake(text)
    if _STOP.match(text):
        return "stop"
    if _BRIEFING.match(text):
        return "briefing"
    if parse_remember(text):
        return "remember"
    if _DO.match(text):
        return "do"
    return "ask"


def evening(text: str) -> bool:
    return bool(re.search(r"end of|evening|wrap up|how was my day", text, re.I))
