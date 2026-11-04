"""`do "open X [in Y]"` decided by rules, without the model (actions/quick.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from personal_os.actions.paths import PathPolicy
from personal_os.actions.planner import run_request
from personal_os.actions.quick import find_path, plan_open
from personal_os.config.schema import Config
from personal_os.llm.fake import FakeProvider
from tests.test_actions import Env


def never(*_: Any) -> Any:
    raise AssertionError("an open request with a clear target must not need the model")


@pytest.fixture
def env(home: Path, config: Config) -> Env:
    e = Env(home, config)
    apps = {"notepad": ["notepad.exe"], "explorer": ["explorer.exe"], "vscode": ["notepad.exe"]}
    e.ex.config = e.config = e.config.model_copy(
        update={"actions": e.config.actions.model_copy(update={"apps": apps})}
    )
    e.ex.paths = PathPolicy(e.config)
    (e.work / "typewriter").mkdir()
    (e.work / "docs" / "deep").mkdir(parents=True)
    (e.work / "docs" / "deep" / "Report Final.pdf").write_bytes(b"%PDF")
    (e.work / "holiday.jpg").write_bytes(b"jpg")
    (e.work / "clip.mp4").write_bytes(b"mp4")
    return e


def do(env: Env, request: str) -> Any:
    return run_request(request, env.ex, FakeProvider(never))


@pytest.mark.parametrize(
    ("request_text", "call"),
    [
        ("open typewriter", ("open_path", "typewriter")),
        ("open the typewriter folder", ("open_path", "typewriter")),
        ("open typewriter folder in fileexplorer", ("launch", "typewriter")),
        ("can you open holiday.jpg", ("open_path", "holiday.jpg")),
        ("open clip video", ("open_path", "clip.mp4")),
        ("open report final pdf", ("open_path", "Report Final.pdf")),  # three folders down
        ("open Report Final.pdf in notepad", ("launch", "Report Final.pdf")),
    ],
)
def test_files_folders_pdfs_images_and_videos_open_without_the_model(
    env: Env, request_text: str, call: tuple[str, str]
):
    report = do(env, request_text)
    assert [o.status for o in report.outcomes] == ["succeeded"], report.reply
    kind, name = call
    last = env.desktop.calls[-1]
    assert last[0] == kind
    opened = last[1] if kind == "open_path" else last[1][-1]
    assert Path(opened).name == name


def test_apps_open_by_name(env: Env):
    report = do(env, "open file explorer")
    assert report.outcomes[0].tool == "open_app" and env.desktop.calls == [
        ("launch", ["explorer.exe"])
    ]


def test_two_files_with_one_name_ask_which_and_a_folder_settles_it(env: Env):
    (env.work / "a").mkdir()
    (env.work / "b").mkdir()
    (env.work / "a" / "no.pdf").write_bytes(b"%PDF")
    (env.work / "b" / "no.pdf").write_bytes(b"%PDF")
    report = do(env, "open no.pdf")
    assert report.outcomes == [] and "more than one 'no.pdf'" in report.reply
    assert env.desktop.calls == []
    report = do(env, "open no.pdf in b")
    assert env.desktop.calls == [("open_path", str(env.work / "b" / "no.pdf"))]


def test_nothing_found_says_where_it_looked(env: Env):
    def no_plan(*_: Any) -> Any:
        return {"steps": "none", "calls": [], "reply": ""}

    report = run_request("open moonbase folder", env.ex, FakeProvider(no_plan))
    assert report.outcomes == [] and "couldn't find 'moonbase'" in report.reply
    assert str(env.work) in report.reply


def test_only_allowed_folders_are_searched(env: Env, tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret-plans.pdf").write_bytes(b"%PDF")
    assert find_path(env.ex.paths, "secret-plans.pdf") is None
    assert (env.work / ".env").write_text("X=1") and find_path(env.ex.paths, ".env") is not None
    report = do(env, "open .env")  # found, but the path rules still refuse secrets
    assert [o.status for o in report.outcomes] == ["blocked"]


def test_requests_that_are_not_simple_opens_go_to_the_model(env: Env):
    for text in ("take a screenshot", "open https://example.org", "open youtube.com"):
        assert plan_open(text, env.ex.paths, env.conn) is None, text
