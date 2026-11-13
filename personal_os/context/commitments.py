"""Commitments: what you said you'd do, and Donna holding you to it (docs/AGENT.md, phase 11).

Roamies' best idea, made local: when you say "I'll finish the deck tonight", the assistant
keeps it, with a due time, and brings it up when it's due or overdue. Here a commitment is a
to-do (project_items, kind 'todo') with an optional due time, so it shows wherever to-dos do
(projects, the dashboard, the brief). Reminders stay what they are: an alert at a time.

- `detect()` spots a sentence that commits you to something, by rules (no model): "I'll …",
  "I need to …", "I have to …", with an optional time ("tonight", "by Friday", "at 5pm").
  It's offered as a *proactive* call, so the policy asks first (ADR-049): a spoken yes, a
  confirmation, or an Approve card on the dashboard. Nothing is kept without your yes.
- `prompt_block()` puts your open commitments (overdue first) and next reminders in front of
  the agent on every turn, so "what did I say I'd do?" and "I finished the deck" just work.
  It's your own words, never captured content.
- `take_due()` returns commitments whose time has passed, once each, for the daemon to say.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from personal_os.core.clock import Clock, from_ms, to_ms

# "I'll / I will / I'm going to / I need to / I have to / I've got to / I must / I should /
# I plan to / I promise to", at the start, after an optional "ok", "so" or "right".
_LEAD = re.compile(
    r"^\W*(?:(?:ok(?:ay)?|so|right|yeah|alright|well)\W+)*"
    r"(?:i'?ll|i will|i'?m going to|i am going to|i'?m gonna|i need to|i have to|"
    r"i'?ve got to|i got to|i gotta|i must|i should|i plan to|i promise to)\s+"
    r"(?P<rest>.+?)\W*$",
    re.I,
)
# Not something to keep: about the conversation itself, or too vague to act on.
_NOT_A_TASK = re.compile(
    r"^(?:be\b|see\b|think\b|try\b|let you\b|ask you\b|tell you\b|get back to you\b|"
    r"check (?:it|that)\b|do (?:it|that|this)\b|go\b\W*$|say\b|answer\b|wait\b|stop\b|"
    r"know\b|remember\b|remind\b)",
    re.I,
)
# Without a time, these are something to do on the computer now ("I need to open Chrome"),
# for the agent, not something to keep.
_COMPUTER_NOW = re.compile(
    r"^(?:open|close|launch|start|play|pause|resume|mute|unmute|search|google|find|look up|"
    r"look at|check|read|listen|watch|turn|set|switch|go to|visit|show|lock|take a screenshot|"
    r"copy|move|rename|delete|run|commit|stash|install)\b",
    re.I,
)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_PART_HOUR = {"morning": 11, "afternoon": 15, "evening": 19, "night": 21, "tonight": 21}
_CLOCK = r"\d{1,2}(?:[:.]\d{2})?\s*(?:[ap]\.?\s*m\.?)?|noon|midday|midnight"
# Time phrases at the end of the sentence, longest first. Each has a "when" group.
_WHEN = re.compile(
    r"\s*,?\s*(?P<when>"
    r"(?:by |before )?(?:the )?end of (?:the )?(?:day|today)"
    r"|(?:by |on |before |this |next )?(?:" + "|".join(_WEEKDAYS) + r")"
    r"(?: (?:morning|afternoon|evening|night))?(?: (?:at|by) (?:" + _CLOCK + r"))?"
    # A clock time needs "at"/"by"/"before" or an am/pm or a colon: "buy 2" isn't 2 o'clock.
    r"|(?:(?:by|at|before) (?:" + _CLOCK + r")|\d{1,2}(?::\d{2})?\s*[ap]\.?\s*m\.?"
    r"|\d{1,2}:\d{2}|noon|midnight)(?: (?:today|tonight|tomorrow))?"
    r"|(?:by |before )?tomorrow(?: (?:morning|afternoon|evening|night))?"
    r"(?: (?:at|by) (?:" + _CLOCK + r"))?"
    r"|(?:by |before )?(?:tonight|today|this (?:morning|afternoon|evening))"
    r"(?: (?:at|by) (?:" + _CLOCK + r"))?"
    r"|in (?:\d{1,3}|an?|one|two|three|four|five|ten|twenty|thirty) (?:minutes?|mins?|hours?)"
    r"|later today"
    r")\s*$",
    re.I,
)
_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10,
            "twenty": 20, "thirty": 30}  # fmt: skip
_DAY_END_HOUR = 18  # "today", "by Friday": the end of that working day


@dataclass(frozen=True)
class Proposal:
    text: str  # what you'll do, in your words ("finish the deck")
    when: str  # the time as you said it ("tonight"), or ""

    def args(self) -> dict[str, str]:
        return {"text": self.text, "when": self.when}


def detect(text: str) -> Proposal | None:
    """A sentence that commits you to something, or None. A question is never one."""
    if "?" in text:
        return None
    m = _LEAD.match(text.strip())
    if not m:
        return None
    rest = m["rest"].strip()
    when = ""
    w = _WHEN.search(rest)
    if w and w.start() > 0:
        when = w["when"].strip()
        rest = rest[: w.start()].strip(" ,")
    rest = re.sub(r"\s*,?\s*(?:please|for sure|definitely)$", "", rest, flags=re.I).strip()
    if len(rest.split()) < 2 or _NOT_A_TASK.match(rest):
        return None
    if not when and _COMPUTER_NOW.match(rest):
        return None
    return Proposal(text=rest[:300], when=when[:40])


def _clock(word: str, day: datetime) -> datetime:
    """`word` ("5pm", "17:30", "noon") on `day` (a local datetime)."""
    from personal_os.actions.reminders import due_time

    w = word.strip().lower()
    w = {"noon": "12pm", "midday": "12pm", "midnight": "11:59pm"}.get(w, w)
    bare = re.fullmatch(r"(\d{1,2})(?::\d{2})?", w)
    if bare and 1 <= int(bare[1]) <= 7:
        w += "pm"  # "by 5": a time to have something done by is 5 pm, not 5 am
    start = day.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(seconds=1)
    return due_time(start, None, w)  # the next such time after the day's start


def due_from(when: str, now: datetime) -> datetime | None:
    """A due time for a phrase like "tonight", "by Friday", "at 5pm", "tomorrow morning",
    "in 2 hours"; None for no phrase. Raises ValueError for one it can't read."""
    w = " ".join(when.lower().replace(".", " ").split())
    w = re.sub(r"\b(?:by|before|on|this|at|the|of)\b ?", "", w).strip()
    if not w:
        return None
    local = now.astimezone()
    m = re.fullmatch(r"in (\S+) (minutes?|mins?|hours?)", when.lower().strip())
    if m:
        n = int(m[1]) if m[1].isdigit() else _NUMBERS.get(m[1], 0)
        if not n:
            raise ValueError(f"can't read {when!r}")
        return now + timedelta(minutes=n * (60 if m[2].startswith("hour") else 1))
    clock = re.search(rf"\b({_CLOCK})\b", w)
    day = local
    named = False  # a day was said ("Friday", "tomorrow"), even if it's today
    following = w.startswith("next ")
    if following:
        w = w[5:]
    for i, name in enumerate(_WEEKDAYS):
        if w.startswith(name):
            ahead = (i - local.weekday()) % 7  # "by Friday" said on a Friday: today
            if following and ahead == 0:
                ahead = 7
            day, named = local + timedelta(days=ahead), True
            break
    else:
        if w.startswith("tomorrow"):
            day, named = local + timedelta(days=1), True
    if clock:
        return _clock(clock[1], day)
    part = next((p for p in _PART_HOUR if p in w), None)
    if part is not None:
        due = day.replace(hour=_PART_HOUR[part], minute=0, second=0, microsecond=0)
    elif named or any(k in w for k in ("end day", "end today", "today", "later")):
        due = day.replace(hour=_DAY_END_HOUR, minute=0, second=0, microsecond=0)
    else:
        raise ValueError(f"can't read {when!r}")
    if due <= local:  # "tonight" said at 22:30: an hour from now rather than in the past
        due = local + timedelta(hours=1)
    return due


# --- reading them back -----------------------------------------------------------------------


@dataclass(frozen=True)
class Commitment:
    id: str
    text: str
    project: str  # its name, or "" for the inbox
    due: datetime | None
    kind: str  # "todo" or "reminder"

    def overdue(self, now: datetime) -> bool:
        return self.due is not None and self.due < now


def open_list(conn: sqlite3.Connection, now: datetime, limit: int = 8) -> list[Commitment]:
    """Open to-dos you made yourself (overdue first, then by due time, then the newest), then
    reminders that haven't gone off. Items Donna attached herself ('auto') aren't yours."""
    rows = conn.execute(
        "SELECT i.id, i.title, COALESCE(p.name, ''), i.due_at FROM project_items i "
        "LEFT JOIN projects p ON p.id = i.project_id "
        "WHERE i.kind = 'todo' AND i.done = 0 AND i.source != 'auto' "
        "ORDER BY i.due_at IS NULL, i.due_at, i.created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = [Commitment(r[0], r[1], r[2], from_ms(r[3]) if r[3] else None, "todo") for r in rows]
    from personal_os.actions import reminders

    for r in reminders.pending(conn)[: max(0, limit - len(out))]:
        out.append(Commitment(r.id, r.text, "", r.due, "reminder"))
    return out


def _when(due: datetime, now: datetime) -> str:
    local, today = due.astimezone(), now.astimezone().date()
    clock = local.strftime("%H:%M")
    if local.date() == today:
        return f"today {clock}"
    if local.date() == today + timedelta(days=1):
        return f"tomorrow {clock}"
    if local.date() == today - timedelta(days=1):
        return f"yesterday {clock}"
    return local.strftime("%a %d %b ") + clock


def prompt_block(conn: sqlite3.Connection, now: datetime) -> str:
    """The lines the agent sees before each request; "" when there's nothing open."""
    items = open_list(conn, now)
    if not items:
        return ""
    lines = []
    for c in items:
        where = f" ({c.project})" if c.project else ""
        if c.kind == "reminder" and c.due is not None:
            lines.append(f"- reminder {_when(c.due, now)}: {c.text}")
        elif c.due is not None:
            late = ", OVERDUE" if c.overdue(now) else ""
            lines.append(f"- {c.text}{where}: due {_when(c.due, now)}{late}")
        else:
            lines.append(f"- {c.text}{where}")
    return (
        "What they said they'd do (their own words; open to-dos, then reminders). Bring up "
        "an overdue one when it fits; when they say one is done, tick it off with todo_done:\n"
        + "\n".join(lines)
    )


# "What did I say I'd do?", "what's due?", "anything overdue?", "what's on my list?": answered
# from the list by rules. qwen3:4b sent the first to recall (60 s, "no data") and read an
# overdue item as on time in the second (measured 2026-11-13).
_ASKED = re.compile(
    r"\bwhat (?:did|have) i (?:say|said|told you|promised?|committed to)(?: (?:i'?d|i would|"
    r"i'?ll|i will|to))\b"
    r"|\bwhat(?:'s|s| is| are)? (?:due|overdue|on my (?:list|plate|to-?do(?: list)?)|"
    r"left to do|still (?:open|pending|to do)|my (?:commitments|to-?dos))\b"
    r"|\b(?:is |are )?(?:any|anything)(?: thing)? (?:overdue|due|pending|late)\b"
    r"|\bwhat do i (?:still )?(?:have|need|got) to do\b"
    r"|\bwhat am i supposed to (?:do|be doing)\b"
    r"|\b(?:list|show|read) (?:me )?my (?:commitments|to-?dos)\b",
    re.I,
)


# The calendar and mail have their own tools: "what's due on my calendar" is for them.
_ELSEWHERE = re.compile(r"\b(?:calendar|meetings?|events?|e-?mails?|mail|inbox|gmail)\b", re.I)


def asked_about(text: str) -> bool:
    return bool(_ASKED.search(text)) and not _ELSEWHERE.search(text)


def answer(conn: sqlite3.Connection, now: datetime) -> tuple[str, str]:
    """A spoken answer (overdue first, then what's next) and the full list for the screen."""
    items = open_list(conn, now, limit=20)
    if not items:
        return "Nothing's open. You haven't told me about anything you'll do.", ""

    def said(c: Commitment) -> str:
        return f"{c.text}, due {_when(c.due, now)}" if c.due is not None else c.text

    late = [c for c in items if c.kind == "todo" and c.overdue(now)]
    upcoming = [c for c in items if c not in late]
    parts = []
    if late:
        n = "One thing is" if len(late) == 1 else f"{len(late)} things are"
        parts.append(f"{n} overdue: {'; '.join(said(c) for c in late[:2])}.")
    else:
        parts.append("Nothing's overdue.")
    if upcoming:
        parts.append(f"{'Next' if late else 'Next up'}: {said(upcoming[0])}.")
    rest = len(items) - min(len(late), 2) - (1 if upcoming else 0)
    if rest > 0:
        parts.append(f"{rest} more on screen.")
    details = "\n".join(
        f"- {c.text}" + (f" ({c.project})" if c.project else "")
        + (f": due {_when(c.due, now)}" if c.due is not None else "")
        + (", OVERDUE" if c.kind == "todo" and c.overdue(now) else "")
        + (" [reminder]" if c.kind == "reminder" else "")
        for c in items
    )  # fmt: skip
    return " ".join(parts), details


def take_due(conn: sqlite3.Connection, clock: Clock) -> list[Commitment]:
    """Commitments whose due time has passed and that haven't been brought up yet; each is
    marked so it's said once, even across a restart."""
    now = clock.now()
    rows = conn.execute(
        "SELECT id, title, due_at FROM project_items WHERE kind = 'todo' AND done = 0 "
        "AND source != 'auto' AND due_at IS NOT NULL AND nudged_at IS NULL AND due_at <= ?",
        (to_ms(now),),
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE project_items SET nudged_at = ? WHERE id = ?", (to_ms(now), r[0]))
    return [Commitment(r[0], r[1], "", from_ms(r[2]), "todo") for r in rows]
