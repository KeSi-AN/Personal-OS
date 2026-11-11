"""How time was spent: categories for apps, pages and YouTube videos, the breakdown, flags."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from personal_os.context import activity
from personal_os.llm.fake import FakeProvider
from tests.conftest import T0


@pytest.mark.parametrize(
    ("exe", "path", "cat"),
    [
        ("Code.exe", "", "building"),
        ("WindowsTerminal.exe", "", "building"),
        ("claude.exe", "", "building"),
        ("VALORANT-Win64-Shipping.exe", "", "gaming"),
        ("whatever.exe", "D:/SteamLibrary/steamapps/common/Game/whatever.exe", "gaming"),
        ("Discord.exe", "", "communication"),
        ("vlc.exe", "", "chill"),
        ("explorer.exe", "", "other"),
        ("brave.exe", "", None),  # its pages decide
    ],
)
def test_apps(exe: str, path: str, cat: str | None):
    assert activity.classify_app(exe, path) == cat


@pytest.mark.parametrize(
    ("url", "title", "ctype", "cat"),
    [
        # Learning on YouTube is research, not chill.
        (
            "https://www.youtube.com/watch?v=1",
            "How Claude agents use RAG - deep dive",
            "video",
            "research",
        ),
        ("https://www.youtube.com/watch?v=2", "31 Ways to Use Claude in 2027", "video", "research"),
        (
            "https://www.youtube.com/watch?v=3",
            "Artist - Song (Official Music Video)",
            "video",
            "chill",
        ),
        ("https://www.youtube.com/watch?v=4", "SHOW S2 EP8 ft. Guests", "video", "chill"),
        ("https://www.youtube.com/shorts/x", "anything", "video", "chill"),
        ("https://www.youtube.com/watch?v=5", "Monday", "video", None),  # unsure: ask the model
        ("https://music.youtube.com/watch?v=6", "a song", "video", "chill"),
        ("https://github.com/me/repo", "repo", "github_repo", "building"),
        ("http://localhost:3000/", "my app", "other", "building"),
        ("http://192.168.1.20:3000/", "my app", "other", "building"),
        ("https://stackoverflow.com/q/1", "error", "other", "research"),
        ("https://claude.ai/chat/1", "chat", "chat", "research"),
        (
            "https://some-anime-site.example/watch/monster-27",
            "Monster Episode 27",
            "other",
            "chill",
        ),
        ("https://www.netflix.com/watch/1", "show", "video", "chill"),
        ("https://www.amazon.jobs/en/jobs/1", "SDE role", "other", "research"),
        ("https://mail.google.com/mail/u/0", "Inbox", "mail", "communication"),
        ("https://example.com/", "a page", "other", "other"),
    ],
)
def test_pages(url: str, title: str, ctype: str, cat: str | None):
    assert activity.classify_page(url, title, ctype)[0] == cat


def test_the_models_verdict_is_used_and_cached(tmp_path: Path):
    cache: dict[str, Any] = {}

    def responder(system: str, user: str, schema: Any) -> Any:
        return {"items": [{"i": 1, "kind": "learning", "topic": "REA on Claude"}]}

    assert activity.classify_titles(FakeProvider(responder), ["Monday"], cache) == 1
    assert (
        activity.classify_page("https://www.youtube.com/watch?v=5", "Monday", "video", cache)[0]
        == "research"
    )
    activity.save_cache(tmp_path, cache)
    assert activity.load_cache(tmp_path) == cache
    assert activity.classify_titles(FakeProvider(responder), ["Monday"], cache) == 0  # asked once


def test_a_broken_model_leaves_titles_unknown():
    assert activity.classify_titles(FakeProvider(lambda *_a: "nonsense"), ["Monday"], {}) == 0


def _event(
    conn: Any,
    source: str,
    typ: str,
    start: Any,
    minutes: float,
    payload: dict[str, Any],
    project: str | None = None,
) -> None:
    from personal_os.core.clock import to_ms

    s = to_ms(start)
    conn.execute(
        "INSERT INTO events (id, source, type, ts_start, ts_end, project_id, privacy_class, retention, "
        "observer_version, payload, inserted_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'normal', 'default', 't', ?, ?, ?)",
        (
            f"{source}-{s}-{typ}-{uuid.uuid4().hex[:8]}",
            source,
            typ,
            s,
            s + int(minutes * 60_000),
            project,
            json.dumps(payload),
            s,
            s,
        ),
    )


def test_breakdown_splits_browser_time_by_page_and_counts_idle(conn: Any):
    _event(conn, "window", "focus", T0, 30, {"exe": "Code.exe", "app": "VS Code"}, "personal-os")
    _event(
        conn,
        "window",
        "focus",
        T0 + timedelta(minutes=40),
        20,
        {"exe": "brave.exe", "app": "Brave"},
    )
    _event(
        conn,
        "browser",
        "page_visit",
        T0 + timedelta(minutes=40),
        0,
        {
            "url": "https://www.youtube.com/watch?v=a",
            "title": "Song (Official Video)",
            "active_dwell_ms": 10 * 60_000,
        },
    )
    _event(conn, "browser", "page_visit", T0 + timedelta(minutes=50), 0,
           {"url": "https://www.youtube.com/watch?v=b", "title": "RAG tutorial for beginners", "active_dwell_ms": 10 * 60_000})  # fmt: skip
    b = activity.breakdown(conn, T0 - timedelta(hours=1), T0 + timedelta(hours=2))
    mins = {k: v // 60_000 for k, v in b.by_category.items()}
    assert mins == {"building": 30, "chill": 10, "research": 10, "idle": 10}
    assert b.by_project == {"personal-os": 30 * 60_000}
    assert round(b.productive_share, 2) == round(40 / 50, 2)
    assert [r["title"] for r in b.research] == ["RAG tutorial for beginners"]


def test_overlapping_pages_never_exceed_browser_time(conn: Any):
    _event(conn, "window", "focus", T0, 10, {"exe": "brave.exe"})
    for n in range(3):  # 30 min of page time inside 10 min of browser focus
        _event(conn, "browser", "page_visit", T0, 0,
               {"url": f"https://example.com/{n}", "title": "x", "active_dwell_ms": 10 * 60_000})  # fmt: skip
    b = activity.breakdown(conn, T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert b.active_ms == 10 * 60_000
