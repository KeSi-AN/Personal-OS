"""Shell hook installer (always) and the hooks themselves in real shells (`-m live`)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from personal_os.hooks import installer
from personal_os.observers.terminal import SpoolStats, ingest_spool_dir

# --- installer -----------------------------------------------------------------------------


@pytest.fixture
def no_policy_check(monkeypatch):
    monkeypatch.setattr(installer, "execution_policy", lambda: "RemoteSigned")


def test_install_is_idempotent_reversible_and_keeps_user_content(tmp_path, no_policy_check):
    profile = tmp_path / "profile.ps1"
    original = "Set-Alias ll Get-ChildItem\r\nfunction prompt { 'my> ' }\r\n"
    profile.write_bytes(b"\xef\xbb\xbf" + original.encode())
    kw = dict(
        hooks_dir=tmp_path / "hooks",
        spool_dir=tmp_path / "spool",
        pause_marker=tmp_path / "paused",
        profile=profile,
    )
    installer.install("powershell", **kw)
    installer.install("powershell", **kw)  # second run replaces, never duplicates
    text = profile.read_bytes()
    assert text.startswith(b"\xef\xbb\xbf")  # encoding preserved
    decoded = text.decode("utf-8-sig")
    assert decoded.count(installer.BEGIN) == 1
    assert "Set-Alias ll Get-ChildItem" in decoded
    assert "\r\n" in decoded
    assert (tmp_path / "hooks" / "powershell.ps1").exists()
    assert (tmp_path / "profile.ps1.personal-os.bak").read_bytes().decode("utf-8-sig") == original
    assert installer.status("powershell", hooks_dir=tmp_path / "hooks", profile=profile).installed

    installer.uninstall("powershell", hooks_dir=tmp_path / "hooks", profile=profile)
    after = profile.read_bytes().decode("utf-8-sig")
    assert installer.BEGIN not in after
    assert after.replace("\r\n", "\n").strip() == original.replace("\r\n", "\n").strip()
    assert not (tmp_path / "hooks" / "powershell.ps1").exists()


def test_install_creates_missing_profile_and_dry_run_writes_nothing(tmp_path, no_policy_check):
    profile = tmp_path / "WindowsPowerShell" / "profile.ps1"
    kw = dict(
        hooks_dir=tmp_path / "hooks",
        spool_dir=tmp_path / "spool",
        pause_marker=tmp_path / "paused",
        profile=profile,
    )
    st = installer.install("powershell", dry_run=True, **kw)
    assert not profile.exists() and not st.installed and "Would write" in st.notes[0]
    installer.install("powershell", **kw)
    assert installer.BEGIN in profile.read_text("utf-8-sig")


def test_execution_policy_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "execution_policy", lambda: "Restricted")
    st = installer.install(
        "powershell",
        hooks_dir=tmp_path / "h",
        spool_dir=tmp_path / "s",
        pause_marker=tmp_path / "p",
        profile=tmp_path / "p.ps1",
        dry_run=True,
    )
    assert any("Set-ExecutionPolicy -Scope CurrentUser RemoteSigned" in n for n in st.notes)


def test_rendered_paths_are_quoted(tmp_path):
    weird = tmp_path / "it's here"
    ps = installer.render_hook("powershell", weird / "spool", weird / "paused")
    assert "it''s here" in ps and "__SPOOL__" not in ps
    sh = installer.render_hook("bash", weird / "spool", weird / "paused")
    assert "it'\\''s here" in sh and "__MARKER__" not in sh


# --- live: the hooks in real shells --------------------------------------------------------

PS_SCRIPT = r"""
. "$PSScriptRoot\hook.ps1"
function Add-TestHistory($cmd) {
    $n = Get-Date
    Add-History -InputObject ([pscustomobject]@{ CommandLine = $cmd; ExecutionStatus = 'Completed';
        StartExecutionTime = $n.AddSeconds(-2); EndExecutionTime = $n })
}
Add-TestHistory 'git status'; cmd /c "exit 3"; prompt | Out-Null
Add-TestHistory ' private command'; prompt | Out-Null
prompt | Out-Null
Add-TestHistory ('weird "quote" back\slash' + [char]9 + 'tab' + [char]7 + 'bell'); prompt | Out-Null
$total = 0.0
for ($i = 0; $i -lt 20; $i++) {
    Add-TestHistory "echo $i"
    $sw = [Diagnostics.Stopwatch]::StartNew(); prompt | Out-Null; $sw.Stop()
    $total += $sw.Elapsed.TotalMilliseconds
}
New-Item -ItemType File "$PSScriptRoot\paused" -Force | Out-Null
Add-TestHistory 'while paused'; prompt | Out-Null
Remove-Item "$PSScriptRoot\paused"
"AVG_MS=" + ($total / 20)
"PROMPT=" + (prompt)
"""


@pytest.mark.live
@pytest.mark.skipif(not shutil.which("powershell"), reason="Windows PowerShell not found")
def test_powershell_hook_live(tmp_path: Path):
    (tmp_path / "hook.ps1").write_text(
        installer.render_hook("powershell", tmp_path / "spool", tmp_path / "paused"), "utf-8"
    )
    (tmp_path / "run.ps1").write_text(PS_SCRIPT, "utf-8")
    out = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(tmp_path / "run.ps1"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "PROMPT=PS " in out.stdout  # the original prompt still renders
    avg_ms = float(out.stdout.split("AVG_MS=")[1].split()[0])
    print(f"powershell hook: {avg_ms:.2f} ms per prompt")
    assert avg_ms < 60  # gross-regression guard only; timing varies run to run (see BUDGET.md)

    lines = [
        json.loads(x)
        for f in (tmp_path / "spool").glob("*.jsonl")
        for x in f.read_text("utf-8").splitlines()
    ]
    commands = [r["command"] for r in lines]
    assert (
        commands[0] == "git status" and lines[0]["exit_code"] == 3 and lines[0]["success"] is False
    )
    assert commands[1] == 'weird "quote" back\\slash\ttab\x07bell'
    assert " private command" not in commands and "while paused" not in commands
    assert len(commands) == 22  # git status + weird + 20 echoes; empty Enter recorded nothing

    events = ingest_spool_dir(tmp_path / "spool", SpoolStats())
    assert len(events) == 22 and not list((tmp_path / "spool").iterdir())
    assert events[0].payload.duration_ms == 2000  # type: ignore[union-attr]


# Git Bash, not WSL's C:\Windows\System32\bash.exe (which runs a different OS).
_GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
BASH = str(_GIT_BASH) if _GIT_BASH.exists() else (shutil.which("bash") or "bash")


@pytest.mark.live
@pytest.mark.skipif(not Path(BASH).exists(), reason="bash not found")
def test_bash_hook_live(tmp_path: Path):
    hook = tmp_path / "hook.sh"
    hook.write_text(
        installer.render_hook("bash", tmp_path / "spool", tmp_path / "paused"),
        "utf-8",
        newline="\n",
    )
    rc = tmp_path / "rc.sh"
    rc.write_text(f"HISTFILE=/dev/null\n. '{hook.as_posix()}'\n", "utf-8", newline="\n")
    marker = (tmp_path / "paused").as_posix()
    # A real interactive bash reading typed lines from stdin: prompts, history and traps all run.
    typed = (
        "\n".join(
            [
                "(exit 2)",
                " echo secret thing",
                "",
                'echo "hi" \\ there',
                f"touch '{marker}'",
                "echo while paused",
                f"rm '{marker}'",
                "exit",
            ]
        )
        + "\n"
    )
    out = subprocess.run(
        [BASH, "--rcfile", str(rc), "-i"], input=typed, capture_output=True, text=True, timeout=60
    )
    records = [
        json.loads(x)
        for f in (tmp_path / "spool").glob("*.jsonl")
        for x in f.read_text("utf-8").splitlines()
    ]
    commands = [r["command"] for r in records]
    assert "(exit 2)" in commands and 'echo "hi" \\ there' in commands, (commands, out.stderr)
    assert not any("secret" in c or "while paused" in c for c in commands)
    assert records[0]["exit_code"] == 2 and records[0]["success"] is False
    # `touch marker` is not recorded (the marker exists when its prompt runs); `rm marker` is.
    assert len(commands) == 3 and commands[-1].startswith("rm ")
    events = ingest_spool_dir(tmp_path / "spool", SpoolStats())
    assert len(events) == 3
    assert events[0].payload.cwd and events[0].payload.cwd[1] == ":"  # type: ignore[union-attr]  # /c/x → C:/x
