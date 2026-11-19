"""Every app installed on this PC, as the Start menu lists them (for `open_app`).

Windows lists installed apps with an app ID (Get-StartApps): desktop programs and Store apps
alike. Any of them starts with `explorer.exe shell:AppsFolder\\<AppID>`, the same thing a
click in the Start menu does. To open a file *in* an app, its program is needed; that comes
from the app's Start menu shortcut. Apps in your own list (actions.apps) come first.

The list is read with one PowerShell call and cached for a few hours in the runtime folder.
Uninstallers are left out, so "open X" never removes anything.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from personal_os.core import runtime

CACHE = "apps.json"
CACHE_HOURS = 6
_SKIP = re.compile(r"^(uninstall|remove)\b|\buninstall(er)?\b", re.I)

_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$sh = New-Object -ComObject WScript.Shell
$targets = @{}
foreach ($dir in "$env:ProgramData\Microsoft\Windows\Start Menu\Programs",
                 "$env:APPDATA\Microsoft\Windows\Start Menu\Programs") {
  Get-ChildItem $dir -Recurse -Filter *.lnk | ForEach-Object {
    $t = $sh.CreateShortcut($_.FullName).TargetPath
    if ($t -and $t.ToLower().EndsWith('.exe')) { $targets[$_.BaseName] = $t }
  }
}
Get-StartApps | ForEach-Object {
  [pscustomobject]@{ name = $_.Name; id = $_.AppID; exe = $targets[$_.Name] }
} | ConvertTo-Json -Compress
"""


@dataclass(frozen=True)
class InstalledApp:
    name: str
    app_id: str
    exe: str | None = None  # the program, when its shortcut says (needed to open a file in it)

    def argv(self, target: str | None = None) -> list[str]:
        if target is not None:
            if not self.exe:
                raise LookupError(f"{self.name} can only be started on its own, not with a file")
            return [self.exe, target]
        return ["explorer.exe", f"shell:AppsFolder\\{self.app_id}"]


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower().removesuffix(".exe"))


SCAN_TIMEOUT_S = 25  # it took 7 s here warm; a stalled scan gives up rather than hang a turn


def read_installed() -> list[InstalledApp]:
    """Ask Windows (one PowerShell call: about 7 s here, far longer when PowerShell is cold).
    [] if it doesn't answer in time."""
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _SCRIPT],
            capture_output=True,
            text=True,
            timeout=SCAN_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    try:
        rows = json.loads(r.stdout or "[]")
    except ValueError:
        return []
    if isinstance(rows, dict):
        rows = [rows]
    out = []
    for row in rows:
        name, app_id = str(row.get("name") or ""), str(row.get("id") or "")
        if name and app_id and not _SKIP.search(name):
            out.append(InstalledApp(name, app_id, row.get("exe") or None))
    return out


_memo: tuple[float, list[InstalledApp]] | None = None
_refreshing = threading.Lock()


def _cached(runtime_dir: Path) -> tuple[float, list[InstalledApp]] | None:
    """The last list and when it was made, whatever its age (memory first, then the file)."""
    if _memo is not None:
        return _memo
    try:
        data = json.loads((runtime_dir / CACHE).read_text("utf-8"))
        apps = [InstalledApp(a["name"], a["app_id"], a.get("exe")) for a in data["apps"]]
        return float(data["at"]), apps
    except (OSError, ValueError, KeyError, TypeError):
        return None


def installed_now(runtime_dir: Path) -> list[InstalledApp]:
    """The installed apps without ever waiting for Windows: the last list, however old, with
    a fresh one made in the background when it's stale ([] the very first time). For a turn
    in progress ("play music"), which once waited 60 s on a stalled scan (17 Nov)."""
    return _installed_now(runtime_dir)


def installed(runtime_dir: Path, *, refresh: bool = False) -> list[InstalledApp]:
    """The installed apps, from the cache when it's fresh (may run the scan: not mid-turn)."""
    return _installed(runtime_dir, refresh=refresh)


# The two above are what callers use, and what tests replace; these do the work.


def _installed_now(runtime_dir: Path) -> list[InstalledApp]:
    cached = _cached(runtime_dir)
    stale = cached is None or time.time() - cached[0] >= CACHE_HOURS * 3600
    if stale and _refreshing.acquire(blocking=False):  # one refresh at a time

        def refresh() -> None:
            try:
                _installed(runtime_dir, refresh=True)
            finally:
                _refreshing.release()

        threading.Thread(target=refresh, name="pos-apps-refresh", daemon=True).start()
    return cached[1] if cached is not None else []


def _installed(runtime_dir: Path, *, refresh: bool = False) -> list[InstalledApp]:
    global _memo
    now = time.time()
    if not refresh and _memo is not None and now - _memo[0] < CACHE_HOURS * 3600:
        return _memo[1]
    path = runtime_dir / CACHE
    if not refresh:
        try:
            data = json.loads(path.read_text("utf-8"))
            if now - float(data["at"]) < CACHE_HOURS * 3600:
                apps = [InstalledApp(a["name"], a["app_id"], a.get("exe")) for a in data["apps"]]
                _memo = (now, apps)
                return apps
        except (OSError, ValueError, KeyError, TypeError):
            pass
    apps = read_installed()
    if not apps:
        # The scan stalled or failed: keep the last list (whatever its age) and try again in
        # 5 minutes, rather than know no apps for the next 6 hours.
        old = _cached(runtime_dir)
        _memo = (now - CACHE_HOURS * 3600 + 300, old[1] if old else [])
        return _memo[1]
    runtime.write_json_atomic(
        path,
        {"at": now, "apps": [{"name": a.name, "app_id": a.app_id, "exe": a.exe} for a in apps]},
    )
    _memo = (now, apps)
    return apps


class AmbiguousApp(LookupError):
    pass


def find_installed(
    name: str, apps: list[InstalledApp], *, fuzzy: bool = True
) -> InstalledApp | None:
    """The app `name` means: the same name ("antigravity" -> Antigravity, not Antigravity IDE)
    or program ("chrome"), else (fuzzy) the one shortest name containing it ("powershell" ->
    Windows PowerShell). Two equally good matches raise AmbiguousApp listing them."""
    k = _key(name)
    if len(k) < 3:
        return None
    exact = [a for a in apps if _key(a.name) == k]
    if exact:
        return exact[0]
    by_exe = [a for a in apps if a.exe and _key(Path(a.exe).stem) == k]
    if len({a.exe for a in by_exe}) == 1:
        return by_exe[0]
    if not fuzzy:
        return None
    containing = sorted((a for a in apps if k in _key(a.name)), key=lambda a: len(_key(a.name)))
    if not containing:
        return None
    best = [a for a in containing if len(_key(a.name)) == len(_key(containing[0].name))]
    if len({a.name for a in best}) > 1:
        names = ", ".join(sorted({a.name for a in best})[:5])
        raise AmbiguousApp(f"'{name}' could be {names}; say which")
    return best[0]
