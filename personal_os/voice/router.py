"""The fast path (spec §6): spoken commands matched by rules and fuzzy names, no LLM.

"open brave", "open my resume", "open typewriter in vs code", "show resume in explorer",
"volume up", "set volume to 40", "mute", "lock the screen", "take a screenshot",
"brightness 70", "minimize chrome", "what time is it".

Names are matched with rapidfuzz against the index (voice/index.py). A score at or above the
threshold (0.85) runs at once; a near miss ("open brat": Brave 0.67) comes back as a guess
the voice loop can check, first by listening again with recognition biased to the candidates,
then by asking "Did you mean Brave?". Anything that isn't one of these commands returns None
and goes to the LLM.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PureWindowsPath as Path
from typing import Any

from rapidfuzz import fuzz

from personal_os.context.route import strip_polite, strip_wake
from personal_os.voice.index import Entry, Index, norm

THRESHOLD = 0.85
GUESS_FLOOR = 0.55  # below this a name isn't worth asking about
TIE_MARGIN = 0.04  # two names this close: ask which
SHORT_NAME = 5  # names this short need a closer match
SHORT_THRESHOLD = 0.9
CLEAR_MATCH = 0.72  # this close, and ...
CLEAR_LEAD = 0.12  # ... this far ahead of every other name: it's the one you mean
KIND_ORDER = {"app": 0, "project": 1, "folder": 2, "file": 3}
WHOLE_NAME = 0.9  # the whole phrase matches a name this well: take it as said

NUMBERS = {
    "zero": 0, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "a hundred": 100, "half": 50,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9,
}  # fmt: skip


@dataclass
class Match:
    entry: Entry
    score: float


@dataclass
class Intent:
    """A command the fast path can run: tool calls and the short thing to say."""

    calls: list[tuple[str, dict[str, Any]]]
    reply: str
    confidence: float = 1.0
    # A near miss: the names it might be, best first (the voice loop checks before acting).
    guesses: list[Match] = field(default_factory=list)
    question: str = ""
    verb: str = "open"
    # Builds the command for the guess you pick.
    build: Callable[[Entry], Intent] | None = None

    @property
    def sure(self) -> bool:
        return not self.guesses and not self.question

    def phrase(self, entry: Entry) -> str:
        """What you'd have said for this guess: "open Brave"."""
        return f"{self.verb} {entry.name}"


RECHECK_TRUST = 0.7  # a biased re-decode may only confirm a guess at least this good


def targets(intent: Intent) -> set[str]:
    """What a command would open (apps, paths, targets), for comparing two hearings."""
    out = set()
    for _tool, args in intent.calls:
        for key in ("app", "path", "target"):
            if isinstance(args.get(key), str):
                out.add(args[key])
    return out


def confirms(rechecked: Intent | None, guess: Intent) -> bool:
    """Does a biased re-decode confirm one of the strong guesses? Biasing towards candidates
    makes the model pick one of them whatever was said, so it may only confirm a guess the
    first hearing already rated well, never pick a weak one ("open brat" -> "open read")."""
    if rechecked is None or not rechecked.sure:
        return False
    strong = {
        g.entry.target for i, g in enumerate(guess.guesses) if i == 0 or g.score >= RECHECK_TRUST
    }
    return bool(targets(rechecked) & strong)


_ORDINAL = re.compile(
    r"\b(first|second|third|last)\b|\b(?:number|option)\s+(one|two|three|1|2|3)\b", re.I
)
_ORDINAL_INDEX = {"first": 0, "second": 1, "third": 2, "one": 0, "two": 1, "three": 2,
                  "1": 0, "2": 1, "3": 2}  # fmt: skip
_YES = re.compile(r"\b(yes|yeah|yep|yup|sure|right|correct|that one|do it|ok(ay)?)\b", re.I)
_NO = re.compile(r"\b(no|nope|neither|none|cancel|never ?mind|stop)\b", re.I)


def choose(answer: str, intent: Intent, *, threshold: float = THRESHOLD) -> Intent | None:
    """Your answer to "Did you mean ...?": yes (the first guess), "the second one", or a name
    (matched against the guesses, then against everything). None for no or nothing."""
    if not answer or intent.build is None or not intent.guesses:
        return None
    a = answer.lower()
    m = _ORDINAL.search(a)
    if m:
        word = (m[1] or m[2]).lower()
        i = len(intent.guesses) - 1 if word == "last" else _ORDINAL_INDEX[word]
        if i < len(intent.guesses):
            return intent.build(intent.guesses[i].entry)
    # "the one in macos rag finder": a folder on its path that the others don't share.
    said_flat = norm(a).replace(" ", "")
    folders = [
        {norm(p).replace(" ", "") for p in Path(g.entry.target).parts[:-1] if len(norm(p)) >= 3}
        for g in intent.guesses
    ]
    in_folder = [
        g
        for i, g in enumerate(intent.guesses)
        if any(
            f in said_flat and all(f not in o for j, o in enumerate(folders) if j != i)
            for f in folders[i]
        )
    ]
    if len(in_folder) == 1:
        return intent.build(in_folder[0].entry)
    named = best(_clean(answer), [g.entry for g in intent.guesses], n=1)
    if named and named[0].score >= 0.75:
        return intent.build(named[0].entry)
    if _NO.search(a):
        return None
    if _YES.search(a):
        return intent.build(intent.guesses[0].entry)
    return None


def _clean(text: str) -> str:
    # Hint words can come back glued to the verb: "OpenBrave." -> "Open Brave."
    text = re.sub(r"\b((?i:open|launch|start|show|close))(?=[A-Z])", r"\1 ", text.strip())
    t = strip_polite(strip_wake(text)).strip()
    t = re.sub(r"[.!?,]+$", "", t)
    # Punctuation said out loud: "agentic underscore EDA", "macos dash rag dash finder".
    t = re.sub(r"\s+(?:underscore|under score)\s+", "_", t, flags=re.I)
    t = re.sub(r"\s+(?:dash|hyphen|minus)\s+", "-", t, flags=re.I)
    t = re.sub(r"\s+dot\s+(\w{2,4})\b", r".\1", t, flags=re.I)
    return t.strip()


SUBSET_WEIGHT = 0.93
WORD_MATCH = 0.8


def score(said: str, entry: Entry) -> float:
    a, b = norm(said), entry.key
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    whole = fuzz.ratio(a, b) / 100
    joined = fuzz.ratio(a.replace(" ", ""), b.replace(" ", "")) / 100  # "vs code" = "vscode"
    return max(whole, joined, _subset(a, b))


def _subset(a: str, b: str) -> float:
    """What you said is part of the name ("chrome" in "google chrome"): high, but under an
    exact name. Only that way round: a name that's part of what you said ("os" in "personal
    os", "site" in "personal site") leaves words unexplained, so it isn't a match."""
    said, name = a.split(), b.split()
    if len(said) >= len(name) or len(a) < 3:
        return 0.0
    for w in said:
        if max((fuzz.ratio(w, n) / 100 for n in name), default=0.0) < WORD_MATCH:
            return 0.0
    return SUBSET_WEIGHT


PREFILTER = 400  # candidates scored in full after a fast first pass


def ranked(m: Match) -> float:
    """Match quality, less a little for deep, in-repo or part-of-a-project items."""
    return m.score - m.entry.penalty


def best(said: str, entries: list[Entry], n: int = 3) -> list[Match]:
    pool = entries
    if len(entries) > PREFILTER:
        from rapidfuzz import process

        a = norm(said)
        keys = [x.key for x in entries]
        # Two cheap scorers, so neither near spellings nor partial names are missed.
        picked = {i for _, _, i in process.extract(a, keys, scorer=fuzz.WRatio, limit=PREFILTER)}
        flat = [k.replace(" ", "") for k in keys]
        picked |= {
            i
            for _, _, i in process.extract(
                a.replace(" ", ""), flat, scorer=fuzz.ratio, limit=PREFILTER // 2
            )
        }
        pool = [entries[i] for i in picked]
    scored = [Match(x, score(said, x)) for x in pool]
    scored.sort(key=lambda m: (-ranked(m), KIND_ORDER.get(m.entry.kind, 9), len(m.entry.key)))
    out: list[Match] = []
    for m in scored:
        # One thing listed twice (your app list and the Start menu) is still one thing.
        if all(m.entry.target.casefold() != o.entry.target.casefold() for o in out):
            out.append(m)
        if len(out) == n:
            break
    return out


def _inside(child: Entry, parent: Entry) -> bool:
    if parent.kind not in ("folder", "project") or child.kind not in ("folder", "file"):
        return False
    c, p = child.target.casefold().rstrip("\\/"), parent.target.casefold().rstrip("\\/")
    return c.startswith(p + "\\") or c.startswith(p + "/")


def _decide(said: str, entries: list[Entry], threshold: float) -> tuple[Match | None, list[Match]]:
    """(sure match, guesses): a sure match is over the threshold and clearly ahead."""
    top = best(said, entries)
    if not top or top[0].score < GUESS_FLOOR:
        return None, []
    first = top[0]
    second = top[1] if len(top) > 1 else None
    lead = ranked(first) - ranked(second) if second is not None else 1.0
    # One letter is a lot in a short name: "bread" isn't "read".
    short = min(len(first.entry.key), len(norm(said))) <= SHORT_NAME
    needed = max(threshold, SHORT_THRESHOLD) if short else threshold
    if first.score >= needed:
        if lead > TIE_MARGIN:
            return first, []
        if second is not None and first.entry.kind == "app" and second.entry.kind != "app":
            return first, []  # "antigravity" the app, not an E:\Antigravity folder
        if second is not None and _inside(second.entry, first.entry):
            return first, []  # the My-study-plan folder, not my-study-plan.md inside it
        if (
            second is not None
            and first.entry.kind in ("folder", "project")
            and second.entry.kind == "file"
            and first.entry.key == second.entry.key
        ):
            return first, []  # the Tutedude folder, not Tutedude.zip (say "file" for that)
    # The existing name that's clearly closest to what you said: no need to ask.
    if not short and first.score >= CLEAR_MATCH and lead >= CLEAR_LEAD:
        return first, []
    return None, [m for m in top if m.score >= GUESS_FLOOR]


_OPEN = re.compile(
    r"^(?:open|launch|start|run|show me|bring up|pull up)\s+(?:up\s+)?(?:the\s+|my\s+)?(.+)$",
    re.I,
)
# "open X in/on/with/using Y": split at the last connector, so names with "on" or "in" survive.
_IN = re.compile(r"^(.+)\s+(?:in|on|with|using)\s+(?:the\s+)?(.+?)(?:\s+app)?$", re.I)
_EXT = re.compile(r"\.(\w{2,4})$")
_REVEAL = re.compile(
    r"^(?:show|reveal|find|locate)\s+(?:me\s+)?(?:the\s+|my\s+)?(.+?)\s+in\s+(?:the\s+)?"
    r"(?:file\s*explorer|explorer|files|folder)$|^where(?:'s| is)\s+(?:the\s+|my\s+)?(.+)$",
    re.I,
)
_APP_WORD = re.compile(r"\s+(app|application|program|browser)$", re.I)
_KIND = re.compile(
    r"\s+(folder|directory|file|project|document|pdf|video|picture|photo|image)$", re.I
)


def route(
    text: str, index: Index, *, threshold: float = THRESHOLD, now: datetime | None = None
) -> Intent | None:
    t = _clean(text)
    low = t.lower()
    if not low:
        return None
    fixed = _system(low, now)
    if fixed is not None:
        return fixed
    m = _REVEAL.match(t)
    if m:
        return _open_target((m[1] or m[2]).strip(), index, threshold, reveal=True)
    m = _OPEN.match(t)
    if not m:
        return None
    rest = m[1].strip()
    split = _IN.match(rest)
    if split:
        app, guesses = _decide(split[2], index.of("app"), threshold)
        if app is not None or guesses:
            return _open_target(split[1], index, threshold, app=app, app_guesses=guesses)
    return _open_target(rest, index, threshold)


def _open_target(
    said: str,
    index: Index,
    threshold: float,
    *,
    reveal: bool = False,
    app: Match | None = None,
    app_guesses: list[Match] | None = None,
) -> Intent | None:
    in_app = app is not None or bool(app_guesses) or reveal
    everything = index.of("folder", "project", "file") if in_app else index.entries
    # The whole phrase first: a folder called "cadence-app" or "My Project" is a name, not
    # "cadence" + the word app.
    whole, _ = _decide(said, everything, threshold)
    if whole is not None and whole.score >= WHOLE_NAME:
        target: Match | None = whole
        guesses: list[Match] = []
    else:
        kind_m = _KIND.search(said)
        name = said[: kind_m.start()].strip() if kind_m else said
        kind = kind_m[1].lower() if kind_m else None
        app_word = _APP_WORD.search(name) if not kind else None
        if app_word and name[: app_word.start()].strip():
            name, kind = name[: app_word.start()].strip(), "app"  # "brave browser" is the app
        if kind is None and _EXT.search(name):
            kind = "file"  # "node.pdf" is a file, never the Node.js app
        if kind == "app":
            pool = index.of("app")
        elif kind in ("folder", "directory"):
            pool = index.of("folder", "project")
        elif kind in ("file", "document", "pdf", "video", "picture", "photo", "image"):
            pool = index.of("file")
        else:
            pool = everything
        target, guesses = _decide(name, pool, threshold)
        said = name
    if app_guesses:  # the app is the unsure part: ask about it, then open the target in it
        if target is None:
            return None
        chosen_target = target.entry
        return _guess(
            app_guesses,
            f"open {said} in",
            lambda a: _command(chosen_target, app=a),
        )
    if target is None:
        if guesses:
            fixed_app = app.entry if app is not None else None
            return _guess(
                [g for g in guesses if not (reveal or fixed_app) or g.entry.kind != "app"]
                or guesses,
                "show" if reveal else "open",
                lambda e: _command(e, reveal=reveal, app=fixed_app),
            )
        return None
    if (reveal or app is not None) and target.entry.kind == "app":
        return None
    out = _command(target.entry, reveal=reveal, app=app.entry if app is not None else None)
    out.confidence = min(target.score, app.score) if app is not None else target.score
    return out


def _command(e: Entry, *, reveal: bool = False, app: Entry | None = None) -> Intent:
    """The tool call for opening (or showing) `e`, optionally in `app`."""
    if reveal:
        return Intent([("reveal_path", {"path": e.target})], f"Showing {e.name}.")
    if app is not None:
        return Intent(
            [("open_app", {"app": app.target, "target": e.target})],
            f"Opening {e.name} in {app.name}.",
        )
    if e.kind == "app":
        return Intent([("open_app", {"app": e.target})], f"Opening {e.name}.")
    return Intent([("open_path", {"path": e.target})], f"Opening {e.name}.")


ASK_MARGIN = TIE_MARGIN  # only guesses about as good as the best are offered


def _labels(entries: list[Entry]) -> list[str]:
    """How to say each guess so they can be told apart: an app by its real name ("Windows
    PowerShell ISE", not "powershell"), a file or folder whose name repeats by as much of its
    path as it takes ("icon in StickyBoard/assets" vs "icon in Smartmeter/assets")."""
    out = []
    for e in entries:
        if e.kind == "app":
            out.append(e.target if norm(e.target) != e.key and " " in e.target else e.name)
        elif e.kind == "file":
            out.append(Path(e.target).name)  # with its extension: "Tutedude.zip"
        else:
            out.append(e.name)
    for i, e in enumerate(entries):
        if e.kind == "app" or sum(1 for x in entries if x.key == e.key and x.kind == e.kind) < 2:
            continue
        parts = Path(e.target).parts[:-1]
        others = [
            Path(x.target).parts[:-1]
            for x in entries
            if x is not e and x.key == e.key and x.kind == e.kind
        ]
        n = 1
        for n in range(1, min(4, len(parts)) + 1):
            tail = parts[-n:]
            if all(tuple(o[-n:]) != tuple(tail) for o in others):
                break
        out[i] = f"{out[i]} in {'/'.join(p.strip(chr(92)) for p in parts[-n:])}"
    return out


def _guess(guesses: list[Match], verb: str, build: Callable[[Entry], Intent]) -> Intent:
    guesses = [g for g in guesses if ranked(g) >= ranked(guesses[0]) - ASK_MARGIN][:3]
    names = _labels([g.entry for g in guesses])
    if len(names) == 1:
        q = f"Did you mean {names[0]}?"
    else:
        q = "Did you mean " + ", ".join(names[:-1]) + f", or {names[-1]}?"
    return Intent([], "", guesses[0].score, guesses=guesses[:3], question=q, verb=verb, build=build)


def _number(text: str) -> int | None:
    m = re.search(r"(\d{1,3})\s*(?:%|percent)?", text)
    if m:
        return min(100, int(m[1]))
    words = re.findall(r"[a-z]+", text)
    total, found = 0, False
    for w in words:
        if w in NUMBERS:
            total += NUMBERS[w]
            found = True
    return min(100, total) if found else None


_SOUND = r"(?:the\s+)?(?:volume|sound)"
_UP = re.compile(rf"(?:turn\s+)?{_SOUND}\s+up|louder|turn it up|(?:increase|raise)\s+{_SOUND}")
_DOWN = re.compile(
    rf"(?:turn\s+)?{_SOUND}\s+down|quieter|softer|turn it down|(?:decrease|lower|reduce)\s+{_SOUND}"
)


def _system(low: str, now: datetime | None) -> Intent | None:
    if _UP.fullmatch(low):
        return Intent([("media", {"action": "volume_up", "times": 5})], "Volume up.")
    if _DOWN.fullmatch(low):
        return Intent([("media", {"action": "volume_down", "times": 5})], "Volume down.")
    if re.fullmatch(r"(?:un)?mute(?:\s+(?:the\s+)?(?:sound|volume|audio))?", low):
        return Intent([("media", {"action": "mute"})], "Okay.")
    m = re.fullmatch(r"(?:set\s+(?:the\s+)?)?(?:volume|sound)\s+(?:to\s+|at\s+)?(.+)", low)
    if m and (n := _number(m[1])) is not None:
        return Intent([("set_volume", {"percent": n})], f"Volume {n}.")
    m = re.fullmatch(r"(?:set\s+(?:the\s+)?)?(?:screen\s+)?brightness\s+(?:to\s+|at\s+)?(.+)", low)
    if m and (n := _number(m[1])) is not None:
        return Intent([("set_brightness", {"percent": n})], f"Brightness {n}.")
    if re.fullmatch(r"lock(?:\s+(?:the|my))?(?:\s+(?:screen|computer|pc|laptop))?", low):
        return Intent([("lock_screen", {})], "Locking.")
    if re.fullmatch(
        r"(?:take\s+(?:a\s+)?)?screenshot|screen\s*shot|capture\s+(?:the\s+)?screen", low
    ):
        return Intent([("screenshot", {})], "Screenshot saved.")
    if re.fullmatch(r"(?:play|pause|resume)(?:\s+(?:the\s+)?(?:music|song|video))?", low):
        return Intent([("media", {"action": "play_pause"})], "Okay.")
    if re.fullmatch(r"(?:next|skip)(?:\s+(?:song|track))?", low):
        return Intent([("media", {"action": "next"})], "Next.")
    if re.fullmatch(r"previous(?:\s+(?:song|track))?|go back a (?:song|track)", low):
        return Intent([("media", {"action": "previous"})], "Previous.")
    m = re.fullmatch(
        r"(minimi[sz]e|maximi[sz]e|restore|focus|switch to)\s+(?:the\s+)?(.+?)(?:\s+window)?", low
    )
    if m:
        action = {"switch to": "focus", "minimise": "minimize", "maximise": "maximize"}.get(
            m[1], m[1]
        )
        return Intent([("window", {"title": m[2], "action": action})], "Okay.")
    if re.fullmatch(r"what(?:'s| is) the time|what time is it|time", low):
        when = (now or datetime.now()).astimezone()
        return Intent([], f"It's {when:%H:%M}.")
    return None
