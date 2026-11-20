"""Work mode's "back to work?" nudge: when it was last said, and snoozing it.

The daemon says the nudge (daemon._nudge) and records it here; voice reads it to know that
"snooze", "5 more minutes" or "not now" right after one is about the nudge. A snooze holds
nudges for that long, and then one can come again if chill is still in front. Both live in the
runtime folder, so they survive a restart.

    "snooze"                      5 minutes
    "snooze 10", "snooze it for ten minutes"
    "give me 5 more minutes", "10 more minutes", "just five more"
    "not now", "later", "in a bit"
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from personal_os.core import runtime

FILE = "nudge.json"
ALERT_PREFIX = "nudge-"  # the island's alert for a nudge (interfaces/alerts.py ids)
DEFAULT_MIN = 5
MAX_MIN = 120
RECENT = timedelta(minutes=10)  # what's said this soon after a nudge can be about it
EVERY = timedelta(minutes=30)  # nudges at most this often, unless a snooze ran out

_WORDS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30,
          "a couple": 2, "a couple of": 2, "a few": 3, "few": 3}  # fmt: skip
_NUM = r"(\d{1,3}|" + "|".join(sorted(map(re.escape, _WORDS), key=len, reverse=True)) + ")"
_LEAD = r"^\W*(?:(?:ok(?:ay)?|hey|so|yeah|ugh|fine)\W+)*(?:donna\W+)?(?:please\s+)?"
_TAIL = r"(?:\s+(?:please|donna|ok(?:ay)?))*\W*$"
_SNOOZE = re.compile(
    _LEAD + rf"snooze(?:\s+(?:it|that|me|this|the nudge))?(?:\s+(?:for\s+)?{_NUM}"
    r"(?:\s*(?:more\s+)?(?:minutes?|mins?))?)?" + _TAIL,
    re.I,
)
_MORE = re.compile(
    _LEAD + rf"(?:(?:give|let)\s+me\s+|just\s+|i\s+need\s+)?(?:another\s+)?{_NUM}\s+more"
    r"(?:\s+(?:minutes?|mins?))?" + _TAIL,
    re.I,
)
_NOT_NOW = re.compile(_LEAD + r"(?:not\s+now|later|in\s+a\s+(?:bit|minute|sec)|not\s+yet)" + _TAIL,
                      re.I)  # fmt: skip
_MINS = r"(?:\s*(?:minutes?|mins?))?"
# "gimme 5", "give me ten minutes" (21 Nov: "gimme 5" became a to-do "get back in 5").
_GIVE = re.compile(_LEAD + rf"(?:gimme|give\s+me|let\s+me\s+have)\s+(?:a\s+)?{_NUM}{_MINS}" + _TAIL,
                   re.I)  # fmt: skip
# "I'll get back in 5", "I'll be back to work in ten minutes", "back in 5" (21 Nov: "i'll get
# back in 5" became a commitment Donna would hold you to).
_BACK_IN = re.compile(
    _LEAD + r"(?:i'?ll\s+|i\s+will\s+|i'?m\s+going\s+to\s+|gonna\s+)?(?:get\s+|be\s+|go\s+)?"
    rf"back(?:\s+(?:to\s+(?:it|work|work\s+mode)|on\s+it))?\s+in\s+(?:a\s+)?{_NUM}{_MINS}" + _TAIL,
    re.I,
)


def _n(word: str | None) -> int:
    if not word:
        return DEFAULT_MIN
    w = word.lower().strip()
    return int(w) if w.isdigit() else _WORDS.get(w, DEFAULT_MIN)


def parse(text: str) -> int | None:
    """Minutes to snooze the nudge for, or None if this isn't a snooze."""
    t = " ".join(text.split())
    m = _SNOOZE.match(t) or _MORE.match(t) or _GIVE.match(t) or _BACK_IN.match(t)
    if m:
        return max(1, min(MAX_MIN, _n(m[1])))
    if _NOT_NOW.match(t):
        return DEFAULT_MIN
    return None


def _state(rt: Path) -> dict[str, str]:
    return {k: str(v) for k, v in (runtime.read_json(rt / FILE) or {}).items()}


def _at(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def said(rt: Path, now: datetime) -> None:
    """A nudge was just said."""
    st = _state(rt)
    st["said_at"] = now.isoformat()
    st.pop("snoozed_until", None)
    runtime.write_json_atomic(rt / FILE, dict(st))


def recent(rt: Path, now: datetime) -> bool:
    """Was a nudge said in the last few minutes (so "snooze" is about it)?"""
    at = _at(_state(rt).get("said_at"))
    return at is not None and timedelta(0) <= now - at <= RECENT


def clear(rt: Path) -> int:
    """Take the nudge off the island. It's an alert, and alerts stay until dismissed, so
    after "snooze" it stayed up and had to be closed by hand (21 Nov)."""
    gone = 0
    for f in (rt / "alerts").glob(f"{ALERT_PREFIX}*.json"):
        f.unlink(missing_ok=True)
        gone += 1
    return gone


def showing(rt: Path) -> bool:
    return any((rt / "alerts").glob(f"{ALERT_PREFIX}*.json"))


def snooze(rt: Path, minutes: int, now: datetime) -> datetime:
    clear(rt)
    st = _state(rt)
    until = now + timedelta(minutes=minutes)
    st["snoozed_until"] = until.isoformat()
    runtime.write_json_atomic(rt / FILE, dict(st))
    return until


def due(rt: Path, now: datetime) -> bool:
    """May a nudge be said now? Not during a snooze; right when one runs out; otherwise not
    within EVERY of the last one."""
    st = _state(rt)
    until = _at(st.get("snoozed_until"))
    if until is not None:
        return now >= until
    last = _at(st.get("said_at"))
    return last is None or now - last > EVERY


def reply(minutes: int) -> str:
    return (f"Okay, {minutes} more minute{'s' * (minutes != 1)}. I'll nudge you again then if "
            "you're still on it.")  # fmt: skip
