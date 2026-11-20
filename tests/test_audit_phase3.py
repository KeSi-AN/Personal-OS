"""Phase 3: projects get only what is theirs. Files and commits by their folder alone;
entertainment never by a guess; a session's side work on another project gets a summary of
its own, and the main summary leaves it out."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from personal_os.config.schema import Config
from personal_os.context import summarizer
from personal_os.core.clock import SimClock
from personal_os.projects.resolver import RESOLVER_VERSION, reassess_if_needed
from personal_os.projects.sessions import build_sessions, rebuild_sessions
from personal_os.storage import repo
from tests.conftest import T0
from tests.test_projects import AUTH, SITE, World


@pytest.fixture
def w(home: Path, config: Config) -> World:
    return World(home, config)


def test_a_file_outside_every_project_is_never_guessed_into_one(w: World):
    w.commit(0, AUTH, "auth work")
    w.window(0, 30, "session.py - auth-service - Visual Studio Code")
    inside = w.edit(5, f"{AUTH}/auth/session.py", AUTH)
    # Named like the project's work, but in no project's folder (as a Jetty script was).
    outside = w.edit(6, "C:/Users/me/Documents/Jetty/auth_session_export.py")
    w.resolve()
    assert w.project_of(inside)[0] == "auth-service"
    assert w.project_of(outside) == (None, "unassigned")


@pytest.mark.parametrize(
    ("url", "title"),
    [
        ("https://ww6-fmovies.com/watch/dune", "Fmovies - Watch Movies online for free"),
        ("https://music.youtube.com/watch?v=x", "Bad Luck - YouTube Music"),
        ("https://www.youtube.com/watch?v=c", "31 Ways to Use Claude in 2027 - YouTube"),
    ],
)
def test_entertainment_is_never_guessed_into_the_project_around_it(w: World, url: str, title: str):
    w.commit(0, AUTH, "x")
    w.window(0, 20, "session.py - auth-service - Visual Studio Code")
    page = w.visit(8, 12, url, title)
    w.resolve()
    assert w.project_of(page)[0] != "auth-service"


def test_a_window_on_a_project_page_is_still_placed(w: World):
    w.commit(0, AUTH, "x", remote="github.com/me/auth-service")
    w.window(0, 20, "session.py - auth-service - Visual Studio Code")
    gh = w.visit(8, 12, "https://github.com/me/auth-service/pull/3", "Fix refresh · auth-service")
    w.resolve()
    assert w.project_of(gh)[0] == "auth-service"


def side_work(w: World) -> None:
    """09:00-10:00 on auth-service, with two portfolio-site commits in the middle."""
    w.commit(0, AUTH, "auth work")
    w.commit(0, SITE, "site start")
    w.window(0, 60, "session.py - auth-service - Visual Studio Code")
    for m in range(2, 60, 4):
        w.edit(m, f"{AUTH}/auth/session.py", AUTH)
    w.edit(30, f"{SITE}/src/Hero.tsx", SITE)
    w.commit(31, SITE, "Hero: new tagline")
    w.edit(32, f"{SITE}/src/Hero.tsx", SITE)
    w.commit(33, SITE, "Hero: fix spacing")
    w.resolve()


def test_side_work_gets_its_own_summary_and_leaves_the_main_one(w: World):
    side_work(w)
    (session,) = [s for s in build_sessions(w.conn, T0, T0 + timedelta(hours=2))
                  if s.project_id == "auth-service"]  # fmt: skip
    sides = summarizer.side_pieces(w.conn, session)
    assert [(s.project_id, s.side_of) for s in sides] == [("portfolio-site", "auth-service")]
    main_text = summarizer.session_pack(w.conn, session).render()
    side_text = summarizer.session_pack(w.conn, sides[0]).render()
    assert "Hero: new tagline" not in main_text and "session.py" in main_text
    assert "Hero: new tagline" in side_text and "session.py" not in side_text
    assert summarizer.summary_id(sides[0]) == f"session:{session.id}:portfolio-site"
    prompt = summarizer.session_prompt(sides[0], summarizer.session_pack(w.conn, sides[0]))
    assert "side work on project portfolio-site" in prompt


def test_side_pieces_are_pending_like_sessions(w: World):
    side_work(w)
    clock = SimClock(T0 + timedelta(hours=3))
    rebuild_sessions(w.conn, clock, T0 - timedelta(hours=1), T0 + timedelta(hours=3))
    pending = summarizer.pending_sessions(w.conn, clock, min_active_ms=0)
    assert {(s.project_id, s.side_of) for s, _ in pending} >= {
        ("auth-service", None), ("portfolio-site", "auth-service")
    }  # fmt: skip


def test_history_is_reassessed_once(w: World):
    side_work(w)
    clock = SimClock(T0 + timedelta(hours=3))
    assert reassess_if_needed(w.conn, clock) is True
    assert repo.get_setting(w.conn, "projects.resolver_version") == RESOLVER_VERSION
    assert reassess_if_needed(w.conn, clock) is False


@pytest.mark.parametrize(
    "said", ["what did I do on shop-app recently", "what have I been up to lately"]
)
def test_recently_means_the_last_week(said: str):
    from personal_os.context.question import parse_time_range

    now = T0 + timedelta(hours=16)  # 1 a.m.: "today" would be empty
    start, end, label, _used = parse_time_range(said.lower(), now)
    assert (start, end, label) == (now - timedelta(days=7), now, "the last 7 days")
