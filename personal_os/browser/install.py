"""Register the native messaging host with Chrome, Brave and Edge (per user, no admin rights).

Writes a host manifest and a small launcher .bat into the data dir, and points the browser's
per-user registry key at the manifest. `allowed_origins` names only our extension's ID, so no
other extension can talk to the host.
"""

from __future__ import annotations

import contextlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from personal_os.browser.native_host import HOST_NAME

# Fixed by the public `key` in extensions/browser/manifest.json (a test keeps them in sync).
EXTENSION_ID = "oapjpjppogicpgagjemcbpedbbjajmap"

Browser = Literal["chrome", "brave", "edge"]
BROWSERS: tuple[Browser, ...] = ("chrome", "brave", "edge")
_REG_PATHS: dict[Browser, str] = {
    "chrome": rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}",
    "brave": rf"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts\{HOST_NAME}",
    "edge": rf"Software\Microsoft\Edge\NativeMessagingHosts\{HOST_NAME}",
}


@dataclass
class HostStatus:
    browser: Browser
    registered: bool
    manifest: Path | None


def extension_id_from_key(key_b64: str) -> str:
    """Chromium's extension ID: first 16 bytes of sha256(public key), hex digits mapped to a-p."""
    import base64
    import hashlib

    digest = hashlib.sha256(base64.b64decode(key_b64)).digest()[:16]
    return "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 15)) for b in digest)


def host_files(data_dir: Path, browser: Browser) -> tuple[Path, Path]:
    d = data_dir / "native_host"
    return d / f"{HOST_NAME}.{browser}.json", d / f"host-{browser}.bat"


def write_host_files(data_dir: Path, browser: Browser, *, home: Path | None = None) -> Path:
    manifest, launcher = host_files(data_dir, browser)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable)
    home_arg = f' --home="{home}"' if home else ""
    # -I: isolated mode, so a stray PYTHONPATH or user site-packages can't inject code here.
    launcher.write_text(
        "@echo off\r\n"
        f'"{python}" -I -m personal_os.browser.native_host --browser={browser}{home_arg} %*\r\n',
        encoding="utf-8",
    )
    manifest.write_text(
        json.dumps(
            {
                "name": HOST_NAME,
                "description": "Personal OS native messaging host",
                "path": str(launcher),
                "type": "stdio",
                "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return manifest


def register(data_dir: Path, browser: Browser, *, home: Path | None = None) -> Path:
    manifest = write_host_files(data_dir, browser, home=home)
    if sys.platform == "win32":
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_PATHS[browser]) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest))
    return manifest


def unregister(data_dir: Path, browser: Browser) -> None:
    if sys.platform == "win32":
        import winreg

        with contextlib.suppress(FileNotFoundError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _REG_PATHS[browser])
    for f in host_files(data_dir, browser):
        f.unlink(missing_ok=True)


def status(browser: Browser) -> HostStatus:
    if sys.platform != "win32":
        return HostStatus(browser, False, None)
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_PATHS[browser]) as key:
            value, _ = winreg.QueryValueEx(key, "")
    except FileNotFoundError:
        return HostStatus(browser, False, None)
    path = Path(value)
    return HostStatus(browser, path.exists(), path)
