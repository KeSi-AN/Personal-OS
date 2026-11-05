"""Reminder requests read by rules: set, change, cancel. No model.

The planner (a 4B model) used to fill a reminder in itself, and when the message was missing
("remind me in a minute" — the rest cut off by a pause) it used your sentence as the message.
Reminders follow a few fixed shapes, so they're parsed here instead: fast, testable, and a
missing message or time is *known* to be missing, so Donna asks for it rather than guessing.

    "remind me in 10 minutes to stretch"          set     text=stretch, minutes=10
    "remind me to call mum at 5 p.m."             set     text=call mum, at=5pm
    "set a reminder for two minutes saying hi"    set     text=hi, minutes=2
    "remind me in a minute"                       set     missing: what
    "edit that reminder to say go for a stretch"  update  text=go for a stretch
    "move the stretch reminder to 5 pm"           update  which=stretch, at=5pm
    "push it back 10 minutes" (after a reminder)  update  delay=10
    "cancel that reminder"                        cancel
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from personal_os.context.route import normalize, strip_polite

_NUM = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "forty five": 45, "fifty": 50,
    "sixty": 60, "ninety": 90, "a couple of": 2, "a couple": 2, "couple of": 2, "few": 3,
    "a few": 3,
}  # fmt: skip
_NUM_RE = r"(\d{1,4}|" + "|".join(sorted((re.escape(k) for k in _NUM), key=len, reverse=True)) + ")"
_UNIT = r"(minutes?|mins?|hours?|hrs?|seconds?|secs?)"
_HALF_HOUR = re.compile(r"\b(?:in|for|after)\s+(?:half an hour|a half hour|30 min\w*)\b", re.I)
_IN = re.compile(rf"\b(?:in|for|after|within)\s+(?:another\s+)?{_NUM_RE}\s+{_UNIT}\b", re.I)
_BY = re.compile(rf"\b(?:by|back|another|for another)\s+{_NUM_RE}\s+{_UNIT}\b", re.I)
_CLOCK = r"(\d{1,2}(?:[:.]\d{2})?\s*(?:[ap]\.?\s*m\.?)?|noon|midday|midnight)"
_AT = re.compile(rf"\b(?:at|for|to|till|until)\s+{_CLOCK}(?=$|[\s,.!?])", re.I)

_BARE_CLOCK = re.compile(rf"^{_CLOCK}$", re.I)
_BARE_SPAN = re.compile(rf"^{_NUM_RE}\s+{_UNIT}(?:\s+from now)?$", re.I)

_SET = re.compile(
    r"^(?:remind me|set (?:up )?(?:a |an |me a )?reminders?|create (?:a )?reminders?|"
    r"add (?:a )?reminders?|make (?:a )?reminders?|reminder)\b(.*)$",
    re.I,
)
_TARGET = r"(?:that|the|my|this|last|latest|previous|it|its|those|these)"
_UPDATE = re.compile(
    rf"^(?:edit|change|update|modify|rename|reword|move|push|postpone|delay|reschedule|snooze|"
    rf"shift|make)\s+(?:{_TARGET}\s+)?(?:{_TARGET}\s+)?(?:(.*?)\s*\breminders?\b|it|that)(.*)$",
    re.I,
)
_CANCEL = re.compile(
    rf"^(?:cancel|delete|remove|clear|drop|forget|turn off|stop|get rid of)\s+"
    rf"(?:{_TARGET}\s+)?(?:{_TARGET}\s+)?(all\s+)?(?:(?:of\s+)?(?:my\s+)?(.*?)\s*\breminders?\b|it|that)\s*$",
    re.I,
)
_SAY = re.compile(r"\b(?:to say|saying|to read|that says|to|so it says|with)\s*[,:]?\s*(.+)$", re.I)
_LEAD = re.compile(
    r"^(?:[,.:;]\s*)?(?:me\s+)?(?:to|that|about|of|saying|to say|for|so)\b[\s,:]*", re.I
)
_TAIL = re.compile(r"[\s,]*(?:please|thanks|thank you|instead|ok(?:ay)?)?[\s,.!?]*$", re.I)
_EMPTY = re.compile(r"^(?:something|it|this|that|me|now|later|soon|again)?$", re.I)


@dataclass
class ReminderRequest:
    op: Literal["set", "update", "cancel"]
    text: str = ""
    minutes: int | None = None
    at: str | None = None
    delay: int | None = None  # update: push back by this many minutes
    which: str = ""  # update/cancel: words naming the reminder ("stretch"); "" = the latest
    all: bool = False  # cancel: every pending reminder
    words: list[str] = field(default_factory=list)  # what was understood, for tests

    @property
    def missing(self) -> Literal["what", "when"] | None:
        """For a new reminder: the part that wasn't said (asked for, never guessed)."""
        if self.op != "set":
            return None
        if not self.text:
            return "what"
        if self.minutes is None and not self.at:
            return "when"
        return None

    def args(self) -> dict[str, object]:
        """The tool call's arguments."""
        out: dict[str, object] = {}
        if self.op == "set" or self.text:
            out["text"] = self.text
        if self.minutes is not None:
            out["minutes"] = self.minutes
        if self.at:
            out["at"] = self.at
        if self.delay is not None:
            out["delay_minutes"] = self.delay
        if self.which:
            out["which"] = self.which
        if self.all:
            out["all"] = True
        return out

    @property
    def tool(self) -> str:
        return {"set": "set_reminder", "update": "update_reminder", "cancel": "cancel_reminder"}[
            self.op
        ]


def _number(word: str) -> int:
    w = word.lower().strip()
    return int(w) if w.isdigit() else _NUM.get(w, 1)


def _minutes(n: int, unit: str) -> int:
    u = unit.lower()
    if u.startswith(("h",)):
        return n * 60
    if u.startswith("s"):
        return max(1, round(n / 60))
    return n


def _clock(word: str) -> str:
    w = word.lower().strip()
    return {"noon": "12:00", "midday": "12:00", "midnight": "0:00"}.get(w, w)


def _take_time(rest: str) -> tuple[str, int | None, str | None]:
    """Remove the first time phrase from `rest`: (what's left, minutes, at)."""
    m = _HALF_HOUR.search(rest)
    if m:
        return rest[: m.start()] + " " + rest[m.end() :], 30, None
    m = _IN.search(rest)
    if m:
        return rest[: m.start()] + " " + rest[m.end() :], _minutes(_number(m[1]), m[2]), None
    m = _AT.search(rest)
    if m:
        return rest[: m.start()] + " " + rest[m.end() :], None, _clock(m[1])
    return rest, None, None


def _message(rest: str) -> str:
    t = re.sub(r"\s+", " ", rest).strip(" ,.!?:;")
    for _ in range(2):
        t = _LEAD.sub("", t).strip(" ,.!?:;")
    t = _TAIL.sub("", t).strip(" ,.!?:;")
    return "" if _EMPTY.match(t) else t


def parse(text: str, *, allow_pronoun: bool = False) -> ReminderRequest | None:
    """A reminder request, or None if this isn't one. `allow_pronoun`: "move it to 5 pm" may
    mean the reminder (only right after one was talked about)."""
    t = strip_polite(normalize(text)).strip().rstrip(".!?")
    if not t:
        return None
    m = _SET.match(t)
    if m:
        rest, minutes, at = _take_time(m[1])
        return ReminderRequest("set", _message(rest), minutes, at)
    m = _CANCEL.match(t)
    if m and (m[0].lower().find("reminder") >= 0 or allow_pronoun):
        which = _message(m[2] or "")
        return ReminderRequest("cancel", which=which, all=bool(m[1]) or which.lower() == "all")
    m = _UPDATE.match(t)
    if m and ("reminder" in m[0].lower() or allow_pronoun):
        which = _message(m[1] or "")
        rest = m[2] or ""
        delay = None
        d = _BY.search(rest)
        if d and re.match(r"^(?:push|postpone|delay|snooze|move)", t, re.I):
            delay = _minutes(_number(d[1]), d[2])
            rest = rest[: d.start()] + " " + rest[d.end() :]
        say = _SAY.search(rest)
        new_text = ""
        when_part = rest
        if say and not _AT.match(say[0]) and not _IN.match(say[0]):
            candidate = _message(say[1])
            # "to 5 pm" is a time, not a message.
            if not _AT.fullmatch(f"at {candidate}") and candidate:
                new_text = candidate
                when_part = rest[: say.start()]
        _left, minutes, at = _take_time(when_part)
        if minutes is None and not at:  # "make it 5 pm", "make it 10 minutes"
            bare = _message(when_part)
            c = _BARE_CLOCK.match(bare)
            s = _BARE_SPAN.match(bare)
            if c:
                at = _clock(c[1])
            elif s:
                minutes = _minutes(_number(s[1]), s[2])
        if not new_text and minutes is None and not at and delay is None:
            return None  # "change that reminder" with nothing to change: not a command we know
        return ReminderRequest("update", new_text, minutes, at, delay, which)
    return None


def needs_more(text: str) -> bool:
    """A reminder whose message hasn't been said yet ("remind me in a minute"): the speaker
    probably paused, so keep listening a moment before acting."""
    r = parse(text)
    return r is not None and r.missing == "what"


_NO_ANSWER = re.compile(
    r"^(?:no+|nope|nothing|never ?mind|cancel|forget it|don'?t|skip it|leave it)\b", re.I
)


def message_from(answer: str) -> str:
    """Your answer to "What should I remind you about?": "to stretch" -> "stretch". Empty for
    silence or a no."""
    a = normalize(answer).strip()
    if not a or _NO_ANSWER.match(a):
        return ""
    return _message(a)


def time_from(answer: str) -> tuple[int | None, str | None]:
    """Your answer to "When should I remind you?": "in ten minutes", "at 5 p.m.", "5 pm",
    "ten minutes". (None, None) if no time was said."""
    a = normalize(answer).strip().rstrip(".!?")
    _rest, minutes, at = _take_time(a)
    if minutes is not None or at:
        return minutes, at
    bare = _message(a)
    c, s = _BARE_CLOCK.match(bare), _BARE_SPAN.match(bare)
    if c:
        return None, _clock(c[1])
    if s:
        return _minutes(_number(s[1]), s[2]), None
    return None, None
