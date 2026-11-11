"""How your time was spent: building, research, chill, gaming, communication, other, idle.

Apps decide most of it (VS Code is building, a game is gaming). In a browser the page in
front decides, and on YouTube the video does: a tutorial, talk or anything about AI,
programming or your tools is research, not chill; songs, episodes, trailers and Shorts are
chill. Titles the rules can't place are asked of the local model once, in a batch, and the
verdict is cached (data dir/cache/media_kinds.json) so each video is judged one time only.

Time comes from the window record (focused intervals) and, inside browsers, each page's
active dwell. Gaps between focused intervals up to IDLE_MAX (30 min) are idle (at the
computer, no input); longer gaps are "away" and not counted.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from personal_os.core.clock import from_ms, to_ms

Category = Literal["building", "research", "chill", "gaming", "communication", "other", "idle"]
CATEGORIES: tuple[Category, ...] = (
    "building", "research", "chill", "gaming", "communication", "other", "idle",
)  # fmt: skip
PRODUCTIVE = ("building", "research")
IDLE_MAX = timedelta(minutes=30)  # longer gaps are "away": not charged as idle
BROWSERS = {"brave.exe", "chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "vivaldi.exe"}

_BUILD_APPS = {
    "code.exe", "cursor.exe", "antigravity.exe", "windsurf.exe", "windowsterminal.exe",
    "powershell.exe", "pwsh.exe", "cmd.exe", "wt.exe", "mintty.exe", "alacritty.exe",
    "wezterm-gui.exe", "docker desktop.exe", "idea64.exe", "pycharm64.exe", "webstorm64.exe",
    "devenv.exe", "rider64.exe", "clion64.exe", "goland64.exe", "sublime_text.exe",
    "notepad++.exe", "postman.exe", "insomnia.exe", "github desktop.exe", "gitkraken.exe",
    "claude.exe", "zed.exe", "android studio.exe", "studio64.exe", "unity.exe", "blender.exe",
    "figma.exe", "obsidian.exe", "notion.exe",
}  # fmt: skip
_GAME_APPS = {
    "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe", "riotclientservices.exe",
    "valorant.exe", "valorant-win64-shipping.exe", "leagueclient.exe", "league of legends.exe",
    "cs2.exe", "dota2.exe", "fortniteclient-win64-shipping.exe", "minecraft.exe",
    "javaw.exe", "robloxplayerbeta.exe", "gta5.exe", "rdr2.exe", "eldenring.exe",
    "overwatch.exe", "apex_legends.exe", "r5apex.exe", "rocketleague.exe", "battle.net.exe",
    "eadesktop.exe", "ubisoftconnect.exe", "upc.exe", "xboxpcapp.exe", "genshinimpact.exe",
}  # fmt: skip
_GAME_PATHS = ("steamapps", "epic games", "riot games", "xboxgames", "\\games\\", "/games/",
               "gog galaxy", "ea games", "ubisoft game launcher")  # fmt: skip
_CHILL_APPS = {"vlc.exe", "spotify.exe", "netflix.exe", "potplayermini64.exe", "mpc-hc64.exe",
               "photos.exe", "primevideo.exe", "disney+.exe"}  # fmt: skip
_COMM_APPS = {"discord.exe", "whatsapp.exe", "telegram.exe", "slack.exe", "teams.exe",
              "ms-teams.exe", "zoom.exe", "outlook.exe", "olk.exe", "thunderbird.exe",
              "signal.exe"}  # fmt: skip

_BUILD_SITES = ("github.com", "gitlab.com", "bitbucket.org", "localhost", "127.0.0.1",
                "vercel.com", "netlify.app", "render.com", "railway.app", "console.aws.amazon.com",
                "console.cloud.google.com", "portal.azure.com", "codesandbox.io", "stackblitz.com",
                "replit.com", "vercel.app", "huggingface.co", "colab.research.google.com", "kaggle.com",
                "supabase.com", "firebase.google.com", "npmjs.com", "pypi.org")  # fmt: skip
_RESEARCH_SITES = ("stackoverflow.com", "stackexchange.com", "readthedocs.io", "docs.", "developer.",
                   "learn.microsoft.com", "developer.mozilla.org", "arxiv.org", "wikipedia.org",
                   "claude.ai", "chatgpt.com", "chat.openai.com", "gemini.google.com",
                   "perplexity.ai", "medium.com", "dev.to", "geeksforgeeks.org", "w3schools.com",
                   "coursera.org", "udemy.com", "edx.org", "leetcode.com", "anthropic.com",
                   "openai.com", "towardsdatascience.com", "paperswithcode.com", "scholar.google.com",
                   "freecodecamp.org", "realpython.com", "python.org")  # fmt: skip
_CHILL_SITES = ("netflix.com", "primevideo.com", "hotstar.com", "jiocinema.com", "instagram.com",
                "reddit.com", "twitter.com", "x.com", "facebook.com", "twitch.tv", "music.youtube.com",
                "open.spotify.com", "9gag.com", "imdb.com", "crunchyroll.com", "zee5.com",
                "sonyliv.com", "tiktok.com", "pinterest.com", "disneyplus.com")  # fmt: skip
_COMM_SITES = ("mail.google.com", "outlook.live.com", "outlook.office.com", "web.whatsapp.com",
               "discord.com", "slack.com", "teams.microsoft.com", "meet.google.com", "zoom.us",
               "linkedin.com/messaging", "web.telegram.org")  # fmt: skip
_GAME_SITES = ("store.steampowered.com", "steamcommunity.com", "epicgames.com", "chess.com",
               "lichess.org", "poki.com", "miniclip.com")  # fmt: skip

# Streaming and anime sites by their names (there are too many to list one by one).
_STREAMING = re.compile(
    r"(anime|anikoto|aniwatch|gogoanime|hianime|zoro|9anime|kissanime|crunchyroll|manga|"
    r"fmovies|movies?|flix|putlocker|soap2day|123movies|solarmovie|kdrama|dramacool|toon|"
    r"stream|watchseries|series|cinema|hdhub|filmy|torrent)",
    re.I,
)
_LOCAL = re.compile(
    r"^(localhost|127\.0\.0\.1|\[::1\]|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+)(:\d+)?$"
)
_JOBS = re.compile(
    r"(^|\.)(jobs?|careers?)\.|amazon\.jobs|naukri|indeed|glassdoor|wellfound|instahyre|"
    r"internshala|freshers|linkedin\.com/jobs|myworkdayjobs|oraclecloud\.com|lever\.co|greenhouse\.io",
    re.I,
)

# On YouTube: what makes a video research (learning) or chill.
_LEARN = re.compile(
    r"\b(tutorial|course|lecture|explained|explain|how to|how do|learn|learning|guide|introduction|"
    r"intro to|deep dive|walkthrough|crash course|for beginners|masterclass|interview|system design|"
    r"conference|keynote|talk|paper|review of|benchmark|build(ing)?|coding|code|programming|"
    r"developer|engineering|python|javascript|typescript|react|node|rust|golang|java|c\+\+|sql|"
    r"linux|git|docker|kubernetes|aws|azure|gcp|cloud|api|backend|frontend|database|algorithm|"
    r"data structures|leetcode|machine learning|deep learning|neural|transformer|llm|llms|gpt|"
    r"claude|anthropic|openai|gemini|ai|a\.i\.|agents?|agentic|rag|mcp|prompt|fine[- ]?tun\w*|"
    r"embedding|vector|startup|product|design system|ux|math|physics|statistics|finance|"
    r"productivity|research|study|exam|ielts|gate|upsc|science|history of)\b",
    re.I,
)
_CHILL = re.compile(
    r"\b(song|songs|official (music )?video|lyrics?|lyrical|audio|full album|trailer|teaser|"
    r"reaction|reacts?|vlog|funny|comedy|stand[- ]?up|meme|prank|highlights|gameplay|let'?s play|"
    r"episode|ep ?\d+|s\d+ ?e\d+|season \d+|web ?series|movie|film scene|roast|asmr|lo-?fi|"
    r"remix|cover|jukebox|mashup|dance|bgmi|valorant|minecraft|gta|fortnite|cricket|ipl|football|"
    r"latent|podcast clips?|shorts?)\b",
    re.I,
)


@dataclass
class Breakdown:
    start: datetime
    end: datetime
    by_category: dict[str, int] = field(default_factory=lambda: defaultdict(int))  # ms
    by_hour: dict[int, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int))
    )
    by_project: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    by_app: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    by_site: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    research: list[dict[str, Any]] = field(default_factory=list)  # pages/videos counted as research
    chill_titles: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    unknown_titles: list[str] = field(default_factory=list)

    @property
    def active_ms(self) -> int:
        return sum(v for k, v in self.by_category.items() if k != "idle")

    @property
    def productive_share(self) -> float:
        a = self.active_ms
        return sum(self.by_category.get(c, 0) for c in PRODUCTIVE) / a if a else 0.0


def _domain(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return (m[1] if m else "").lower().removeprefix("www.")


def _has(domain: str, url: str, sites: tuple[str, ...]) -> bool:
    return any(s in domain or (("/" in s) and s in url) for s in sites)


def classify_app(exe: str, exe_path: str = "", app: str = "") -> Category | None:
    """A category for an app, or None for a browser (its pages decide)."""
    e = (exe or "").lower()
    path = (exe_path or "").lower()
    if e in BROWSERS:
        return None
    if e in _GAME_APPS or any(g in path for g in _GAME_PATHS):
        return "gaming"
    if e in _BUILD_APPS:
        return "building"
    if e in _COMM_APPS:
        return "communication"
    if e in _CHILL_APPS:
        return "chill"
    return "other"


def media_key(title: str) -> str:
    return hashlib.sha1(title.strip().lower().encode("utf-8")).hexdigest()[:16]


def classify_page(
    url: str, title: str, content_type: str = "", cache: dict[str, Any] | None = None
) -> tuple[Category | None, str]:
    """(category, why). None = an unknown YouTube video, to ask the model about."""
    d = _domain(url)
    u = (url or "").lower()
    t = title or ""
    if d.endswith("youtube.com") and not d.startswith("music."):
        if "/shorts/" in u:
            return "chill", "short"
        if "/watch" not in u:
            return ("research", "search") if "/results" in u else ("chill", "browsing")
        hit = (cache or {}).get(media_key(t))
        if hit:
            return ("research" if hit.get("kind") == "learning" else "chill"), "model"
        learn, chill = bool(_LEARN.search(t)), bool(_CHILL.search(t))
        if learn and not chill:
            return "research", "title"
        if chill and not learn:
            return "chill", "title"
        return None, "unsure"
    if _has(d, u, _COMM_SITES) or content_type in ("mail", "meeting"):
        return "communication", "site"
    if _has(d, u, _GAME_SITES):
        return "gaming", "site"
    if _has(d, u, _CHILL_SITES) or _STREAMING.search(d) or "ultimate-guitar" in d:
        return "chill", "site"
    if _LOCAL.match(d) or _has(d, u, _BUILD_SITES) or content_type in ("github_repo", "github"):
        return "building", "site"
    if _JOBS.search(d) or _JOBS.search(u):
        return "research", "career"  # job hunting is work on your career, not chill
    if _has(d, u, _RESEARCH_SITES) or content_type in ("chat", "document", "search_results"):
        return "research", "site"
    if content_type in ("video",):
        return ("research" if _LEARN.search(t) else "chill"), "title"
    if content_type == "article" and _LEARN.search(t):
        return "research", "title"
    return "other", "site"


# --- the model, for videos the rules can't place -----------------------------------------------


def cache_path(data_dir: Path) -> Path:
    return data_dir / "cache" / "media_kinds.json"


def load_cache(data_dir: Path) -> dict[str, Any]:
    try:
        data = json.loads(cache_path(data_dir).read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(data_dir: Path, cache: dict[str, Any]) -> None:
    from personal_os.core import runtime

    p = cache_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_json_atomic(p, cache)


KIND_SYSTEM = """You sort YouTube video titles. For each numbered title decide:
- "learning": tutorials, lectures, talks, explanations, tech, AI, programming, science, study,
  career, productivity, product or tool reviews someone watches to learn
- "entertainment": music, songs, comedy, shows, episodes, vlogs, gaming videos, reactions,
  sports, memes, trailers
and give a short topic (2-4 words) for learning ones ("Claude agents", "React hooks").
Reply with JSON only."""


def classify_titles(provider: Any, titles: list[str], cache: dict[str, Any]) -> int:
    """Ask the local model about titles not in the cache (at most 40 at a time). Returns how
    many were added. Never raises: an unusable answer just leaves them unknown."""
    from pydantic import BaseModel

    todo = [t for t in dict.fromkeys(titles) if t and media_key(t) not in cache][:40]
    if not todo or provider is None:
        return 0

    class Item(BaseModel):
        i: int
        kind: Literal["learning", "entertainment"]
        topic: str = ""

    class Out(BaseModel):
        items: list[Item]

    user = "\n".join(f"{n}. {t[:160]}" for n, t in enumerate(todo, start=1))
    try:
        got = provider.chat_json(KIND_SYSTEM, user, Out, temperature=0, max_tokens=60 * len(todo))
    except Exception:
        return 0
    added = 0
    for it in got.items:
        if 1 <= it.i <= len(todo):
            cache[media_key(todo[it.i - 1])] = {"kind": it.kind, "topic": it.topic.strip()[:40]}
            added += 1
    return added


# --- the breakdown ---------------------------------------------------------------------------


def breakdown(
    conn: sqlite3.Connection, start: datetime, end: datetime, cache: dict[str, Any] | None = None
) -> Breakdown:
    from personal_os.context.evidence import fetch_rows

    lo, hi = to_ms(start), to_ms(end)
    b = Breakdown(start, end)
    rows = fetch_rows(conn, lo, hi, sources=("window", "browser"))

    def add(cat: str, a_ms: int, b_ms: int, project: str | None) -> None:
        a_ms, b_ms = max(a_ms, lo), min(b_ms, hi)
        if b_ms <= a_ms:
            return
        b.by_category[cat] += b_ms - a_ms
        if project and project != "none":
            b.by_project[project] += b_ms - a_ms
        t = a_ms
        while t < b_ms:  # split across hours, for the chart
            hour_end = (t // 3_600_000 + 1) * 3_600_000
            piece = min(hour_end, b_ms) - t
            b.by_hour[int(from_ms(t).astimezone().hour)][cat] += piece
            t += piece

    focus = [r for r in rows if r["source"] == "window" and r["type"] == "focus"]
    pages = [r for r in rows if r["source"] == "browser" and r["type"] == "page_visit"]
    browser_ms = 0
    for r in focus:
        p = json.loads(r["payload"])
        exe = str(p.get("exe") or "")
        cat = classify_app(exe, str(p.get("exe_path") or ""), str(p.get("app") or ""))
        s, e = int(r["ts_start"]), int(r["ts_stop"])
        if cat is None:
            browser_ms += max(0, min(e, hi) - max(s, lo))
            continue
        add(cat, s, e, r["project_id"])
        b.by_app[str(p.get("app") or exe)] += max(0, min(e, hi) - max(s, lo))
    # Pages can overlap (two windows, a page left playing): never count more page time than
    # the browser was actually in front. Only where the window record exists, though: while
    # capture was off the extension still kept pages, and those stand as they are.
    any_spans = sorted((int(r["ts_start"]), int(r["ts_stop"])) for r in focus)

    def covered(ts: int) -> bool:
        """Is there a window record around this moment (capture was running)?"""
        return any(a - 300_000 <= ts <= b + 300_000 for a, b in any_spans)

    in_record = [r for r in pages if covered(int(r["ts_start"]))]
    total_dwell = sum(int(json.loads(r["payload"]).get("active_dwell_ms") or 0) for r in in_record)
    scale = min(1.0, browser_ms / total_dwell) if total_dwell and browser_ms else 1.0
    page_ms = 0
    for r in pages:
        p = json.loads(r["payload"])
        url, title = str(p.get("url") or ""), str(p.get("title") or "")
        factor = scale if covered(int(r["ts_start"])) else 1.0
        dwell = int(int(p.get("active_dwell_ms") or 0) * factor)
        if dwell <= 0:
            continue
        cat, _why = classify_page(url, title, str(p.get("content_type") or ""), cache)
        if cat is None:
            b.unknown_titles.append(title)
            cat = "chill"  # until the model says otherwise
        s = int(r["ts_start"])
        add(cat, s, s + dwell, r["project_id"])
        page_ms += dwell
        b.by_site[_domain(url)] += dwell
        if cat == "research":
            topic = ((cache or {}).get(media_key(title)) or {}).get("topic", "")
            b.research.append({"title": title, "url": url, "ms": dwell, "topic": topic,
                               "project": r["project_id"], "at": s})  # fmt: skip
        elif cat == "chill" and title:
            b.chill_titles[title] += dwell
    covered_ms = sum(
        int(int(json.loads(r["payload"]).get("active_dwell_ms") or 0) * scale) for r in in_record
    )
    if browser_ms > covered_ms:  # browser in front, no page counted (new tab, settings...)
        b.by_category["other"] += browser_ms - covered_ms
    # Idle: short gaps between focused windows.
    spans = sorted((max(int(r["ts_start"]), lo), min(int(r["ts_stop"]), hi)) for r in focus)
    for (_a1, b1), (a2, _b2) in itertools.pairwise(spans):
        gap = a2 - b1
        if 60_000 <= gap <= IDLE_MAX.total_seconds() * 1000:
            add("idle", b1, a2, None)
    return b


def flag(b: Breakdown) -> list[str]:
    """Plain observations about the time: what stood out."""
    out = []
    a = b.active_ms
    if not a:
        return ["Nothing was recorded in this window."]
    chill = b.by_category.get("chill", 0)
    if chill >= 45 * 60_000 and chill / a >= 0.3:
        top = sorted(b.chill_titles.items(), key=lambda kv: -kv[1])[:2]
        what = "; ".join(t for t, _ in top)
        out.append(f"Chill took {chill // 60_000} min ({round(100 * chill / a)}% of active time)"
                   + (f", mostly: {what}" if what else "") + ".")  # fmt: skip
    if b.by_category.get("research", 0) >= 20 * 60_000:
        out.append(
            f"Research: {b.by_category['research'] // 60_000} min of learning, which counts as productive."
        )
    idle = b.by_category.get("idle", 0)
    if idle >= 60 * 60_000:
        out.append(f"Idle at the computer for {idle // 60_000} min.")
    if b.by_category.get("gaming", 0) >= 60 * 60_000:
        out.append(f"Gaming: {b.by_category['gaming'] // 60_000} min.")
    return out


def in_front(snap: Any, cache: dict[str, Any] | None = None) -> Category | None:
    """The category of what's in front now: the app, or in a browser its current tab. None
    when it can't be told (no window, an unknown video)."""
    front = next((w for w in snap.windows if w.front), None)
    if front is None:
        return None
    cat = classify_app(front.exe, front.exe_path)
    if cat is not None:
        return cat
    name = front.exe.lower().removesuffix(".exe")
    tabs = [t for t in snap.tabs if t.active and (t.browser in name or name in t.browser)]
    tabs = tabs or [t for t in snap.tabs if t.active]
    if not tabs:
        return None
    return classify_page(tabs[0].url, tabs[0].title, "", cache)[0]
