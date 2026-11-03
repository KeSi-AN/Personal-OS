"""Daemon-side understanding of a page visit (DESIGN.md §4.5): search queries, content type,
and site-specific details for GitHub, Stack Overflow, docs sites and YouTube.

Pure functions over the URL plus a few hints the content script sends. They run at ingest,
before the privacy filter, but only derive fields from data that is already in the event; the
filter then redacts or drops the event as a whole, so nothing derived here can get past it.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

_SEARCH_ENGINES: list[tuple[re.Pattern[str], str, str, str]] = [
    # (host regex, path prefix, query param, engine name)
    (re.compile(r"(^|\.)google\.[a-z.]+$"), "/search", "q", "google"),
    (re.compile(r"(^|\.)bing\.com$"), "/search", "q", "bing"),
    (re.compile(r"(^|\.)duckduckgo\.com$"), "/", "q", "duckduckgo"),
    (re.compile(r"^search\.brave\.com$"), "/search", "q", "brave"),
    (re.compile(r"(^|\.)youtube\.com$"), "/results", "search_query", "youtube"),
    (re.compile(r"^github\.com$"), "/search", "q", "github"),
    (re.compile(r"(^|\.)stackoverflow\.com$"), "/search", "q", "stackoverflow"),
    (re.compile(r"(^|\.)perplexity\.ai$"), "/search", "q", "perplexity"),
]

_SO_HOSTS = re.compile(
    r"(^|\.)(stackoverflow\.com|stackexchange\.com|superuser\.com|serverfault\.com|askubuntu\.com)$"
)
_DOCS_HOSTS = re.compile(
    r"(^docs\.|\.readthedocs\.io$|^developer\.mozilla\.org$|^learn\.microsoft\.com$|"
    r"^pkg\.go\.dev$|^docs\.rs$|^doc\.rust-lang\.org$|^devdocs\.io$|^nodejs\.org$|"
    r"^react\.dev$|^nextjs\.org$|^fastapi\.tiangolo\.com$|^pydantic\.dev$|^kotlinlang\.org$)"
)
_MAIL_HOSTS = re.compile(r"^(mail\.google\.com|outlook\.(live|office|office365)\.com)$")
_CHAT_HOSTS = re.compile(
    r"^(chatgpt\.com|chat\.openai\.com|claude\.ai|gemini\.google\.com|app\.slack\.com|"
    r"teams\.microsoft\.com|teams\.live\.com|web\.whatsapp\.com|discord\.com)$"
)
_MEETING_HOSTS = re.compile(r"^(meet\.google\.com|([a-z0-9-]+\.)?zoom\.us|teams\.microsoft\.com)$")
_VERSION_RE = re.compile(r"^v?\d+(\.\d+)*$|^(latest|stable|dev|main|master)$")


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def search_query(url: str) -> tuple[str, str] | None:
    """(engine, query) if `url` is a search results page."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    for host_re, path, param, engine in _SEARCH_ENGINES:
        if host_re.search(host) and parts.path.startswith(path):
            values = parse_qs(parts.query).get(param)
            if values and values[0].strip():
                return engine, values[0].strip()[:300]
    return None


def map_transition(
    transition: str | None, qualifiers: list[str] | None, referrer: str | None
) -> str:
    """Chrome's webNavigation transitionType/qualifiers → our coarse vocabulary."""
    q = set(qualifiers or [])
    if "forward_back" in q:
        return "back_forward"
    if referrer and search_query(referrer):
        return "search"
    return {
        "generated": "search",  # typed a search into the omnibox
        "typed": "typed",
        "link": "link",
        "auto_bookmark": "bookmark",
        "reload": "reload",
        "form_submit": "form_submit",
        "keyword": "search",
        "keyword_generated": "search",
        "auto_toplevel": "other",
        "start_page": "other",
        "manual_subframe": "link",
    }.get(transition or "", transition or "other")


def _github(path: list[str]) -> tuple[str, dict[str, Any]]:
    reserved = {
        "settings",
        "notifications",
        "explore",
        "marketplace",
        "search",
        "login",
        "orgs",
        "topics",
        "trending",
        "new",
        "pulls",
        "issues",
        "codespaces",
        "sponsors",
    }
    if len(path) < 2 or path[0] in reserved:
        return "github", {}
    info: dict[str, Any] = {"repo": f"{path[0]}/{path[1]}"}
    if len(path) >= 4 and path[2] in ("issues", "pull") and path[3].isdigit():
        info["number"] = int(path[3])
        return ("github_pr" if path[2] == "pull" else "github_issue"), info
    if len(path) >= 4 and path[2] in ("tree", "blob"):
        info["branch"] = path[3]
        if path[2] == "blob":
            info["file"] = "/".join(path[4:])[:200]
            return "github_code", info
    if len(path) >= 3 and path[2] in (
        "actions",
        "commits",
        "commit",
        "compare",
        "releases",
        "wiki",
    ):
        info["section"] = path[2]
    return "github_repo", info


def _docs(host: str, path: list[str]) -> dict[str, Any]:
    lib: str | None = None
    version: str | None = None
    if host == "docs.python.org":
        lib = "python"
        version = path[0] if path and _VERSION_RE.match(path[0]) else None
    elif host.endswith(".readthedocs.io"):
        lib = host.split(".")[0]
        if len(path) >= 2 and path[0] in ("en", "latest") and _VERSION_RE.match(path[1]):
            version = path[1]
    elif host == "docs.rs" and path:
        lib = path[0]
        version = path[1] if len(path) > 1 and _VERSION_RE.match(path[1]) else None
    elif host == "pkg.go.dev" and path:
        lib = "/".join(path[:3])
    elif host == "developer.mozilla.org":
        lib = "mdn"
    elif host == "learn.microsoft.com":
        lib = "microsoft"
    elif host.startswith("docs."):
        lib = host.split(".")[1]
    else:
        lib = host.split(".")[0]
    return {k: v for k, v in {"library": lib, "version": version}.items() if v}


def classify(
    url: str, *, word_count: int = 0, hints: dict[str, Any] | None = None
) -> tuple[str, dict[str, Any]]:
    """(content_type, enrichment) for a page."""
    hints = hints or {}
    try:
        parts = urlsplit(url)
    except ValueError:
        return "other", {}
    host = (parts.hostname or "").lower()
    path = [p for p in parts.path.split("/") if p]

    sq = search_query(url)
    if sq:
        return "search_results", {"engine": sq[0]}
    if host == "github.com":
        return _github(path)
    if _SO_HOSTS.search(host) and len(path) >= 2 and path[0] == "questions" and path[1].isdigit():
        info: dict[str, Any] = {"question_id": int(path[1])}
        if isinstance(hints.get("soAccepted"), bool):
            info["accepted_answer"] = hints["soAccepted"]
        return "stackoverflow", info
    if host.endswith("youtube.com") and path[:1] == ["watch"]:
        vid = parse_qs(parts.query).get("v", [None])[0]
        info = {"video_id": vid} if vid else {}
        if isinstance(hints.get("ytChannel"), str) and hints["ytChannel"]:
            info["channel"] = hints["ytChannel"][:100]
        return "video", info
    if host == "youtu.be" and path:
        return "video", {"video_id": path[0]}
    if _MAIL_HOSTS.match(host):
        return "mail", {}
    if _MEETING_HOSTS.match(host):
        return "meeting", {}
    if _CHAT_HOSTS.match(host):
        return "chat", {}
    if _DOCS_HOSTS.search(host) or "docs" in path[:2]:
        return "docs", _docs(host, path)
    if word_count >= 300:
        return "article", {}
    return "other", {}
