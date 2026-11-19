"""Bench cases and how a run of one is scored. No model, no I/O beyond reading the file.

A case is a request you really made (cases.toml says when and what went wrong) and what a
good answer does:

    [[case]]
    id = "close-brave"
    say = "close brave"
    from = "17 Nov, wake: 82 s and no reply"
    calls = [{ tool = "close_app", name = "brave" }]   # each must run, and succeed
    only = true                                        # and nothing else may run
    reply = ["brave"]                                  # said back (regex, any case)
    max_s = 10

Every case is also checked for the failures seen in the log whatever it expects: an empty
reply, a placeholder ("[list of overdue items]"), a crash.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from personal_os.evals.world import Call

CASES = Path(__file__).with_name("cases.toml")
_PLACEHOLDER = re.compile(r"\[(?:list|insert|name|time|song|title|your|the)\b[^\]]*\]", re.I)


@dataclass
class Case:
    id: str
    say: str
    origin: str = ""  # when you said it and what went wrong then
    before: list[str] = field(default_factory=list)  # said first, not scored ("remind me ...")
    answers: list[str] = field(default_factory=list)  # typed answers to her questions
    playing: bool = True  # YouTube Music is open in a tab and playing
    calls: list[dict[str, Any]] = field(default_factory=list)
    only: bool = False
    no_calls: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)
    no_effects: list[str] = field(default_factory=list)
    reply: list[str] = field(default_factory=list)
    no_reply: list[str] = field(default_factory=list)
    asks: bool | None = None  # True: must ask something; False: must not
    silent: bool = False  # no reply is right ("stop")
    max_s: float = 15.0
    tags: list[str] = field(default_factory=list)


def load(path: Path = CASES) -> list[Case]:
    raw = tomllib.loads(path.read_text("utf-8"))
    out = []
    for c in raw.get("case", []):
        c = dict(c)
        c["origin"] = c.pop("from", "")
        out.append(Case(**c))
    ids = [c.id for c in out]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        raise ValueError(f"case ids used twice: {sorted(dupes)}")
    return out


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Run:
    """What one case did."""

    route: str
    reply: str
    total_s: float
    calls: list[Call]
    effects: list[str]
    asked: list[str]
    error: str = ""
    trace: list[str] = field(default_factory=list)


def matches(spec: dict[str, Any], call: Call) -> bool:
    """`spec` names a tool and, optionally, patterns its arguments must contain."""
    if call.tool != spec.get("tool"):
        return False
    for key, pattern in spec.items():
        if key == "tool":
            continue
        if key not in call.args or not re.search(str(pattern), str(call.args[key]), re.I):
            return False
    return True


def _fmt(spec: dict[str, Any]) -> str:
    args = ", ".join(f"{k}~{v}" for k, v in spec.items() if k != "tool")
    return f"{spec.get('tool')}({args})"


def score(case: Case, run: Run) -> list[Check]:
    checks: list[Check] = []

    def add(name: str, ok: object, detail: str = "") -> None:
        checks.append(Check(name, bool(ok), detail))

    add("no crash", not run.error, run.error)
    if case.silent:
        add("says nothing", not run.reply.strip(), run.reply[:80])
    else:
        add("answers", bool(run.reply.strip()), "empty reply")
    ph = _PLACEHOLDER.search(run.reply)
    add("no placeholder", ph is None, ph[0] if ph else "")

    for spec in case.calls:
        hit = [c for c in run.calls if matches(spec, c)]
        ok = any(c.status == "succeeded" for c in hit)
        seen = "; ".join(c.render() for c in hit) or "not called"
        add(f"does {_fmt(spec)}", ok, "" if ok else seen)
    if case.only:
        extra = [c for c in run.calls if not any(matches(s, c) for s in case.calls)]
        add("nothing else", not extra, "; ".join(c.render() for c in extra))
    for tool in case.no_calls:
        bad = [c for c in run.calls if c.tool == tool]
        add(f"no {tool}", not bad, "; ".join(c.render() for c in bad))
    for pattern in case.effects:
        add(f"effect ~{pattern}", any(re.search(pattern, e, re.I) for e in run.effects))
    for pattern in case.no_effects:
        bad_e = [e for e in run.effects if re.search(pattern, e, re.I)]
        add(f"no effect ~{pattern}", not bad_e, "; ".join(bad_e)[:200])
    for pattern in case.reply:
        add(f"reply ~{pattern}", re.search(pattern, run.reply, re.I), run.reply[:120])
    for pattern in case.no_reply:
        add(f"reply !~{pattern}", not re.search(pattern, run.reply, re.I), run.reply[:120])
    if case.asks is not None:
        add("asks" if case.asks else "doesn't ask", bool(run.asked) == case.asks,
            "; ".join(run.asked)[:160])  # fmt: skip
    add(f"within {case.max_s:g} s", run.total_s <= case.max_s, f"{run.total_s:.1f} s")
    return checks


def verdict(checks: list[Check]) -> str:
    """pass; slow (right, but over its time); fail (wrong)."""
    failed = [c for c in checks if not c.ok]
    if not failed:
        return "pass"
    if all(c.name.startswith("within ") for c in failed):
        return "slow"
    return "fail"
