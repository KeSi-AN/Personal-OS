"""The live bench's own parts: its cases file, scoring, the pretend world, and one rules-only
run through the real turn code (no model needed for these)."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_os.actions.apps import InstalledApp
from personal_os.actions.tools import build_registry
from personal_os.config.schema import Config
from personal_os.evals import bench as bench_mod
from personal_os.evals.cases import Case, Run, load, matches, score, verdict
from personal_os.evals.world import Call, World, copy_db, patched


def test_the_shipped_cases_are_valid():
    cases = load()
    assert len(cases) >= 30
    tools = {t.name for t in build_registry().all()}
    for c in cases:
        assert c.say.strip(), c.id
        for spec in c.calls:
            assert spec["tool"] in tools, (c.id, spec)
        for t in c.no_calls:
            assert t in tools, (c.id, t)


def test_a_call_matches_by_tool_and_argument_patterns():
    call = Call("set_reminder", {"text": "check Claude", "minutes": 5}, "succeeded", "ok")
    assert matches({"tool": "set_reminder"}, call)
    assert matches({"tool": "set_reminder", "minutes": "^5$", "text": "check claude"}, call)
    assert not matches({"tool": "set_reminder", "minutes": "^15$"}, call)
    assert not matches({"tool": "set_reminder", "at": "5"}, call)  # never given
    assert not matches({"tool": "take_rest"}, call)


def run_of(reply: str, *calls: Call, s: float = 1.0, effects: list[str] | None = None) -> Run:
    return Run("do", reply, s, list(calls), effects or [], [])


def test_scoring_right_wrong_and_slow():
    case = Case("c", "pause music", calls=[{"tool": "music", "action": "pause"}], only=True,
                no_effects=["google"], max_s=5)  # fmt: skip
    good = Call("music", {"action": "pause"}, "succeeded", "Paused YouTube Music.")
    assert verdict(score(case, run_of("Paused.", good))) == "pass"
    assert verdict(score(case, run_of("Paused.", good, s=9))) == "slow"
    failed = Call("music", {"action": "pause"}, "failed", "no player")
    assert verdict(score(case, run_of("Paused.", failed))) == "fail"  # must succeed
    extra = Call("browser_open", {"query": "youtube music"}, "succeeded", "ok")
    checks = score(case, run_of("Paused.", good, extra, effects=["browser tabs.open google"]))
    assert {c.name for c in checks if not c.ok} == {"nothing else", "no effect ~google"}


def test_every_case_is_held_to_the_failures_in_the_log():
    case = Case("c", "wadup")
    names = lambda r: {c.name for c in score(case, r) if not c.ok}  # noqa: E731
    assert names(run_of("Here's what's overdue: [list of overdue items].")) == {"no placeholder"}
    assert names(run_of("")) == {"answers"}
    crashed = Run("ask", "Sorry.", 1.0, [], [], [], error="TypeError: x")
    assert names(crashed) == {"no crash"}


def test_the_pretend_browser_plays_and_pauses_its_music_tab():
    w = World([])
    assert w.browser("media", {"action": "status"})["playing"] is True
    assert w.browser("media", {"action": "pause", "tabId": 2})["playing"] is False
    w.browser("tabs.open", {"url": "https://music.youtube.com/search?q=x"})
    assert len(w.tabs) == 4
    assert w.effects[1] == 'browser media {"action": "pause", "tabId": 2}'


def test_regressions_are_named(tmp_path: Path):
    r = bench_mod.Result("a", "x", "pass", "fast", "ok", 1.0, [], [], [], [], [])
    assert bench_mod.save([r], tmp_path) == {}
    r2 = bench_mod.Result("a", "x", "fail", "do", "no", 9.0, [], [], [], [], [])
    before = bench_mod.save([r2], tmp_path)
    assert bench_mod.changes(before, [r2]) == ["WORSE: a pass -> fail"]


def test_the_database_copy_is_a_full_copy(tmp_path: Path):
    import sqlite3

    src = tmp_path / "a.db"
    with sqlite3.connect(src) as c:
        c.execute("create table t (x)")
        c.execute("insert into t values (1)")
    copy_db(src, tmp_path / "copy" / "b.db")
    with sqlite3.connect(tmp_path / "copy" / "b.db") as c:
        assert c.execute("select x from t").fetchall() == [(1,)]


YTM = InstalledApp("YouTube Music", "Brave._crx_cinhimbnkkghhklpknlkffjgod", "chrome_proxy.exe")


@pytest.mark.parametrize(
    ("case", "verdict_is"),
    [
        (Case("hi", "hi donna", only=True, max_s=3), "pass"),
        (Case("rest", "take 5", calls=[{"tool": "take_rest", "minutes": "^5$"}], only=True),
         "pass"),
        (Case("remind", "remind me in 5 to check claude",
              calls=[{"tool": "set_reminder", "minutes": "^5$", "text": "check claude"}],
              only=True, asks=False), "pass"),
        (Case("ask", "remind me in a minute", answers=["to stretch"], asks=True,
              calls=[{"tool": "set_reminder", "minutes": "^1$", "text": "stretch"}]), "pass"),
        (Case("pause", "pause music", calls=[{"tool": "music", "action": "pause"}],
              effects=['media .*"pause"'], only=True), "pass"),
    ],
)  # fmt: skip
def test_rules_only_cases_run_through_the_real_turn_code(
    config: Config, case: Case, verdict_is: str
):
    cfg = config.model_copy(
        update={"llm": config.llm.model_copy(update={"enabled": False}),
                "music": config.music.model_copy(update={"source": "youtube_music"})}
    )  # fmt: skip
    cfg.runtime_dir.mkdir(parents=True, exist_ok=True)
    ref = [World([YTM])]
    with patched(ref):
        b = bench_mod.Bench(cfg, [YTM], ref, prime=False)
        result = b.run(case)
    assert result.verdict == verdict_is, (result.reply, [c for c in result.failed])
