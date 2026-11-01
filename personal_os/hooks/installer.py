"""Install/uninstall the shell hooks as a clearly delimited, reversible profile block.

The hook script itself lives in the data dir (hooks/), so upgrading Personal OS updates the
hook without touching the profile again. The profile block only dot-sources it if it exists,
so deleting the data dir can never break a shell.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Literal

Shell = Literal["powershell", "bash"]
BEGIN = "# >>> personal-os >>>"
END = "# <<< personal-os <<<"
_HOOK_FILES: dict[Shell, str] = {"powershell": "powershell.ps1", "bash": "bash.sh"}
_RISKY_POLICIES = {"Restricted", "AllSigned", "Undefined"}


@dataclass
class HookStatus:
    shell: Shell
    profile: Path
    installed: bool
    hook_file: Path
    notes: list[str] = field(default_factory=list)


def _ps_quote(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def _sh_quote(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "'\\''") + "'"


def _run_powershell(command: str) -> str | None:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return None
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        out = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None


def default_profile(shell: Shell) -> Path:
    if shell == "powershell":
        # CurrentUserAllHosts: loaded by the console, Windows Terminal and the VS Code terminal.
        found = _run_powershell("$PROFILE.CurrentUserAllHosts")
        if found:
            return Path(found)
        return Path.home() / "Documents" / "WindowsPowerShell" / "profile.ps1"
    return Path.home() / ".bashrc"


def execution_policy() -> str | None:
    return _run_powershell("Get-ExecutionPolicy")


def block_for(shell: Shell, hook_file: Path) -> str:
    if shell == "powershell":
        q = _ps_quote(hook_file)
        body = f"if (Test-Path -LiteralPath {q}) {{ . {q} }}"
    else:
        q = _sh_quote(hook_file)
        body = f"[ -f {q} ] && . {q}"
    return "\n".join(
        [
            BEGIN,
            "# Added by `personal-os install-hooks`; remove with `personal-os uninstall-hooks`.",
            body,
            END,
        ]
    )


def render_hook(shell: Shell, spool_dir: Path, pause_marker: Path) -> str:
    template = resources.files("personal_os.hooks").joinpath(_HOOK_FILES[shell]).read_text("utf-8")
    if shell == "powershell":
        spool, marker = str(spool_dir).replace("'", "''"), str(pause_marker).replace("'", "''")
    else:
        spool, marker = spool_dir.as_posix(), pause_marker.as_posix()
        spool, marker = (s.replace("'", "'\\''") for s in (spool, marker))
    return template.replace("__SPOOL__", spool).replace("__MARKER__", marker)


def _read(profile: Path) -> tuple[str, bool]:
    if not profile.exists():
        return "", False
    raw = profile.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    return raw.decode("utf-8-sig", errors="replace"), bom


def _write(profile: Path, text: str, *, bom: bool, crlf: bool) -> None:
    profile.parent.mkdir(parents=True, exist_ok=True)
    text = text.replace("\r\n", "\n")
    if crlf:
        text = text.replace("\n", "\r\n")
    data = text.encode("utf-8")
    profile.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)


def strip_block(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == BEGIN:
            inside = True
            continue
        if inside and line.strip() == END:
            inside = False
            continue
        if not inside:
            out.append(line)
    result = "\n".join(out)
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result


def install(
    shell: Shell,
    *,
    hooks_dir: Path,
    spool_dir: Path,
    pause_marker: Path,
    profile: Path | None = None,
    dry_run: bool = False,
) -> HookStatus:
    profile = profile or default_profile(shell)
    hook_file = hooks_dir / _HOOK_FILES[shell]
    status = HookStatus(shell, profile, True, hook_file)
    text, bom = _read(profile)
    new_text = strip_block(text).rstrip("\n")
    new_text = (new_text + "\n\n" if new_text else "") + block_for(shell, hook_file) + "\n"
    if shell == "powershell":
        policy = execution_policy()
        if policy in _RISKY_POLICIES:
            status.notes.append(
                f"PowerShell's execution policy is {policy}, so profiles don't load. Run this "
                "once yourself to allow local scripts: "
                "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
            )
    if shell == "bash":
        bash_profile = Path.home() / ".bash_profile"
        if bash_profile.exists() and ".bashrc" not in bash_profile.read_text(
            "utf-8", errors="replace"
        ):
            status.notes.append(
                "~/.bash_profile exists but doesn't source ~/.bashrc; login shells "
                "(Git Bash) won't load the hook until it does."
            )
    if dry_run:
        status.installed = False
        status.notes.insert(
            0,
            f"Would write {hook_file} and add this block to {profile}:\n"
            + block_for(shell, hook_file),
        )
        return status
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_file.write_text(
        render_hook(shell, spool_dir, pause_marker), encoding="utf-8", newline="\n"
    )
    if text and not (profile.with_name(profile.name + ".personal-os.bak")).exists():
        shutil.copy2(profile, profile.with_name(profile.name + ".personal-os.bak"))
    _write(
        profile,
        new_text,
        bom=bom or (shell == "powershell" and not text),
        crlf=shell == "powershell",
    )
    return status


def uninstall(shell: Shell, *, hooks_dir: Path, profile: Path | None = None) -> HookStatus:
    profile = profile or default_profile(shell)
    hook_file = hooks_dir / _HOOK_FILES[shell]
    text, bom = _read(profile)
    if BEGIN in text:
        _write(profile, strip_block(text).rstrip("\n") + "\n", bom=bom, crlf=shell == "powershell")
    hook_file.unlink(missing_ok=True)
    return HookStatus(shell, profile, False, hook_file)


def status(shell: Shell, *, hooks_dir: Path, profile: Path | None = None) -> HookStatus:
    profile = profile or default_profile(shell)
    hook_file = hooks_dir / _HOOK_FILES[shell]
    text, _ = _read(profile)
    st = HookStatus(shell, profile, BEGIN in text and hook_file.exists(), hook_file)
    if BEGIN in text and not hook_file.exists():
        st.notes.append("profile block present but hook file missing; re-run install-hooks")
    return st
