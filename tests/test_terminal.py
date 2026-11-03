from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from personal_os.core.events import TerminalCommand
from personal_os.observers.terminal import (
    SpoolStats,
    ingest_spool_dir,
    msys_to_windows,
    parse_record,
)


def rec(**kw):
    base = {"v": 1, "shell": "powershell", "session": "s1", "pid": 1, "history_id": 3,
            "command": "pytest -q", "cwd": None, "start": "2026-11-02T03:30:00.000Z",
            "end": "2026-11-02T03:30:12.500Z", "success": False, "exit_code": 1,
            "host": "WindowsTerminal"}  # fmt: skip
    base.update(kw)
    return base


def test_parse_powershell_record():
    e = parse_record(rec())
    assert e is not None
    p = e.payload
    assert isinstance(p, TerminalCommand)
    assert p.command == "pytest -q" and p.exit_code == 1 and p.success is False
    assert p.duration_ms == 12_500 and p.shell == "powershell" and p.host == "WindowsTerminal"
    assert (
        e.ts_end is not None
        and e.tz_offset_min == e.ts_start.astimezone().utcoffset().total_seconds() // 60
    )  # type: ignore[union-attr]


def test_parse_bash_record_with_comma_decimal_and_msys_cwd():
    e = parse_record(rec(shell="bash", start=None, end=None, start_epoch="1793590200,25",
                         end_epoch="1793590201,75", cwd="/c/Users"))  # fmt: skip
    assert e is not None and e.payload.duration_ms == 1500  # type: ignore[union-attr]
    if sys.platform == "win32":
        assert e.payload.cwd == "C:/Users"  # type: ignore[union-attr]


def test_repo_and_branch_from_cwd(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/feature/x\n")
    (tmp_path / "sub").mkdir()
    e = parse_record(rec(cwd=str(tmp_path / "sub")))
    assert e is not None
    assert e.payload.branch == "feature/x" and e.project_hint == str(tmp_path).replace("\\", "/")  # type: ignore[union-attr]


@pytest.mark.parametrize("bad", [{"command": " secret"}, {"command": ""}, {"start": "not a time"},
                                 {"start": "2026-11-02T03:30:00"}])  # fmt: skip
def test_rejects_private_and_malformed(bad):
    assert parse_record(rec(**bad)) is None


def test_editor_shell_integration_bootstrap_is_not_a_user_command():
    vscode = (
        'try { . "c:\\Users\\me\\AppData\\Local\\Programs\\Microsoft VS Code\\2c33b\\resources\\app'
        '\\out\\vs\\workbench\\contrib\\terminal\\common\\scripts\\shellIntegration.ps1" } catch {}'
    )
    assert parse_record(rec(command=vscode)) is None
    assert parse_record(rec(command="code shellIntegration-notes.md")) is not None


def test_msys_path():
    if sys.platform == "win32":
        assert msys_to_windows("/d/work/x") == "D:/work/x" and msys_to_windows("/c") == "C:/"
    assert msys_to_windows("/home/me") == "/home/me"


def test_ingest_claims_reads_and_deletes(tmp_path: Path):
    spool = tmp_path / "spool"
    spool.mkdir()
    lines = [json.dumps(rec(history_id=i, command=f"echo {i}")) for i in range(3)]
    lines.insert(1, "{not json")
    lines.append(json.dumps(rec(command=" private")))
    (spool / "powershell-1.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    # a leftover claimed file from a crashed daemon is picked up too
    (spool / "bash-9.jsonl.123.ingesting").write_text(json.dumps(rec(command="leftover")) + "\n")
    stats = SpoolStats()
    events = ingest_spool_dir(spool, stats)
    assert sorted(e.payload.command for e in events) == ["echo 0", "echo 1", "echo 2", "leftover"]  # type: ignore[union-attr]
    assert stats.malformed == 1 and stats.private_skipped == 1
    assert list(spool.iterdir()) == []


def test_ingest_tolerates_bom_and_missing_dir(tmp_path: Path):
    assert ingest_spool_dir(tmp_path / "nope", SpoolStats()) == []
    spool = tmp_path / "s"
    spool.mkdir()
    (spool / "powershell-2.jsonl").write_bytes(b"\xef\xbb\xbf" + json.dumps(rec()).encode() + b"\n")
    assert len(ingest_spool_dir(spool, SpoolStats())) == 1
