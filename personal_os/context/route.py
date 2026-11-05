"""What a typed or spoken request is: a briefing, a fact to remember, an action, or a question.

Shared by the overlay and voice so both behave the same way.
"""

from __future__ import annotations

import re
from typing import Literal

from personal_os.context.memory import parse_remember

Route = Literal["stop", "briefing", "remember", "do", "ask", "chat"]

_STOP = re.compile(r"^\s*(stop|cancel|never ?mind|be quiet|shut up|quiet)\W*$", re.I)
_BRIEFING = re.compile(
    r"^\s*(good morning|morning|brief me|briefing|daily briefing|morning briefing|"
    r"end of (the )?day|evening summary|wrap up( the day)?|how was my day)\W*$",
    re.I,
)
# Requests start with a verb. Not "do": "do I have ...?" is a question.
_DO = re.compile(
    r"^\s*(please\s+)?(open|start|launch|play|pause|resume|skip|mute|unmute|turn|set|"
    r"remind|take|save|note|create|move|rename|commit|switch|stash|run|copy|minimi[sz]e|"
    r"maximi[sz]e|focus|dim|brighten|volume|next|previous|edit|change|update|modify|cancel|"
    r"delete|remove|clear|add|schedule|reschedule|postpone|delay|snooze|push|close|lock|"
    r"raise|lower|increase|decrease|make|stop|unpause|go to)\b",
    re.I,
)
# Questions: asked, never acted on. Checked before the verbs above ("is there ...", "do I ...").
_QUESTION = re.compile(
    r"^\s*(what|what's|whats|when|when's|where|where's|who|who's|whom|whose|why|how|how's|"
    r"which|is|isn't|are|aren't|was|wasn't|were|do|does|did|don't|doesn't|didn't|have|has|"
    r"had|haven't|hasn't|am|any|should|shall i|will i|would i|could i|can i)\b",
    re.I,
)
# Words that carry nothing before a request: "Actually, edit that", "okay so open brave".
_FILLER = re.compile(
    r"^\s*(?:(?:actually|okay|ok|so|um+|uh+|er+|erm|well|and|also|then|alright|all right|"
    r"right|oh|hmm+|now|wait|sorry|listen|yeah|yes|no|hey)\b[\s,.!-]*)+",
    re.I,
)
_WAKE = re.compile(r"^\s*(hey\s+)?donna\W*\s*", re.I)
# "Can you open brave", "could you please take a screenshot", "I want you to mute": the
# request is what follows.
_POLITE = re.compile(
    r"^\s*(?:(?:can|could|would|will) you(?: please)?|please|i want you to|i'd like you to|"
    r"i would like you to|go ahead and)\s+",
    re.I,
)
_CHAT = re.compile(
    r"^\s*(hi|hello|hey|hey there|thanks|thank you|thank you so much|thanks a lot|ok|okay|"
    r"cool|great|nice|bye|goodbye|good night|you there|are you there)\W*$",
    re.I,
)
# Words a request can't end on: "can you open the-" was cut off.
_DANGLING = frozenset(
    {
        *("the", "a", "an", "to", "and", "or", "of", "in", "on", "at", "for", "with"),
        *("my", "your", "this", "that", "what", "which"),
        *("open", "start", "play", "set", "turn", "take", "show", "tell"),
    }
)


def strip_wake(text: str) -> str:
    """ "Hey Donna, what did I do yesterday?" -> "what did I do yesterday?"."""
    return _WAKE.sub("", text, count=1).strip()


def normalize(text: str) -> str:
    """Without the wake word and leading filler: "Hey Donna, actually, edit that" -> "edit
    that". A bare filler ("okay", "no") is left as it is: that's the whole message."""
    t = strip_wake(text)
    stripped = _FILLER.sub("", t, count=1).strip()
    return stripped if stripped else t


def strip_polite(text: str) -> str:
    """ "Can you open brave?" -> "open brave?"."""
    return _POLITE.sub("", text, count=1).strip()


def route(text: str) -> Route:
    whole = strip_wake(text)
    if _STOP.match(whole):
        return "stop"
    if _CHAT.match(whole):
        return "chat"
    text = normalize(text)
    if _BRIEFING.match(text):
        return "briefing"
    if parse_remember(text):
        return "remember"
    request = strip_polite(text)  # "can you open ..." is a request; "can I ...?" isn't
    if _QUESTION.match(request) and not _POLITE.match(text):
        return "ask"
    if _DO.match(request):
        return "do"
    return "ask"


def incomplete(text: str) -> bool:
    """Heard only part of a request: "Can you open?", "Can you open the-", "Can you?"."""
    text = strip_wake(text)
    if not text or _CHAT.match(text) or _STOP.match(text):
        return False
    if text.rstrip().endswith(("-", "—", "…")):
        return True
    words = re.findall(r"[a-z0-9']+", strip_polite(text).lower())
    if not words or re.fullmatch(r"\s*(can|could|would|will) you\W*", text, re.I):
        return True
    return len(words) <= 2 and words[-1] in _DANGLING


def chat_reply(text: str) -> str:
    t = strip_wake(text).lower()
    if re.search(r"thank", t):
        return "You're welcome."
    if re.search(r"\b(bye|goodbye|good night)\b", t):
        return "Bye."
    if re.search(r"\b(ok|okay|cool|great|nice)\b", t):
        return "Okay."
    return "Hi. Ask me what you did, or tell me to open something."


def evening(text: str) -> bool:
    return bool(re.search(r"end of|evening|wrap up|how was my day", text, re.I))
