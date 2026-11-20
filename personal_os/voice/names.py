"""Your own names, put back where speech recognition wrote a sound-alike.

Whisper knows English, not your projects: the bench's spoken run (21 Nov) heard "shop-app" as
"Shapa" and "Chordus" as "Cordis", and the answers were then about something else. Here every
run of one to three heard words is compared with the names Donna knows (projects, folders,
apps) by how they sound (a skeleton: the first letter, then the consonants, with look-alike
letters merged) and by spelling (close enough, so "shape" is never taken for shop-app). A
match becomes the name; anything else is left exactly as heard.

    "where did I leave off on Shapa"  ->  "where did I leave off on shop-app"
    "Where did I leave off on Cordis" ->  "Where did I leave off on chordus"
"""

from __future__ import annotations

import difflib
import re

MIN_SKELETON = 2  # two sounds only with spelling this close too
MIN_SPELLING = 0.6  # how close the letters must be as well
_SWAPS = (("ph", "f"), ("ck", "k"), ("ch", "k"), ("c", "k"), ("q", "k"), ("z", "s"), ("x", "ks"))
# Words never taken for a name, whatever they sound like.
_COMMON = frozenset(
    [
        "a",
        "an",
        "and",
        "the",
        "to",
        "of",
        "in",
        "on",
        "at",
        "for",
        "with",
        "my",
        "me",
        "you",
        "it",
        "is",
        "are",
        "was",
        "what",
        "where",
        "when",
        "how",
        "why",
        "open",
        "close",
        "play",
        "pause",
        "stop",
        "show",
        "tell",
        "take",
        "give",
        "make",
        "set",
        "get",
        "go",
        "do",
        "did",
        "done",
        "does",
        "left",
        "off",
        "leave",
        "work",
        "worked",
        "working",
        "project",
        "projects",
        "app",
        "apps",
        "file",
        "folder",
        "code",
        "music",
        "song",
        "video",
        "today",
        "yesterday",
        "last",
        "week",
        "time",
        "now",
        "later",
        "please",
        "donna",
    ]
)


def letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def skeleton(text: str) -> str:
    """How a name sounds, roughly: "shop-app" and "Shapa" are both "shp"."""
    s = letters(text)
    if not s:
        return ""
    for a, b in _SWAPS:
        s = s.replace(a, b)
    s = s[0] + re.sub(r"[aeiouyh]", "", s[1:])
    s = re.sub(r"(.)\1+", r"\1", s)
    return s[:-1] if len(s) > 2 and s.endswith("s") else s  # "roamy" and "roamies"


def _match(heard: str, names: list[str]) -> str | None:
    h = letters(heard)
    if len(h) < 4:
        return None
    sk = skeleton(heard)
    if len(sk) < MIN_SKELETON:
        return None
    best: tuple[float, str] | None = None
    for name in names:
        n = letters(name)
        if not n or n == h:
            continue
        if skeleton(name) != sk:
            continue
        score = difflib.SequenceMatcher(None, h, n).ratio()
        if score >= MIN_SPELLING and (best is None or score > best[0]):
            best = (score, name)
    return best[1] if best else None


# Where a name goes: "leave off on X", "open X", "resume X", "X project". Anywhere else a word
# keeps its meaning ("the cards are ready" sounds like "chordus" too).
_BEFORE = frozenset(
    ["on", "in", "into", "for", "about", "open", "close", "resume", "from", "with", "to", "switch"]
)
_AFTER = frozenset(["project", "repo", "app", "folder"])


def _name_goes_here(words: list[str], i: int, size: int) -> bool:
    before = words[i - 1].strip(".,!?;:'\"").lower() if i > 0 else ""
    after = words[i + size].strip(".,!?;:'\"").lower() if i + size < len(words) else ""
    return before in _BEFORE or after in _AFTER


def correct(text: str, names: list[str]) -> tuple[str, list[tuple[str, str]]]:
    """`text` with sound-alikes of `names` replaced; and what was replaced (heard, name)."""
    words = text.split()
    if not words or not names:
        return text, []
    known = {letters(n) for n in names}
    out: list[str] = []
    changes: list[tuple[str, str]] = []
    i = 0
    while i < len(words):
        done = False
        for size in (3, 2, 1):
            chunk = words[i : i + size]
            if len(chunk) < size:
                continue
            core = [w.strip(".,!?;:'\"") for w in chunk]
            if letters(" ".join(core)) in known:  # already a name ("shop app"): kept whole
                out.extend(chunk)
                i += size
                done = True
                break
            if not _name_goes_here(words, i, size) or any(c.lower() in _COMMON for c in core):
                continue
            name = _match(" ".join(core), names)
            if name is None:
                continue
            tail = chunk[-1][len(chunk[-1].rstrip(".,!?;:'\"")) :]  # keep the punctuation
            out.append(name + tail)
            changes.append((" ".join(core), name))
            i += size
            done = True
            break
        if not done:
            out.append(words[i])
            i += 1
    return " ".join(out), changes
