"""Any installed app opens by name; file actions can be kept to allowed_paths only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from personal_os.actions import apps
from personal_os.actions.apps import AmbiguousApp, InstalledApp, find_installed
from personal_os.actions.base import ActionError
from personal_os.actions.paths import PathPolicy
from personal_os.actions.planner import run_request
from personal_os.config.schema import Config
from personal_os.llm.fake import FakeProvider
from tests.test_actions import Env

START_MENU = [
    InstalledApp("Antigravity", "electron.app.Antigravity", "C:/Programs/antigravity/Antigravity.exe"),
    InstalledApp("Antigravity IDE", "Google.AntigravityIDE", "C:/Programs/AG IDE/Antigravity IDE.exe"),
    InstalledApp("Claude", "Claude_pzs8sxrjxfjjc!Claude"),  # a Store app: no program path
    InstalledApp("Windows PowerShell", "{1AC1}\\WindowsPowerShell\\v1.0\\powershell.exe"),
    InstalledApp("Windows PowerShell ISE", "{1AC1}\\WindowsPowerShell\\v1.0\\PowerShell_ISE.exe"),
    InstalledApp("WhatsApp", "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"),
    InstalledApp("Sticky Notes", "Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe!App"),
    InstalledApp("Teams Home", "a.teams"),
    InstalledApp("Teams Work", "b.teams"),
]  # fmt: skip


def never(*_: Any) -> Any:
    raise AssertionError("the model isn't needed for this")


@pytest.fixture
def env(home: Path, config: Config, monkeypatch: pytest.MonkeyPatch) -> Env:
    monkeypatch.setattr(apps, "installed", lambda *_a, **_k: START_MENU)
    return Env(home, config)


def test_installed_apps_are_matched_by_name():
    assert find_installed("antigravity", START_MENU).name == "Antigravity"  # exact beats longer
    assert find_installed("Antigravity IDE", START_MENU).app_id == "Google.AntigravityIDE"
    assert find_installed("powershell", START_MENU).name == "Windows PowerShell"  # shortest
    assert find_installed("powershell", START_MENU, fuzzy=False) is None
    assert find_installed("photoshop", START_MENU) is None
    with pytest.raises(AmbiguousApp, match="Teams Home, Teams Work"):
        find_installed("teams", START_MENU)


def test_any_installed_app_starts_through_the_start_menu(env: Env):
    for said, app_id in [
        ("open claude", "Claude_pzs8sxrjxfjjc!Claude"),
        ("can you open whatsapp", "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"),
        ("open powershell", "{1AC1}\\WindowsPowerShell\\v1.0\\powershell.exe"),
    ]:
        report = run_request(said, env.ex, FakeProvider(never))
        assert [o.status for o in report.outcomes] == ["succeeded"], (said, report.reply)
        assert env.desktop.calls[-1] == ("launch", ["explorer.exe", f"shell:AppsFolder\\{app_id}"])


def test_a_folder_opens_in_an_installed_app_through_its_program(env: Env):
    (env.work / "newtab").mkdir()
    report = run_request("open newtab in antigravity", env.ex, FakeProvider(never))
    assert [o.status for o in report.outcomes] == ["succeeded"], report.reply
    assert env.desktop.calls[-1] == (
        "launch",
        ["C:/Programs/antigravity/Antigravity.exe", str((env.work / "newtab").resolve())],
    )
    report = run_request("open newtab in claude", env.ex, FakeProvider(never))
    assert report.outcomes[0].status == "blocked"
    assert "only be started on its own" in report.outcomes[0].message


def test_unclear_app_names_ask_instead_of_guessing(env: Env):
    report = run_request("open teams", env.ex, FakeProvider(never))
    assert report.outcomes == [] and "could be Teams Home, Teams Work" in report.reply


def test_a_folder_on_disk_wins_over_a_loose_app_match(env: Env):
    (env.work / "notes").mkdir()
    report = run_request("open notes", env.ex, FakeProvider(never))
    assert env.desktop.calls[-1] == ("open_path", str((env.work / "notes").resolve()))
    assert report.outcomes[0].tool == "open_path"


def test_actions_can_be_kept_out_of_watched_folders(home: Path, config: Config):
    base = home.parent / f"{home.name}-disk"  # outside the data folder, like a real drive
    watched, allowed = base / "Documents", base / "E"
    watched.mkdir(parents=True)
    allowed.mkdir(parents=True)
    (watched / "a.pdf").write_bytes(b"%PDF")
    (allowed / "b.pdf").write_bytes(b"%PDF")
    actions = config.actions.model_copy(
        update={"allowed_paths": [allowed], "act_in_watched_roots": False}
    )
    cfg = config.model_copy(update={"watched_roots": [watched], "actions": actions})
    policy = PathPolicy(cfg)
    assert policy.check(str(allowed / "b.pdf"), must_exist=True)
    with pytest.raises(ActionError, match="outside"):
        policy.check(str(watched / "a.pdf"))
    both = PathPolicy(config.model_copy(update={"watched_roots": [watched]}))
    assert both.check(str(watched / "a.pdf"), must_exist=True)  # the default: watched counts
