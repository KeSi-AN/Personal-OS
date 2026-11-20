"""Phase 4: the bench gate before pushing, and the split functions keep their behaviour (the
whole suite covers that; here, the gate)."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_os.evals import gate


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "personal_os" / "actions").mkdir(parents=True)
    (root / "personal_os" / "actions" / "agent.py").write_text("RULES = 1\n", "utf-8")
    (root / "personal_os" / "other.py").write_text("x = 1\n", "utf-8")
    return root


def test_the_fingerprint_follows_only_the_watched_files(repo: Path):
    first = gate.fingerprint(repo)
    (repo / "personal_os" / "other.py").write_text("x = 2\n", "utf-8")
    assert gate.fingerprint(repo) == first  # not something that decides what Donna does
    (repo / "personal_os" / "actions" / "agent.py").write_text("RULES = 2\n", "utf-8")
    assert gate.fingerprint(repo) != first
    (repo / "personal_os" / "actions" / "agent.py").write_text("RULES = 2\r\n", "utf-8")
    assert gate.fingerprint(repo) == gate.fingerprint(repo)  # line endings don't count


def test_the_gate_wants_a_recent_passing_run(repo: Path, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch):  # fmt: skip
    data = tmp_path / "data"
    (data / "bench").mkdir(parents=True)
    monkeypatch.setattr(gate, "repo_root", lambda: repo)
    assert gate.check(data, repo)[0] is False  # never run

    gate.record(data / "bench", passed=36, total=37, flags=[])
    ok, why = gate.check(data, repo)
    assert not ok and "36 of 37" in why

    gate.record(data / "bench", passed=37, total=37, flags=[])
    assert gate.check(data, repo)[0] is True

    (repo / "personal_os" / "actions" / "agent.py").write_text("RULES = 3\n", "utf-8")
    ok, why = gate.check(data, repo)
    assert not ok and "changed since the last full bench run" in why


def test_the_hook_never_replaces_someone_elses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location(
        "install_git_hooks", Path(__file__).parents[1] / "scripts" / "install_git_hooks.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    hooks = tmp_path / ".git" / "hooks"
    hooks.mkdir(parents=True)
    fake_script = tmp_path / "scripts" / "install_git_hooks.py"
    fake_script.parent.mkdir()
    monkeypatch.setattr(mod, "__file__", str(fake_script))
    monkeypatch.setattr(sys, "argv", ["x"])
    (hooks / "pre-push").write_text("#!/bin/sh\necho mine\n", "utf-8")
    assert mod.main() == 1 and "mine" in (hooks / "pre-push").read_text("utf-8")
    (hooks / "pre-push").unlink()
    assert mod.main() == 0 and mod.MARK in (hooks / "pre-push").read_text("utf-8")
    monkeypatch.setattr(sys, "argv", ["x", "--remove"])
    assert mod.main() == 0 and not (hooks / "pre-push").exists()


@pytest.mark.parametrize(
    ("said", "minutes"),
    [("Remind me in five to check Claude.", 5), ("remind me in twenty to stretch", 20),
     ("remind me in 5 to check claude", 5)],
)  # fmt: skip
def test_a_spoken_number_after_in_is_minutes(said: str, minutes: int):
    """Whisper writes "five", not "5" (found by the bench's spoken run, 21 Nov)."""
    from personal_os.actions import reminder_parse

    r = reminder_parse.parse(said)
    assert r is not None and r.minutes == minutes and r.missing is None


def test_the_wake_phrase_list():
    import json

    from personal_os.voice.wake import STOP_PHRASES, grammar

    words = json.loads(grammar(["Donna", "jarvis"]))
    assert {"donna", "hey donna", "okay jarvis", "stop talking", "dawn", "[unk]"} <= set(words)
    assert words[-1] == "[unk]" and len(words) == len(set(words))
    assert all(p in words for p in STOP_PHRASES)


def test_the_wake_phrase_list_is_off_unless_chosen(config):
    assert config.voice.wake_grammar is False
