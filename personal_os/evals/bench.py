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
    heard: str = ""  # spoken mode: what speech recognition made of it

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


def make_home(real: Config, into: Path, *, keep_on_gpu: bool = False) -> Config:
    """A copy of your settings and database to run against; your own are only read.
    `keep_on_gpu`: the copy keeps the model loaded and reads the prompt ahead, as Donna does
    with the tray's "Keep on GPU", whatever your setting is."""
    into.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_path(real.data_dir), config_path(into))
    text = config_path(into).read_text("utf-8")
    # The copy keeps its data next to itself, whatever the original says.
    lines = ['data_dir = ""' if ln.startswith("data_dir") else ln for ln in text.splitlines()]
    config_path(into).write_text("\n".join(lines) + "\n", "utf-8")
    cfg = load_config(into)
    if keep_on_gpu:
        llm = cfg.llm.model_copy(update={"keep_on_gpu": True})
        cfg = cfg.model_copy(update={"llm": llm})
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    if real.db_path.exists():
        copy_db(real.db_path, cfg.db_path)
    return cfg


class Bench:
    """One Voice, reused across cases as Donna reuses hers, in a pretend world."""

    def __init__(
        self, cfg: Config, apps: list[Any], world_ref: list[World], *, prime: bool = True,
        busy: bool = False, spoken_from: Path | None = None,
    ) -> None:  # fmt: skip
        from personal_os.voice.main import Voice

        self.cfg, self.apps, self.prime, self.world_ref = cfg, apps, prime, world_ref
        self.busy = busy
        # Spoken mode: your data folder, for the Piper voice and the speech models (read only).
        self.spoken_from = spoken_from
        self.spoken = spoken_from is not None
        self.audio: Any = None  # the next utterance "heard" (spoken mode)
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

        def listen(*_a: Any, **_k: Any) -> str:
            # Spoken mode: the request, as speech, through Donna's own speech recognition.
            audio, self.audio = self.audio, None
            if audio is None:
                return ""
            v.last_audio = audio
            return str(v.transcribe(audio))

        v.listen = listen  # type: ignore[method-assign]
        if spoken_from is not None:
            from personal_os.voice.stt import make_transcriber

            vc = cfg.voice
            v.stt = make_transcriber(spoken_from, vc.stt_model)
            fast = make_transcriber(spoken_from, vc.stt_fast_model or vc.stt_model)
            v.stt_fast = fast if vc.stt_fast_model and fast.available else v.stt
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

    def background_job(self) -> None:
        """What the daemon does while you're away: a session summary, with its own prompt
        (about 2,000 tokens), on the same model. Before Ollama kept several read prompts, this
        made the next request read Donna's again (about 30 s)."""
        from personal_os.context.summarizer import SYSTEM, SessionSummary

        provider = self.voice.provider()
        filler = "\n".join(f"[{i}] window: Editor - notes {i}.md (2m)" for i in range(160))
        user = f"Session records:\n<data>\n{filler}\n</data>"
        with contextlib.suppress(Exception):  # the answer doesn't matter, only the reading
            provider.chat_json(SYSTEM, user, SessionSummary, max_tokens=16)

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
        if self.busy:
            self.background_job()
        t0 = time.monotonic()
        if self.spoken:
            assert self.spoken_from is not None
            self.audio = speech(case.say, self.spoken_from / "voice" / "piper", self.cfg)
            v.respond("hotkey")
        else:
            v.respond("typed", case.say)
        took = time.monotonic() - t0
        entry = last_entry(rt)
        heard = str(entry.get("heard") or "")
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
            heard if self.spoken else "",
        )  # fmt: skip


_VOICES: dict[str, Any] = {}


def speech(text: str, voice_dir: Path, cfg: Config) -> Any:
    """`text` spoken by the local Piper voice, as 16 kHz mono float32 (what the mic gives
    speech recognition). A clean synthetic voice: it finds names Whisper mishears, not what
    your own voice, accent or room do to it."""
    import wave

    import numpy as np

    from personal_os.voice.tts import PiperVoice

    piper = PiperVoice(voice_dir, cfg.voice.piper_voice)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "say.wav"
        piper.synthesize(text, out)
        with wave.open(str(out), "rb") as w:
            rate, frames = w.getframerate(), w.readframes(w.getnframes())
    pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    n = int(len(pcm) * 16000 / rate)
    resampled = np.interp(np.linspace(0, len(pcm) - 1, n), np.arange(len(pcm)), pcm)
    pad = np.zeros(4000, dtype=np.float32)  # a quarter second of quiet either side
    return np.concatenate([pad, resampled.astype(np.float32), pad])


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
    if r.heard and r.heard.strip(" .").lower() != r.said.strip(" .?").lower():
        out += f"\n        heard: {r.heard!r}"
    for c in r.failed:
        out += f"\n        x {c.name}" + (f": {c.detail}" if c.detail else "")
    return out


def summary(results: list[Result], model: str, primed: bool, busy_: bool = False) -> str:
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
    busy = ", a background summary before each case" if busy_ else ""
    head = (
        f"## {when}: {count('pass')}/{n} pass, {count('slow')} slow, {count('fail')} wrong\n\n"
        f"Model {model}, prompt {'read ahead' if primed else 'not read ahead'}{busy}. Median "
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


def release(real: Config) -> None:
    """Let Ollama unload the model on your own setting's timer again."""
    from personal_os.llm import make_provider

    with contextlib.suppress(Exception):
        make_provider(real.llm).warm(keep_alive=real.llm.keep_alive)


def main(argv: list[str] | None = None, *, emit: Callable[[str], None] = print) -> int:
    ap = argparse.ArgumentParser(prog="bench", description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", nargs="*", default=[], help="case ids or tags containing these")
    ap.add_argument("--cold", action="store_true", help="don't read the prompt ahead")
    ap.add_argument("--out", type=Path, help="append the summary to this Markdown file")
    ap.add_argument("--cases", type=Path, help="another cases file")
    ap.add_argument(
        "--keep-on-gpu",
        action="store_true",
        help="read the prompt ahead as with Keep on GPU, whatever your setting",
    )
    ap.add_argument(
        "--spoken",
        action="store_true",
        help="say each request with the Piper voice and hear it with Whisper",
    )
    ap.add_argument("--busy", action="store_true",
                    help="run a background summary before each case (daemon work)")  # fmt: skip
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
        cfg = make_home(real, tmp, keep_on_gpu=args.keep_on_gpu)
        emit(f"{len(cases)} cases, model {cfg.llm.fast_model}, copy of your data in {tmp}")
        world_ref: list[World] = [World(apps)]
        with patched(world_ref):
            bench = Bench(cfg, apps, world_ref, prime=not args.cold, busy=args.busy,
                          spoken_from=real.data_dir if args.spoken else None)  # fmt: skip
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
    # Donna reads her prompt ahead only with llm.keep_on_gpu (the tray's "Keep on GPU"):
    # without it, the bench can't either, and says so (20 Nov: a run looked slow for this).
    primed = not args.cold and (real.llm.keep_on_gpu or args.keep_on_gpu)
    if not args.cold and not primed:
        emit("Note: llm.keep_on_gpu is off, so the prompt wasn't read ahead (as for Donna).")
    if args.keep_on_gpu and not real.llm.keep_on_gpu:
        release(real)  # the copy pinned the model; your setting doesn't
    text = summary(results, real.llm.fast_model, primed, args.busy)
    emit("\n" + text)
    before = save(results, real.data_dir / "bench")
    for c in changes(before, results):
        emit(c)
    if not args.only and not args.cases:  # a full run: what the pre-push gate checks
        from personal_os.evals import gate

        flags = [f for f, on in (("spoken", args.spoken), ("busy", args.busy),
                                 ("cold", args.cold)) if on]  # fmt: skip
        passed = sum(r.verdict == "pass" for r in results)
        gate.record(real.data_dir / "bench", passed=passed, total=len(results), flags=flags)
    if args.out:
        with args.out.open("a", encoding="utf-8") as f:
            f.write("\n" + text)
    return 0 if all(r.verdict == "pass" for r in results) else 2


if __name__ == "__main__":
    sys.exit(main())
