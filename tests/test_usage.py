"""Time on sites, apps and projects, and pages on a site: computed answers (context/usage.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from personal_os.config.schema import Config
from personal_os.context import ask as ask_mod
from personal_os.context.question import parse_question
from personal_os.context.usage import find_target
from personal_os.llm.fake import FakeProvider
from personal_os.projects import registry
from personal_os.projects.sessions import rebuild_sessions
from tests.test_context import auth_session
from tests.test_projects import World


@pytest.fixture
def w(home: Path, config: Config) -> World:
    return World(home, config)


def never(*_: Any) -> Any:
    raise AssertionError("the model must not be asked: this is a computed answer")


def ask(w: World, question: str) -> ask_mod.Answer:
    return ask_mod.ask(w.conn, FakeProvider(never), question, w.clock.now())


def browsing(w: World) -> None:
    w.visit(0, 10, "https://www.youtube.com/watch?v=a", "Camping trip - YouTube",
            video_watched_s=540)  # fmt: skip
    w.visit(10, 13, "https://github.com/me/repo", "me/repo")
    w.visit(13, 14, "https://www.youtube.com/feed/subscriptions", "Subscriptions - YouTube")
    w.visit(14, 19, "https://m.youtube.com/watch?v=b", "3D printed tech - YouTube",
            video_watched_s=280)  # fmt: skip
    w.visit(19, 20, "https://meet.google.com/abc-defg", "Meet - abc-defg")


def test_time_on_a_site_adds_up_its_pages_without_the_model(w: World):
    browsing(w)
    a = ask(w, "how long did I watch youtube today?")
    assert a.question.intent == "usage" and a.mode == "facts" and a.confidence == "high"
    assert "You spent 16m on YouTube today, across 3 page visits" in a.text
    assert "Videos were playing for about 14m" in a.text
    assert "'Camping trip - YouTube' (10m)" in a.text
    cited = a.cited()
    assert cited and all("youtube.com" in x.text for x in cited)  # github isn't counted
    assert a.gathered.facts == ["Active time on YouTube (today): 16m over 3 page visits."]


def test_pages_and_videos_on_a_site_are_listed_by_time(w: World):
    browsing(w)
    a = ask(w, "what were the youtube videos I was watching?")
    assert a.question.intent == "visits" and a.mode == "facts"
    assert a.text.startswith("You watched 2 videos on YouTube today (15m in all):")
    assert "\n- 09:00 'Camping trip - YouTube' (10m) [1]" in a.text
    assert "\n- 09:14 '3D printed tech - YouTube' (5m) [2]" in a.text
    assert "Subscriptions" not in a.text  # not a video
    pages = ask(w, "which youtube pages did I visit today?")
    assert "You opened 3 pages on YouTube today" in pages.text and "Subscriptions" in pages.text


def test_time_in_an_app_is_its_focused_window_time(w: World):
    w.window(0, 30, "Inbox - Google Chrome", exe="chrome.exe", app="Google Chrome")
    w.window(30, 40, "main.py - Visual Studio Code")
    w.window(40, 45, "Docs - Google Chrome", exe="chrome.exe", app="Google Chrome")
    a = ask(w, "how much time did I spend in chrome today?")
    assert a.question.intent == "usage" and a.question.target is not None
    assert a.question.target.kind == "app"
    assert "Google Chrome was your active window for 35m today" in a.text
    assert "'Inbox' (30m)" in a.text  # the app name isn't repeated in the title


def test_time_on_a_project_counts_its_share_of_sessions(w: World):
    auth_session(w)
    rebuild_sessions(w.conn, w.clock, w.at(-60), w.at(600))
    a = ask(w, "how long did I work on auth-service today?")
    assert a.mode == "facts" and a.question.project is not None
    assert a.text.startswith("You worked on auth-service for ") and "1 session" in a.text
    assert a.cited()


def test_nothing_found_says_what_was_looked_for(w: World):
    browsing(w)
    a = ask(w, "how long did I watch youtube yesterday?")
    assert a.mode == "no_data" and "time on YouTube" in a.text


def test_questions_without_a_known_site_or_app_keep_their_old_route(w: World):
    browsing(w)
    projects = registry.load_projects(w.conn)
    now = w.clock.now()
    for question, intent in [
        ("what did I do today?", "activity"),
        ("how long did I spend on netflix today?", "activity"),  # never visited: no target
        ("where did I leave off?", "leave_off"),
    ]:
        q = ask_mod.usage.classify(w.conn, parse_question(question, projects, now), now)
        assert q.intent == intent and q.target is None, question


def test_sites_and_apps_are_matched_by_the_names_people_use(w: World):
    browsing(w)
    w.window(0, 5, "Claude", exe="claude.exe", app="Claude")
    w.visit(5, 6, "https://claude.ai/chat/1", "Claude")
    now = w.clock.now()
    meet = find_target(w.conn, "how long was I on google meet?", now)
    assert meet is not None and meet.kind == "site" and meet.names == {"meet.google.com"}
    yt = find_target(w.conn, "time on youtube", now)
    assert yt is not None and yt.names == {"www.youtube.com", "m.youtube.com"}
    assert yt.label == "YouTube"
    app = find_target(w.conn, "how long was I in claude", now)
    site = find_target(w.conn, "how long was I reading claude pages", now)
    assert app is not None and app.kind == "app" and site is not None and site.kind == "site"
    assert find_target(w.conn, "how long did I work today", now) is None


def test_a_repo_called_project_does_not_swallow_another_name(w: World):
    repo = registry.Project("project", "project", roots=["E:/project"])
    now = w.clock.now()
    q = parse_question("how long did I spend on moon-base project?", [repo], now)
    assert q.project is None and q.project_mention == "moon-base"
    q = parse_question("how long did I work on project today?", [repo], now)
    assert q.project is repo  # no other name given: it's that repo
