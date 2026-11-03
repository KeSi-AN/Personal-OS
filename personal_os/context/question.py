"""Question parsing without the model: project, time range and intent (DESIGN.md §6.2).

Deterministic on purpose: routing decides what evidence is collected, so it must be predictable
and testable. The model only ever writes the answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from personal_os.context.retrieval import query_terms
from personal_os.projects.registry import NONE_PROJECT, Project

Intent = Literal["leave_off", "changed_since", "weekly", "activity", "research", "bug", "search"]

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = [
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
]
TIME_WORDS = frozenset(
    {
        "today",
        "yesterday",
        "tonight",
        "morning",
        "afternoon",
        "evening",
        "week",
        "weekly",
        "month",
        "hours",
        "hour",
        "days",
        "am",
        "pm",
        "past",
        "previous",
        "recent",
        "recently",
        "progress",
        "summary",
        "recap",
        "changed",
        "change",
        "changes",
        "new",
        "happened",
        "since",
        "bug",
        "bugs",
        "error",
        "errors",
        "issue",
        "issues",
        "relate",
        "related",
        "relates",
        "commands",
        "command",
        "files",
        "file",
        "pages",
        "page",
        "project",
        "projects",
        "far",
        *WEEKDAYS,
        *MONTHS,
    }
)

_INTENTS: list[tuple[Intent, re.Pattern[str]]] = [
    (
        "leave_off",
        re.compile(
            r"\b(left off|leave off|where was i|where am i|pick (it )?(back )?up|resume|"
            r"last (thing|time) i (did|worked|was))\b"
        ),
    ),
    (
        "changed_since",
        re.compile(
            r"\b(changed|new|happened|different) since\b|\bsince i last\b|\bwhat'?s changed\b"
        ),
    ),
    (
        "weekly",
        re.compile(
            r"\bweekly\b|\b(week'?s?|weeks?) (progress|summary|recap)\b|"
            r"\bprogress (this|last) week\b|\bprogress\b"
        ),
    ),
    (
        "bug",
        re.compile(
            r"\b(bugs?|errors?|exceptions?|traceback|fail(ed|ing|ures?|s)?|broke(n)?|"
            r"crash(ed|es|ing)?|issues?|debug(ging)?)\b"
        ),
    ),
    (
        "research",
        re.compile(
            r"\b(research(ing|ed)?|read(ing)? (about|up)|look(ing|ed)? (in)?to|learn(ing|ed|t)?|"
            r"stud(y|ying|ied)|reading)\b"
        ),
    ),
    (
        "activity",
        re.compile(
            r"\bwhat (did|have) i (do|done|work|worked|been)|\bwhat was i (doing|working)|"
            r"\bhow (much|long)\b|\bsummar(y|ise|ize)\b|\bwhat i did\b|\bwhat i worked\b"
        ),
    ),
]

_TIME = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"
_BETWEEN = re.compile(rf"\bbetween\s+{_TIME}\s+(?:and|to|-)\s+{_TIME}")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MONTH_DATE = re.compile(r"\b(" + "|".join(MONTHS) + r")[a-z]*\.?\s+(\d{1,2})\b")
_LAST_N = re.compile(r"\b(?:last|past)\s+(\d{1,3})\s+(hours?|days?|weeks?)\b")
_MENTION = re.compile(
    r"\b(?:on|with|in|about|for|to)\s+(?:the\s+|my\s+)?"
    r"([a-z0-9][\w.-]*(?:\s+[a-z0-9][\w.-]*){0,2}?)\s+(?:project|repo)\b"
)


@dataclass
class ParsedQuestion:
    text: str
    intent: Intent
    project: Project | None = None
    project_mention: str | None = None  # "... on X project" that matched nothing
    start: datetime | None = None
    end: datetime | None = None
    range_label: str = ""
    terms: list[str] = field(default_factory=list)


def _day(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _clock(h: str, m: str | None, ampm: str | None, other_ampm: str | None) -> tuple[int, int]:
    hour, minute = int(h), int(m or 0)
    ampm = ampm or other_ampm
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    return min(hour, 23), min(minute, 59)


def parse_time_range(
    q: str, now: datetime
) -> tuple[datetime | None, datetime | None, str, set[str]]:
    """(start, end, label, words used). `q` is lower-case; `now` is aware local time."""
    today = _day(now)
    used: set[str] = set()
    anchor = today
    if "yesterday" in q:
        anchor = today - timedelta(days=1)
    for i, name in enumerate(WEEKDAYS):
        if re.search(rf"\b{name}\b", q):
            back = (today.weekday() - i) % 7
            anchor = today - timedelta(days=back)
            used.add(name)
    m = _ISO_DATE.search(q)
    if m:
        anchor = _day(now.replace(year=int(m[1]), month=int(m[2]), day=int(m[3])))
    m2 = _MONTH_DATE.search(q)
    if m2 and not m:
        month = MONTHS.index(m2[1][:3]) + 1
        try:
            anchor = _day(now.replace(month=month, day=int(m2[2])))
        except ValueError:
            pass
        else:
            if anchor > now:
                anchor = anchor.replace(year=anchor.year - 1)
            used.add(m2[1])

    between = _BETWEEN.search(q)
    if between:
        h1, m1, ap1, h2, m2_, ap2 = between.groups()
        a = anchor.replace(hour=_clock(h1, m1, ap1, ap2)[0], minute=_clock(h1, m1, ap1, ap2)[1])
        b = anchor.replace(hour=_clock(h2, m2_, ap2, None)[0], minute=_clock(h2, m2_, ap2, None)[1])
        if b <= a:
            b += timedelta(hours=12) if b + timedelta(hours=12) > a else timedelta(days=1)
        return a, b, f"{a:%a %d %b %H:%M}-{b:%H:%M}", used

    if "since" in q and (
        re.search(
            r"\bsince (yesterday|today|monday|tuesday|wednesday|thursday|friday|"
            r"saturday|sunday|\d{4}-\d{2}-\d{2})",
            q,
        )
        or m2
    ):
        return anchor, now, f"since {anchor:%a %d %b}", used

    n = _LAST_N.search(q)
    if n:
        count, unit = int(n[1]), n[2].rstrip("s")
        delta = {
            "hour": timedelta(hours=count),
            "day": timedelta(days=count),
            "week": timedelta(weeks=count),
        }[unit]
        return now - delta, now, f"the last {count} {unit}{'s' * (count != 1)}", used
    if re.search(r"\bthis week\b", q):
        start = today - timedelta(days=today.weekday())
        return start, now, "this week", used
    if re.search(r"\b(last|past|previous) week\b", q):
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=7), "last week", used
    if re.search(r"\b(this|last|past) month\b", q):
        return now - timedelta(days=30), now, "the last 30 days", used
    for part, (h1, h2) in {
        "morning": (5, 12),
        "afternoon": (12, 17),
        "evening": (17, 24),
        "tonight": (17, 24),
    }.items():
        if re.search(rf"\b{part}\b", q):
            a = anchor.replace(hour=h1)
            b = anchor.replace(hour=23, minute=59) if h2 == 24 else anchor.replace(hour=h2)
            return a, min(b, now), f"{anchor:%a %d %b} {part}", used
    if "yesterday" in q or "today" in q or used or m or m2:
        end = min(anchor + timedelta(days=1), now)
        label = "today" if anchor == today else f"{anchor:%a %d %b}"
        return anchor, end, label, used
    return None, None, "", used


def _norm(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", text.lower()).strip() + " "


def match_project(q: str, projects: list[Project]) -> Project | None:
    """The project whose id or name appears in the question (longest match wins)."""
    nq = _norm(q)
    best: tuple[int, Project] | None = None
    for p in projects:
        if p.archived or p.id == NONE_PROJECT:
            continue
        for phrase in {p.id, p.name}:
            n = _norm(phrase)
            if len(n.strip()) >= 3 and n in nq and (best is None or len(n) > best[0]):
                best = (len(n), p)
    return best[1] if best else None


def parse_question(text: str, projects: list[Project], now: datetime) -> ParsedQuestion:
    now = now.astimezone()
    q = " ".join(text.lower().split())
    start, end, label, _used = parse_time_range(q, now)
    project = match_project(q, projects)
    mention = None
    if project is None:
        m = _MENTION.search(q)
        if m:
            mention = m[1]
    intent: Intent = "search"
    for name, pattern in _INTENTS:
        if pattern.search(q):
            intent = name
            break
    project_words = (
        set(_norm(project.id).split()) | set(_norm(project.name).split()) if project else set()
    )
    terms = [
        t
        for t in query_terms(text)
        if t not in TIME_WORDS
        and t not in project_words
        and not t.isdigit()
        and not re.fullmatch(r"\d{1,2}(:\d{2})?(am|pm)?", t)
    ]
    if intent == "search" and start is not None and not terms:
        intent = "activity"
    if intent == "weekly" and start is None:
        start, end, label = now - timedelta(days=7), now, "the last 7 days"
    return ParsedQuestion(text, intent, project, mention, start, end, label, terms)
