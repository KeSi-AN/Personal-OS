"""Run the live bench: every case through the real turn code and the real model.

    uv run python -m personal_os.evals.bench                  # all cases
    uv run python -m personal_os.evals.bench --only music     # ids or tags containing "music"
    uv run python -m personal_os.evals.bench --out docs/BENCH.md
    uv run python -m personal_os.evals.bench --cold           # no prompt read ahead (worst case)

Each case starts with the agent's prompt already read (as Donna keeps it while idle with
llm.keep_on_gpu), unless --cold. Results go to <data dir>/bench/: one JSON per run and
latest.json, and what changed since the previous run is printed (a case that passed and now
doesn't is a regression). The bench shares Ollama with a running Donna: her prompt is read
again after it, which takes about 30 s on the GTX 1650.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from personal_os.config.loader import ENV_HOME, config_path, default_home, load_config
from personal_os.config.schema import Config
from personal_os.evals.cases import Case, Check, Run, load, score, verdict
from personal_os.evals.world import Call, World, copy_db, default_tabs, patched, read_apps


@dataclass
class Result:
    id: str
    said: str
    verdict: str
    route: str
    reply: str
    total_s: float
    checks: list[Check]
    calls: list[str]
    effects: list[str]
    asked: list[str]
    trace: list[str]
    error: str = ""

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


def make_home(real: Config, into: Path) -> Config:
    """A copy of your settings and database to run against; your own are only read."""
    into.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_path(real.data_dir), config_path(into))
    text = config_path(into).read_text("utf-8")
    # The copy keeps its data next to itself, whatever the original says.
    lines = ['data_dir = ""' if ln.startswith("data_dir") else ln for ln in text.splitlines()]
    config_path(into).write_text("\n".join(lines) + "\n", "utf-8")
    cfg = load_config(into)
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    if real.db_path.exists():
        copy_db(real.db_path, cfg.db_path)
    return cfg


class Bench:
    """One Voice, reused across cases as Donna reuses hers, in a pretend world."""

    def __init__(
        self, cfg: Config, apps: list[Any], world_ref: list[World], *, prime: bool = True
    ) -> None:
        from personal_os.voice.main import Voice

        self.cfg, self.apps, self.prime, self.world_ref = cfg, apps, prime, world_ref
        self.voice = v = Voice(cfg)

        def say(text: str) -> None:
            self.world_ref[0].spoken.append(text)

        def ask(question: str, *, shown: str | None = None) -> str:
            return self.world_ref[0].ask(question)

        def nothing(*_a: Any, **_k: Any) -> None:
            return None

        rest_now = v.take_rest

        def take_rest(minutes: int, *, announce: bool = False) -> str:
            # "take 5" by rules skips the executor: kept as a call all the same.
            said = rest_now(minutes, announce=announce)
            args = {"minutes": minutes, "tell_when_back": announce}
            self.world_ref[0].calls.append(Call("take_rest", args, "succeeded", said))
            return said

        v.take_rest = take_rest  # type: ignore[method-assign]
        # Nothing is heard, said or shown: the turn's words go to the world instead.
        for name, fn in [("say", say), ("ask_aloud", ask), ("beat", nothing),
                         ("wait_for_popup", nothing), ("launch_popup", nothing),
                         ("warm_stt", nothing)]:  # fmt: skip
            setattr(v, name, fn)
        setattr(v.speaker, "earcon", nothing)  # noqa: B010

    def read_prompt_ahead(self) -> None:
        v = self.voice
        v.prime_agent()
        with v._priming:  # held by the priming thread until it's done
            pass

    def run(self, case: Case) -> Result:
        from personal_os.voice import rest

        v, rt = self.voice, self.cfg.runtime_dir
        rest.end_rest(rt)
        v.convo.clear()
        v.next_request = None
        world = World(self.apps, tabs=default_tabs(case.playing), answers=list(case.answers))
        self.world_ref[0] = world
        world.write_tabs(rt)
        for earlier in case.before:
            v.respond("typed", earlier)
        world.calls.clear()
        world.effects.clear()
        world.desktop.calls.clear()
        world.asked.clear()
        if self.prime:
            self.read_prompt_ahead()
        t0 = time.monotonic()
        v.respond("typed", case.say)
        took = time.monotonic() - t0
        entry = last_entry(rt)
        run = Run(
            route=str(entry.get("route", "")),
            reply=str(entry.get("reply") or ""),
            total_s=round(float(entry.get("handle_s", took)), 1),  # not counting speech
            calls=list(world.calls),
            effects=world.all_effects(),
            asked=list(world.asked),
            error=str(entry.get("error", "")),
            trace=[str(t) for t in entry.get("trace", [])],
        )
        checks = score(case, run)
        return Result(
            case.id, case.say, verdict(checks), run.route, run.reply, run.total_s, checks,
            [c.render() for c in run.calls], run.effects, run.asked, run.trace, run.error,
        )  # fmt: skip


def last_entry(runtime_dir: Path) -> dict[str, Any]:
    from personal_os.voice.main import HISTORY

    path = runtime_dir / HISTORY
    try:
        lines = path.read_text("utf-8").splitlines()
        return dict(json.loads(lines[-1])) if lines else {}
    except (OSError, ValueError):
        return {}


def selected(cases: list[Case], only: list[str]) -> list[Case]:
    if not only:
        return cases
    return [c for c in cases if any(o in c.id or o in c.tags for o in only)]


# --- reporting -------------------------------------------------------------------------------


def line(r: Result) -> str:
    mark = {"pass": "PASS", "slow": "SLOW", "fail": "FAIL"}[r.verdict]
    out = f"{mark}  {r.id:<28} {r.total_s:>5.1f} s  {r.route:<10} {r.reply[:70]!r}"
    for c in r.failed:
        out += f"\n        x {c.name}" + (f": {c.detail}" if c.detail else "")
    return out


def summary(results: list[Result], model: str, primed: bool) -> str:
    n = len(results)

    def count(v: str) -> int:
        return sum(r.verdict == v for r in results)

    times = sorted(r.total_s for r in results)
    p90 = times[min(n - 1, int(0.9 * n))] if n else 0.0
    tags: dict[str, list[Result]] = {}
    for r in results:
        for t in r.id.split("-")[:1]:
            tags.setdefault(t, []).append(r)
    rows = [
        f"| {t} | {sum(x.verdict == 'pass' for x in rs)}/{len(rs)} | "
        f"{statistics.median(x.total_s for x in rs):.1f} s |"
        for t, rs in sorted(tags.items())
    ]
    when = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    head = (
        f"## {when}: {count('pass')}/{n} pass, {count('slow')} slow, {count('fail')} wrong\n\n"
        f"Model {model}, prompt {'read ahead' if primed else 'cold'}. Median "
        f"{statistics.median(times) if times else 0:.1f} s, 90th percentile {p90:.1f} s.\n\n"
        "| group | right and in time | median |\n|---|---|---|\n" + "\n".join(rows) + "\n"
    )
    bad = [r for r in results if r.verdict != "pass"]
    if bad:
        head += (
            "\nNot passing:\n\n"
            + "\n".join(
                f"- `{r.id}` ({r.verdict}, {r.total_s:.1f} s): "
                + "; ".join(c.name + (f" ({c.detail[:90]})" if c.detail else "") for c in r.failed)
                for r in bad
            )
            + "\n"
        )
    return head


def changes(before: dict[str, str], now: list[Result]) -> list[str]:
    out = []
    for r in now:
        was = before.get(r.id)
        if was and was != r.verdict:
            word = "better" if r.verdict == "pass" else "WORSE" if was == "pass" else "changed"
            out.append(f"{word}: {r.id} {was} -> {r.verdict}")
    return out


def save(results: list[Result], out_dir: Path) -> dict[str, str]:
    """Write this run; return the previous run's verdicts by case id."""
    out_dir.mkdir(parents=True, exist_ok=True)
    latest = out_dir / "latest.json"
    before: dict[str, str] = {}
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
        before = {r["id"]: r["verdict"] for r in json.loads(latest.read_text("utf-8"))}
    data = json.dumps([asdict(r) for r in results], indent=1, ensure_ascii=False)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"run-{stamp}.json").write_text(data, "utf-8")
    latest.write_text(data, "utf-8")
    return before


def main(argv: list[str] | None = None, *, emit: Callable[[str], None] = print) -> int:
    ap = argparse.ArgumentParser(prog="bench", description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", nargs="*", default=[], help="case ids or tags containing these")
    ap.add_argument("--cold", action="store_true", help="don't read the prompt ahead")
    ap.add_argument("--out", type=Path, help="append the summary to this Markdown file")
    ap.add_argument("--cases", type=Path, help="another cases file")
    args = ap.parse_args(argv)

    real = load_config(default_home(), create=False)
    cases = selected(load(args.cases) if args.cases else load(), args.only)
    if not cases:
        emit("No cases match.")
        return 1
    apps = read_apps(real.runtime_dir / "apps.json")
    tmp = Path(tempfile.mkdtemp(prefix="pos-bench-"))
    old_home = os.environ.get(ENV_HOME)
    os.environ[ENV_HOME] = str(tmp)  # anything that looks up the data folder gets the copy
    results: list[Result] = []
    try:
        cfg = make_home(real, tmp)
        emit(f"{len(cases)} cases, model {cfg.llm.fast_model}, copy of your data in {tmp}")
        world_ref: list[World] = [World(apps)]
        with patched(world_ref):
            bench = Bench(cfg, apps, world_ref, prime=not args.cold)
            bench.voice.index.refresh(wait=True)
            for case in cases:
                r = bench.run(case)
                results.append(r)
                emit(line(r))
    finally:
        if old_home is None:
            os.environ.pop(ENV_HOME, None)
        else:
            os.environ[ENV_HOME] = old_home
        shutil.rmtree(tmp, ignore_errors=True)
    text = summary(results, real.llm.fast_model, not args.cold)
    emit("\n" + text)
    before = save(results, real.data_dir / "bench")
    for c in changes(before, results):
        emit(c)
    if args.out:
        with args.out.open("a", encoding="utf-8") as f:
            f.write("\n" + text)
    return 0 if all(r.verdict == "pass" for r in results) else 2


if __name__ == "__main__":
    sys.exit(main())
