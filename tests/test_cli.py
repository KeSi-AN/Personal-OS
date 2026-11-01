from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from personal_os.interfaces.cli import app
from tests.conftest import FIXTURES

runner = CliRunner()


def run(*args: str):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_cli_status_on_empty_db(home: Path):
    out = run("status")
    assert "schema        v1" in out and "events        0" in out and "active" in out


def test_cli_pause_resume(home: Path):
    assert "paused" in run("pause", "--for", "30m")
    assert "PAUSED until" in run("status")
    run("resume")
    assert "capture       active" in run("status")
    assert (home / "runtime" / "paused").exists() is False
    out = run("events", "--source", "meta")
    assert "meta.pause" in out and "meta.resume" in out


def test_cli_sim_search_inspect_forget(home: Path):
    out = run("sim", "run", str(FIXTURES / "scenarios" / "workday.toml"))
    assert "blocked" in out
    db = str(home / "sim.db")
    assert "refresh_token" in run("search", "refresh token", "--db", db)
    out = run("inspect", "--db", db)
    assert "page_extracts" in out and "filter.blocked" in out
    out = run("forget", "--since", "2026-11-02T09:00:00+05:30", "--until", "2026-11-02T12:00:00+05:30",
              "--yes", "--db", db)  # fmt: skip
    assert "Forgot" in out
    assert "No matches." in run("search", "refresh token", "--db", db)


def test_cli_sim_refuses_real_db(home: Path):
    result = runner.invoke(
        app,
        [
            "sim",
            "run",
            str(FIXTURES / "scenarios" / "workday.toml"),
            "--db",
            str(home / "personal_os.db"),
        ],
    )
    assert result.exit_code != 0


def test_cli_export_and_replay(home: Path, tmp_path: Path):
    out_file = tmp_path / "w.jsonl"
    run("sim", "export", str(FIXTURES / "scenarios" / "workday.toml"), str(out_file))
    assert "Replayed" in run("replay", str(out_file), "--db", str(tmp_path / "r.db"))
