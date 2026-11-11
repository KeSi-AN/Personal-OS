"""Sites as data: where to search, and how to pick the first result (ADR-076).

Adding a site is adding an entry here, not writing a tool. `search` is the results page for a
query; `first` (optional) is a CSS selector for the link to the first real result, which the
extension opens ("play X on YouTube" = search, then open the first video, which plays).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote_plus


@dataclass(frozen=True)
class Site:
    name: str
    search: str  # with {q}
    first: str = ""  # selector for the first result's link, if it can be opened directly
    aliases: tuple[str, ...] = ()
    home: str = ""  # the site itself, for "open YouTube Music in Brave"


SITES = (
    Site(
        "google",
        "https://www.google.com/search?q={q}",
        aliases=("web", "the web", "internet"),
        home="https://www.google.com/",
    ),
    Site(
        "youtube",
        "https://www.youtube.com/results?search_query={q}",
        "ytd-video-renderer a#video-title[href*='watch?v='], a#video-title[href*='watch?v=']",
        aliases=("yt",),
        home="https://www.youtube.com/",
    ),
    Site(
        "youtube music",
        "https://music.youtube.com/search?q={q}",
        "a[href*='watch?v=']",
        aliases=("yt music", "ytmusic", "youtube-music"),
        home="https://music.youtube.com/",
    ),
    Site("github", "https://github.com/search?q={q}&type=repositories", home="https://github.com/"),
    Site(
        "wikipedia",
        "https://en.wikipedia.org/w/index.php?search={q}",
        aliases=("wiki",),
        home="https://en.wikipedia.org/",
    ),
    Site(
        "maps",
        "https://www.google.com/maps/search/{q}",
        aliases=("google maps",),
        home="https://www.google.com/maps",
    ),
    Site(
        "stack overflow",
        "https://stackoverflow.com/search?q={q}",
        aliases=("stackoverflow",),
        home="https://stackoverflow.com/",
    ),
    Site("amazon", "https://www.amazon.in/s?k={q}", home="https://www.amazon.in/"),
)


def find(name: str) -> Site | None:
    n = name.lower().strip()
    for s in SITES:
        if n == s.name or n in s.aliases:
            return s
    return None


def search_url(site: Site, query: str) -> str:
    return site.search.format(q=quote_plus(query.strip()))
