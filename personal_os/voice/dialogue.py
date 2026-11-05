"""Conversation: understanding answers to Donna's questions, and short-term context.

Pure functions (no audio, no model unless one is passed in), so every rule is testable.

- `understand`: your answer to "Did you mean X or Y?". You can pick one, correct it ("no, I
  wanted Zoom"), ask for something else entirely, or cancel. Only a plain no (or silence)
  cancels; an answer the rules can't place goes to the local model, if there is one.
- `yes_no`: your answer to "Shall I ...? Say yes or no." A "no, make it 5 pm instead" is an
  amendment, carried out as the next request, not a refusal that loses what you said.
- `closing`: "thanks", "that's all": the end of a conversation.
- `Conversation`: the last few exchanges, kept in memory for a few minutes so "open it" or
  "and yesterday?" make sense. Nothing here is written to disk.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from personal_os.voice import router
from personal_os.voice.index import Index

Kind = Literal["choose", "correct", "new", "cancel", "unclear"]
MAX_ASKS = 2  # "did you mean ...?" at most twice in a turn


@dataclass
class Reading:
    """How an answer was understood: a command to run (choose, correct; it may itself still
    be unsure and need asking again), a new request to handle instead, a cancel, or unclear."""

    kind: Kind
    intent: router.Intent | None = None
    text: str = ""
    by_model: bool = False


_NO_LEAD = re.compile(
    r"^(?:no+|nope|nah|not that(?: one)?|neither(?: of (?:those|them))?|none of (?:those|them)|"
    r"wrong(?: one)?|that'?s wrong|that'?s not it|forget (?:it|that)|scratch that|skip it)"
    r"\b[\s,.!]*",
    re.I,
)
_SOFT_LEAD = re.compile(r"^(?:actually|rather|sorry|um+|uh+|well)\b[\s,.]*", re.I)
_WANTED = re.compile(
    r"^(?:i\s+(?:wanted|want|meant|mean|said|asked for|was asking for|needed|need|was looking for)"
    r"|i'?m looking for|it'?s|it is|it was|make it|try)\s+(?:to\s+)?(.*)$",
    re.I,
)
# A request or a question, not a name: "what's the weather", "remind me at 5".
_REQUEST = re.compile(
    r"^(?:what|what's|whats|when|where|who|why|how|is|are|do|does|did|can|could|would|will|"
    r"should|remind|set|tell|play|pause|turn|search|find|send|brief|remember|summari[sz]e|"
    r"give|read|check|lock|mute|take|close|start|stop)\b",
    re.I,
)
_TAIL = re.compile(r"\s+(?:instead|please|then)$", re.I)
_CANCEL = re.compile(
    r"^(?:no+|nope|nah|cancel|never ?mind|forget it|stop|leave it|neither|none|nothing)"
    r"(?:\s+(?:thanks?|thank you))?[.!]*$",
    re.I,
)


def _strip(answer: str) -> tuple[str, bool, bool]:
    """(what's left, began with a no, said "I wanted ..."): "No, I meant Zoom." -> Zoom."""
    a = answer.strip().strip(".!?").strip()
    rest = _NO_LEAD.sub("", a)
    said_no = rest != a
    rest = _SOFT_LEAD.sub("", rest).strip()
    m = _WANTED.match(rest)
    wanted = m is not None
    if m:
        rest = m[1]
    rest = _TAIL.sub("", rest.strip(" ,.!?")).strip(" ,.!?")
    return rest, said_no, wanted


def _with_verb(rest: str, asked: router.Intent) -> str:
    """The correction said as a whole command: "zoom" after "Did you mean Zotero?" is
    "open zoom"; after "Did you mean the vault in ...?" (show) it's "show zoom in explorer"."""
    if asked.verb == "show":
        return f"show {rest} in explorer"
    return f"{asked.verb} {rest}"


def understand(
    answer: str,
    asked: router.Intent,
    index: Index,
    *,
    threshold: float = router.THRESHOLD,
    classify: Callable[[str, router.Intent, str], Reading | None] | None = None,
) -> Reading:
    """Your answer to `asked.question`."""
    if not answer.strip():
        return Reading("cancel")
    chosen = router.choose(answer, asked, threshold=threshold)
    if chosen is not None:
        return Reading("choose", chosen)
    if _CANCEL.match(answer.strip()):
        return Reading("cancel")
    rest, said_no, wanted = _strip(answer)
    if not rest:
        return Reading("cancel") if said_no else Reading("unclear")
    # A whole command on its own: "no, open Spotify", "show it in explorer".
    whole = router.route(rest, index, threshold=threshold)
    if whole is not None:
        return Reading("correct", whole)
    # A name, said in place of the one offered: "I wanted Zoom" -> "open Zoom".
    if said_no or wanted or len(rest.split()) <= 4:
        fixed = router.route(_with_verb(rest, asked), index, threshold=threshold)
        if fixed is not None:
            return Reading("correct", fixed)
    if classify is not None:
        read = classify(answer, asked, rest)
        if read is not None:
            return read
    if said_no or wanted:
        # A name that isn't on this laptop by any spelling the rules know: let the normal
        # path (the planner, which also searches) have it, rather than dropping it.
        name_like = len(rest.split()) <= 4 and not _REQUEST.match(rest)
        return Reading("new", text=_with_verb(rest, asked) if name_like else rest)
    if len(rest.split()) >= 3:
        return Reading("new", text=rest)  # something else entirely
    return Reading("unclear")


# --- the model, for answers the rules can't place ------------------------------------------


class AnswerReading(BaseModel):
    kind: Literal["choose", "correct", "new", "cancel"]
    choice: int | None = Field(None, description="choose: which option, 1-based")
    name: str = Field("", description="correct: the name they actually want")
    request: str = Field("", description="new: their request, in their words")


CLASSIFY_SYSTEM = """You sort a spoken answer to a clarifying question from a voice assistant.
Reply with JSON only. kinds:
- choose: they picked one of the options (give its number, 1-based)
- correct: they want a different thing than the options (give its name)
- new: they are asking for something else entirely (give the request)
- cancel: they don't want anything
If they also ask for something, it is "new", not "cancel". When they describe a thing instead of
naming it, and it isn't one of the options, give its usual name ("the maths thing": calculator).
Examples:
Question "Did you mean Zotero or Zoom Rooms?", answer "the video call one, zoom" ->
{"kind": "correct", "name": "zoom"}
Question "Did you mean Brave?", answer "hmm the maths thing" ->
{"kind": "correct", "name": "calculator"}
Question "Did you mean Brave?", answer "the one for writing code" ->
{"kind": "correct", "name": "visual studio code"}
Question "Did you mean Brave?", answer "skip it, what's the weather" ->
{"kind": "new", "request": "what's the weather"}
Question "Did you mean Brave or Bravo?", answer "the browser" -> {"kind": "choose", "choice": 1}"""


def model_classifier(
    provider: object, index: Index, *, threshold: float = router.THRESHOLD
) -> Callable[[str, router.Intent, str], Reading | None]:
    """A classifier for `understand` backed by the local model. The answer is your own speech,
    not captured content, so nothing here is tainted; it only picks how to read the answer,
    and whatever it picks still goes through the router and the action policy."""

    def classify(answer: str, asked: router.Intent, rest: str) -> Reading | None:
        options = [g.entry.name for g in asked.guesses]
        user = (
            f"Question: {asked.question}\nOptions: "
            + "; ".join(f"{i + 1}. {o}" for i, o in enumerate(options))
            + f"\nAnswer: {answer}"
        )
        try:
            got = provider.chat_json(  # type: ignore[attr-defined]
                CLASSIFY_SYSTEM, user, AnswerReading, temperature=0, max_tokens=120
            )
        except Exception:
            return None
        if got.kind == "cancel":
            return Reading("cancel", by_model=True)
        n = got.choice or 0
        if got.kind == "choose" and 1 <= n <= len(asked.guesses) and asked.build is not None:
            return Reading("choose", asked.build(asked.guesses[n - 1].entry), by_model=True)
        if got.kind == "correct" and got.name.strip():
            fixed = router.route(_with_verb(got.name.strip(), asked), index, threshold=threshold)
            if fixed is not None:
                return Reading("correct", fixed, by_model=True)
            return Reading("new", text=_with_verb(got.name.strip(), asked), by_model=True)
        if got.kind == "new":
            return Reading("new", text=got.request.strip() or rest, by_model=True)
        return None

    return classify


# --- yes or no, with amendments ------------------------------------------------------------

_YES_WORD = re.compile(
    r"\b(yes|yeah|yep|yup|sure|confirm|go ahead|do it|please do|ok(ay)?|correct|right)\b", re.I
)
_NO_WORD = re.compile(r"\b(no|nope|don'?t|do not|stop|cancel|wait|not)\b", re.I)
_YES_BUT = re.compile(r"^(?:yes|yeah|yep|ok(?:ay)?|sure)\b[\s,]*but\s+(.+)$", re.I)


def yes_no(answer: str) -> tuple[Literal["yes", "no", "amend"], str]:
    """("yes", ""), ("no", ""), or ("amend", the changed request): "no, at 5 pm instead",
    "yes but in VS Code". Only a clear yes approves, as before (ADR-066)."""
    a = answer.strip()
    m = _YES_BUT.match(a.strip(".!?"))
    if m:
        return "amend", _TAIL.sub("", m[1].strip(" .!?"))
    if _YES_WORD.search(a) and not _NO_WORD.search(a):
        return "yes", ""
    rest, said_no, wanted = _strip(a)
    if (said_no or wanted) and len(rest.split()) >= 2 and not _CANCEL.match(rest):
        return "amend", rest
    return "no", ""


# --- ending a conversation -----------------------------------------------------------------

_CLOSING = re.compile(
    r"^(?:(?:ok(?:ay)?|cool|great|perfect|nice|alright)[\s,]*)?(?:thanks?(?: you)?(?: donna)?"
    r"(?: so much)?|thank you|that'?s (?:all|it|everything)|no thanks?|nothing(?: else)?|"
    r"i'?m (?:good|done)|bye|goodbye|good night|never ?mind|ok(?:ay)?|cool|great|got it)"
    r"[.!]*$",
    re.I,
)


def closing(text: str) -> bool:
    """Is this the end of the conversation ("thanks", "that's all")?"""
    return bool(_CLOSING.match(text.strip()))


_REFERS = re.compile(
    r"\b(it|its|that|this|these|those|them|they|instead|again|same|one|other|another|there|"
    r"then|earlier|before|previous|last|also|too)\b",
    re.I,
)


def refers_back(request: str) -> bool:
    """Does the request lean on what was said before ("make it 5 pm", "open that one too")?"""
    return bool(_REFERS.search(request))


# --- the last few exchanges ----------------------------------------------------------------

CONTEXT_TTL_S = 300.0
CONTEXT_KEEP = 6
# Replies that are safe to show the action planner: they come from Donna's own commands,
# not from your captured activity (answers from `ask` do, so they're left out: ADR-050).
PLANNER_SAFE = ("fast", "do", "chat", "remember")


@dataclass
class Exchange:
    at: float
    heard: str
    reply: str
    route: str


class Conversation:
    """What was said in the last few minutes. In memory only; gone when voice restarts."""

    def __init__(self, ttl_s: float = CONTEXT_TTL_S, keep: int = CONTEXT_KEEP) -> None:
        self.ttl_s = ttl_s
        self.items: deque[Exchange] = deque(maxlen=keep)

    def add(self, heard: str, reply: str, route: str, now: float | None = None) -> None:
        if heard:
            self.items.append(
                Exchange(time.monotonic() if now is None else now, heard, reply, route)
            )

    def active(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        return bool(self.items) and now - self.items[-1].at < self.ttl_s

    def recent(self, now: float | None = None) -> list[Exchange]:
        return list(self.items) if self.active(now) else []

    def for_planner(self, now: float | None = None) -> str:
        """Earlier in this conversation, as the planner may see it: everything you said, and
        Donna's replies only where they came from her own commands."""
        lines = []
        for x in self.recent(now):
            lines.append(f"You: {x.heard}")
            if x.reply and x.route.split(" ")[0] in PLANNER_SAFE:
                lines.append(f"Donna: {x.reply[:300]}")
        return "\n".join(lines)

    def clear(self) -> None:
        self.items.clear()
