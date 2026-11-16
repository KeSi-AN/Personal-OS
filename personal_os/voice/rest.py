"""Donna takes a break: "take 10", "rest up for 20 minutes", "take a breather" (ADR-085).

While she rests, nothing she hears starts a turn: the wake word is off and she doesn't listen
for a follow-up after a reply. The hotkey or the island's Talk button wakes her at once (and
that press is a normal turn: she listens). Otherwise she's back by herself when the time is up.
Announcements (a reminder coming due) still happen: resting is about listening, not silence.

The rest is a small file in the runtime folder, so the island can show it ("Resting · 8 min")
and a restart doesn't forget it. Read by rules, no model: it must be instant and predictable.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from personal_os.core import runtime

REST_FILE = "voice_rest.json"
DEFAULT_MIN = 10  # "take a break" with no time
MAX_MIN = 240  # four hours at most: longer is turning voice off, which the settings do

_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20,
    "twenty five": 25, "twenty-five": 25, "thirty": 30, "forty": 40, "forty five": 45,
    "forty-five": 45, "sixty": 60, "ninety": 90, "a couple": 2, "a couple of": 2, "a few": 3,
}  # fmt: skip
_NUM = r"(\d{1,3}|" + "|".join(sorted(map(re.escape, _WORDS), key=len, reverse=True)) + ")"
_UNIT = r"(minutes?|mins?|hours?|hrs?|hr)"
_SPAN = rf"(?:{_NUM}(?:\s*-?\s*{_UNIT})?|half\s+an\s+hour|an\s+hour|a\s+half\s+hour)"
_LEAD = r"^\W*(?:(?:hey|ok(?:ay)?|so)\W+)?(?:donna\W+)?(?:(?:can|could)\s+you\s+)?(?:please\s+)?"
_TAIL = r"(?:\s+(?:please|now|donna))*\W*$"
# "take 10", "take ten minutes", "take five", "take a 10 minute break", "take a break"
_TAKE = re.compile(
    _LEAD + rf"take\s+(?:(?:a|an)\s+)?(?:(?P<span>{_SPAN})(?:\s+(?:break|rest|breather))?"
    r"|(?:break|rest|breather|nap|beat))" + rf"(?:\s+for\s+(?P<span2>{_SPAN}))?" + _TAIL,
    re.I,
)
# "rest up for 20 mins", "go rest", "snooze 15", "go on a break": a rest even with no time.
_VERB = re.compile(
    _LEAD + r"(?:go\s+)?(?:rest(?:\s+up)?|snooze|nap|go\s+on\s+(?:a\s+)?break|have\s+a\s+break|"
    r"have\s+a\s+rest)" + rf"(?:\s+(?:for\s+)?(?P<span>{_SPAN}))?" + _TAIL,
    re.I,
)
# "be quiet", "stop listening", "sleep" alone already mean "stop now" (voice's stop words):
# they're a rest only with a time ("be quiet for half an hour").
_TIMED = re.compile(
    _LEAD + r"(?:go\s+)?(?:sleep|chill|stop\s+listening|don'?t\s+listen|be\s+quiet|go\s+quiet|"
    r"switch\s+off|stand\s+by|standby|mute\s+yourself)"
    + rf"\s+(?:for\s+)?(?P<span>{_SPAN})"
    + _TAIL,
    re.I,
)


def _minutes(span: str | None) -> int:
    if not span:
        return DEFAULT_MIN
    s = " ".join(span.lower().split())
    if "half" in s and "hour" in s:
        return 30
    if s == "an hour":
        return 60
    m = re.match(_NUM, s)
    n = (int(m[1]) if m[1].isdigit() else _WORDS[m[1]]) if m else DEFAULT_MIN
    return n * 60 if re.search(r"\b(hours?|hrs?|hr)\b", s) else n


def parse(text: str) -> int | None:
    """Minutes of rest asked for, or None if this isn't a request for a rest."""
    t = " ".join(text.strip().split())
    m = _TAKE.match(t)
    if m:
        return max(1, min(MAX_MIN, _minutes(m["span"] or m["span2"])))
    m = _VERB.match(t) or _TIMED.match(t)
    if m:
        return max(1, min(MAX_MIN, _minutes(m["span"])))
    return None


def start(runtime_dir: Path, minutes: int, now: float | None = None) -> float:
    """Rest for `minutes`; returns when it ends (epoch seconds)."""
    until = (now if now is not None else time.time()) + minutes * 60
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime.write_json_atomic(runtime_dir / REST_FILE, {"until": until, "minutes": minutes})
    return until


def until(runtime_dir: Path, now: float | None = None) -> float | None:
    """When the rest ends, or None when she isn't resting (an ended rest is cleared)."""
    data = runtime.read_json(runtime_dir / REST_FILE)
    if not data:
        return None
    try:
        end = float(data.get("until", 0))
    except (TypeError, ValueError):
        end = 0.0
    if end <= (now if now is not None else time.time()):
        end_rest(runtime_dir)
        return None
    return end


def resting(runtime_dir: Path, now: float | None = None) -> bool:
    return until(runtime_dir, now) is not None


def end_rest(runtime_dir: Path) -> bool:
    """Wake her: True if she was resting."""
    path = runtime_dir / REST_FILE
    if not path.exists():
        return False
    path.unlink(missing_ok=True)
    return True


def left_min(runtime_dir: Path, now: float | None = None) -> int:
    """Whole minutes of rest left (rounded up); 0 when she isn't resting."""
    end = until(runtime_dir, now)
    if end is None:
        return 0
    return max(1, int((end - (now if now is not None else time.time()) + 59) // 60))


def reply(minutes: int) -> str:
    span = "an hour" if minutes == 60 else (f"{minutes} minutes" if minutes != 1 else "a minute")
    if minutes > 60 and minutes % 60 == 0:
        span = f"{minutes // 60} hours"
    return f"Okay, taking {span}. Press Talk or the hotkey when you need me."
