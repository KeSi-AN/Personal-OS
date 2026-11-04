"""Time on a website, an app or a project, and the pages you opened on a site.

"how long did I watch youtube today?", "how much time in chrome this week?", "how long did I work
on personal-os?", "what youtube videos did I watch?". These are sums and lists over what was
captured, so they're answered directly, without the model: the numbers can't be misread, and
every sentence cites the page, window or session lines it comes from.

A site is matched against the domains you've visited (youtube -> www.youtube.com, m.youtube.com),
an app against the apps you've used (chrome, "visual studio code", "file explorer"). Time on a
site is the page's active time (the tab in front, you not idle); time in an app is how long its
window had focus, which already stops when you go idle or lock the screen.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from personal_os.context.evidence import EvidenceLine, fetch_rows, finalize, lines_from_rows
from personal_os.context.question import ParsedQuestion, Target
from personal_os.core.clock import to_ms
from personal_os.projects.sessions import load_sessions

LOOKBACK = timedelta(days=90)  # where site and app names are learned from

USAGE_Q = re.compile(
    r"\bhow (long|much time|many (hours|minutes))\b|\b(time|hours|minutes) (did i )?(spent|spend)\b"
    r"|\bspen[dt]\b"
)
VISITS_Q = re.compile(
    r"\b(what|which)\b.*\b(videos?|pages?|sites?|websites?|articles?|posts?|links?|tabs?|"
    r"repos?|watch(ed|ing)?|visit(ed|ing)?|read|open(ed)?|look(ed|ing)? at|brows(ed|ing))\b"
)
_VIDEO_Q = re.compile(r"\b(videos?|watch(ed|ing)?|clips?|shorts)\b")
_SITE_WORDS = re.compile(
    r"\b(watch\w*|videos?|sites?|websites?|pages?|visit\w*|read\w*|brows\w*)\b"
)

# Words that are never a site or app name, even if a domain or app is called that.
_STOP_WORDS = """how long much time many hours minutes did was were what which watch watching
watched today yesterday week month work working worked spend spent use used using the and for
app apps site sites website page pages video videos on in at with my me you all any"""
STOP = frozenset(_STOP_WORDS.split())
# Generic words in app names that don't identify the app on their own.
_GENERIC_APP_WORDS = """google microsoft windows browser ide app desktop host ui experience start
shell search menu cmdpal the for"""
GENERIC_APP_WORDS = frozenset(_GENERIC_APP_WORDS.split())
APP_ALIASES = {
    "vscode": "code",
    "vs code": "code",
    "file explorer": "explorer",
    "files": "explorer",
}
PRETTY = {
    "youtube": "YouTube",
    "github": "GitHub",
    "chatgpt": "ChatGPT",
    "linkedin": "LinkedIn",
    "stackoverflow": "Stack Overflow",
    "reddit": "Reddit",
}
_SECOND_LEVEL = frozenset({"co", "com", "org", "net", "ac", "gov", "edu"})


def _norm(text: str) -> str:
    return " " + " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split()) + " "


def _site_keys(domain: str) -> tuple[str, list[str]]:
    """(site name, phrases that name it): www.youtube.com gives youtube; meet.google.com gives
    google, meet google and google meet."""
    host = domain.lower().removeprefix("www.")
    labels = host.split(".")
    if len(labels) < 2:
        return host, [host]
    i = -2
    if labels[i] in _SECOND_LEVEL and len(labels) >= 3:
        i = -3
    site = labels[i]
    keys = [site, " ".join(labels)]
    subs = labels[:i]
    if subs:
        keys += [f"{subs[0]} {site}", f"{site} {subs[0]}"]
    return site, keys


def _app_keys(app: str, exe: str) -> list[str]:
    stem = re.sub(r"\.exe$", "", exe.lower())
    keys = [app, stem, *[w for w in _norm(app).split() if w not in GENERIC_APP_WORDS]]
    keys += [alias for alias, target in APP_ALIASES.items() if target == _norm(stem).strip()]
    return keys


def _best(qn: str, candidates: dict[str, set[str]]) -> tuple[str, set[str]] | None:
    found: tuple[str, set[str]] | None = None
    for key, names in candidates.items():
        k = _norm(key)
        word = k.strip()
        if len(word) < 3 or word in STOP or k not in qn:
            continue
        if found is None or len(word) > len(found[0]):
            found = (word, names)
    return found


def find_target(conn: sqlite3.Connection, text: str, now: datetime) -> Target | None:
    """The site or app the question names, from the ones you've actually used."""
    qn = _norm(text)
    since = to_ms(now - LOOKBACK)
    sites: dict[str, set[str]] = {}
    site_of: dict[str, str] = {}
    for (domain,) in conn.execute(
        "SELECT DISTINCT json_extract(payload, '$.domain') FROM events "
        "WHERE source = 'browser' AND type = 'page_visit' AND ts_start >= ?",
        (since,),
    ):
        if not domain:
            continue
        site, keys = _site_keys(domain)
        site_of[domain] = site
        for key in keys:
            sites.setdefault(key, set()).add(domain)
    # A site's own name covers all its subdomains (youtube -> www. and m.youtube.com).
    for domain, site in site_of.items():
        sites.setdefault(site, set()).add(domain)
    apps: dict[str, set[str]] = {}
    for app, exe in conn.execute(
        "SELECT DISTINCT json_extract(payload, '$.app'), json_extract(payload, '$.exe') "
        "FROM events WHERE source = 'window' AND type = 'focus' AND ts_start >= ?",
        (since,),
    ):
        if app:
            for key in _app_keys(app, exe or ""):
                apps.setdefault(key, set()).add(app)
    site_hit, app_hit = _best(qn, sites), _best(qn, apps)
    if site_hit and app_hit and len(site_hit[0]) == len(app_hit[0]):
        # "claude" is both a site and an app: watching/visiting means the site.
        if _SITE_WORDS.search(qn):
            app_hit = None
        else:
            site_hit = None
    if site_hit and (not app_hit or len(site_hit[0]) >= len(app_hit[0])):
        word, domains = site_hit
        if len(domains) == 1 and " " in word:
            label = next(iter(domains)).removeprefix("www.")
        else:
            label = PRETTY.get(word, word.capitalize())
        return Target("site", label, frozenset(domains))
    if app_hit:
        names = app_hit[1]
        return Target("app", sorted(names)[0], frozenset(names))
    return None


def classify(conn: sqlite3.Connection, q: ParsedQuestion, now: datetime) -> ParsedQuestion:
    """Turn a question into a usage or visits question when it names a site or app (a project,
    if named, wins: "how long did I work on personal-os" stays a project question)."""
    if q.project is not None or q.project_mention or q.intent in ("leave_off", "changed_since"):
        return q
    usage, visits = USAGE_Q.search(q.text.lower()), VISITS_Q.search(q.text.lower())
    if not (usage or visits):
        return q
    target = find_target(conn, q.text, now)
    if target is None or (visits and not usage and target.kind != "site"):
        return q
    q.intent = "usage" if usage else "visits"
    q.target = target
    return q


# --- gathering ----------------------------------------------------------------------------------


@dataclass
class Direct:
    """A computed answer: sentences with the refs they rest on, plus the evidence lines."""

    sentences: list[tuple[str, list[int]]]
    lines: list[EvidenceLine]
    facts: list[str]
    scope: str


def _dur(ms: int) -> str:
    m = round(ms / 60000)
    if ms and m == 0:
        return f"{max(1, ms // 1000)}s"
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def _range(q: ParsedQuestion, now: datetime) -> tuple[datetime, datetime, str]:
    start = q.start or now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return start, q.end or now, q.range_label or "today"


def _site_rows(
    conn: sqlite3.Connection, t: Target, start: datetime, end: datetime
) -> list[sqlite3.Row]:
    return [
        r
        for r in fetch_rows(conn, to_ms(start), to_ms(end), sources=("browser",))
        if r["source"] == "browser"
        and r["type"] == "page_visit"
        and json.loads(r["payload"]).get("domain") in t.names
    ]


def _titles(lines: list[EvidenceLine], n: int) -> tuple[str, list[int]]:
    top = sorted(lines, key=lambda x: -x.weight)[:n]
    return ", ".join(_short(x) for x in top), sorted(x.ref for x in top)


def _short(line: EvidenceLine) -> str:
    """A page or window line without what the question already named: the domain of a page,
    the app of a window ("Google Chrome: Inbox - Google Chrome (12m)" -> "'Inbox' (12m)")."""
    text = line.text
    if line.kind == "page":
        m = re.fullmatch(r"(.*) \([^()]*\), (\S+)", text)
        return f"{m[1]} ({m[2]})" if m else text
    m = re.fullmatch(r"(.+?): (.*) \((\S+)\)", text)
    if line.kind == "window" and m:
        app, title, dur = m[1], m[2], m[3]
        title = title.removesuffix(f" - {app}").removesuffix(f" — {app}").strip()
        return f"'{title}' ({dur})" if title else f"{app} ({dur})"
    return text


def site_usage(conn: sqlite3.Connection, q: ParsedQuestion, now: datetime) -> Direct:
    t = q.target
    assert t is not None
    start, end, label = _range(q, now)
    rows = _site_rows(conn, t, start, end)
    scope = f"time on {t.label} {label}"
    if not rows:
        return Direct([], [], [], scope)
    payloads = [json.loads(r["payload"]) for r in rows]
    active = sum(int(p.get("active_dwell_ms") or 0) for p in payloads)
    video_s = sum(int(p.get("video_watched_s") or 0) for p in payloads)
    pack = finalize([x for x in lines_from_rows(rows) if x.kind == "page"], 40)
    fact = f"Active time on {t.label} ({label}): {_dur(active)} over {len(rows)} page visits."
    first, refs = _titles(pack.lines, 3)
    sentences = [
        (
            f"You spent {_dur(active)} on {t.label} {label}, across {len(rows)} page visits "
            "(time with the tab in front and you active).",
            refs,
        )
    ]
    if video_s >= 60 and _VIDEO_Q.search(q.text.lower()):
        sentences.append((f"Videos were playing for about {_dur(video_s * 1000)} of that.", refs))
    if first:
        sentences.append((f"Most of it went to {first}.", refs))
    sentences += _by_day(rows, start, end, refs, lambda p, _r: int(p.get("active_dwell_ms") or 0))
    return Direct(sentences, pack.lines, [fact], scope)


def app_usage(conn: sqlite3.Connection, q: ParsedQuestion, now: datetime) -> Direct:
    t = q.target
    assert t is not None
    start, end, label = _range(q, now)
    lo, hi = to_ms(start), to_ms(end)
    rows = [
        r
        for r in fetch_rows(conn, lo, hi, sources=("window",))
        if r["type"] == "focus" and json.loads(r["payload"]).get("app") in t.names
    ]
    scope = f"time in {t.label} {label}"
    if not rows:
        return Direct([], [], [], scope)

    def clipped(_p: dict[str, Any], r: sqlite3.Row) -> int:
        return max(0, min(int(r["ts_stop"]), hi) - max(int(r["ts_start"]), lo))

    total = sum(clipped({}, r) for r in rows)
    pack = finalize(lines_from_rows(rows), 40)
    fact = f"Focused time in {t.label} ({label}): {_dur(total)}."
    first, refs = _titles(pack.lines, 3)
    sentences = [
        (f"{t.label} was your active window for {_dur(total)} {label} (idle time not counted).",
         refs),
    ]  # fmt: skip
    if first:
        sentences.append((f"The windows you spent longest in: {first}.", refs))
    sentences += _by_day(rows, start, end, refs, clipped)
    return Direct(sentences, pack.lines, [fact], scope)


def _by_day(
    rows: list[sqlite3.Row],
    start: datetime,
    end: datetime,
    refs: list[int],
    ms_of: Callable[[dict[str, Any], sqlite3.Row], int],
) -> list[tuple[str, list[int]]]:
    """For ranges over a day: one sentence with the time per day."""
    if end - start <= timedelta(days=1, hours=1):
        return []
    days: dict[str, int] = {}
    for r in rows:
        day = datetime.fromtimestamp(r["ts_start"] / 1000).astimezone().strftime("%a %d %b")
        days[day] = days.get(day, 0) + ms_of(json.loads(r["payload"]), r)
    parts = ", ".join(f"{d} {_dur(v)}" for d, v in days.items() if v >= 60_000)
    return [(f"By day: {parts}.", refs)] if parts else []


MAX_LISTED = 12


def site_visits(conn: sqlite3.Connection, q: ParsedQuestion, now: datetime) -> Direct:
    t = q.target
    assert t is not None
    start, end, label = _range(q, now)
    rows = _site_rows(conn, t, start, end)
    videos_only = bool(_VIDEO_Q.search(q.text.lower()))
    if videos_only:
        playing = [r for r in rows if _is_video(json.loads(r["payload"]))]
        rows = playing or rows
    what = "videos" if videos_only else "pages"
    scope = f"{what} on {t.label} {label}"
    lines = [x for x in lines_from_rows(rows) if x.kind == "page"]
    if not lines:
        return Direct([], [], [], scope)
    pack = finalize(lines, 60)
    shown = sorted(pack.lines, key=lambda x: -x.weight)[:MAX_LISTED]
    shown.sort(key=lambda x: x.ts)
    total = sum(x.weight for x in pack.lines)
    verb = "watched" if videos_only else "opened"
    sentences: list[tuple[str, list[int]]] = [
        (f"You {verb} {len(pack.lines)} {what} on {t.label} {label} ({_dur(total)} in all):", []),
    ]
    for x in shown:  # one per line when printed
        sentences.append((f"\n- {x.ts.astimezone():%H:%M} {_short(x)}", [x.ref]))
    if len(pack.lines) > len(shown):
        rest = len(pack.lines) - len(shown)
        sentences.append((f"\n…and {rest} shorter ones (`ask --all` lists every one).", []))
    fact = f"{what.capitalize()} on {t.label} ({label}): {len(pack.lines)}."
    return Direct(sentences, pack.lines, [fact], scope)


def _is_video(p: dict[str, object]) -> bool:
    url = str(p.get("url") or "")
    return bool(p.get("video_watched_s")) or "/watch" in url or "/shorts/" in url


def project_usage(conn: sqlite3.Connection, q: ParsedQuestion, now: datetime) -> Direct:
    """Time attributed to a project. A mixed session counts only its share for the project
    (a session labelled personal-os that was half YouTube counts half)."""
    assert q.project is not None
    pid = q.project.id
    start, end, label = _range(q, now)
    scope = f"sessions on {pid} {label}"
    total = 0
    lines: list[EvidenceLine] = []
    count = 0
    for s in load_sessions(conn, start, end):
        share = int((s.stats.get("by_project_s") or {}).get(pid, 0)) * 1000
        if not share and s.project_id == pid:
            share = s.active_ms
        if share < 60_000:
            continue
        count += 1
        total += share
        a, b = s.start.astimezone(), s.end.astimezone()
        ids = [
            r["id"]
            for r in fetch_rows(conn, to_ms(s.start), to_ms(s.end), project_id=pid, limit=10)
        ]
        lines.append(
            EvidenceLine(
                s.start,
                "summary",
                f"session {a:%a %d %b %H:%M}-{b:%H:%M}: {_dur(share)} on {pid}"
                + (f" (of {_dur(s.active_ms)} active)" if share < s.active_ms * 0.9 else ""),
                ids,
                project_id=pid,
                weight=share,
            )
        )
    if not lines:
        return Direct([], [], [], scope)
    pack = finalize(lines, 40)
    refs = [x.ref for x in pack.lines]
    spans = ", ".join(_strip_session(x.text) for x in pack.lines[:6])
    if len(pack.lines) > 6:
        spans += f" and {len(pack.lines) - 6} more"
    sentences = [
        (f"You worked on {pid} for {_dur(total)} {label}, over {count} "
         f"session{'s' * (count != 1)}: {spans}.", refs[:6]),
    ]  # fmt: skip
    fact = f"Time on {pid} ({label}): {_dur(total)}."
    return Direct(sentences, pack.lines, [fact], scope)


def _strip_session(text: str) -> str:
    """Session line -> "Wed 04 Nov 15:36-15:45 (9m)" for listing."""
    m = re.match(r"session (.+?): (\S+) on ", text)
    return f"{m[1]} ({m[2]})" if m else text
