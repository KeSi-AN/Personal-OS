"""Research worth keeping: unclaimed learning becomes an R&D project (or joins one)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from personal_os.config.schema import Config
from personal_os.llm.fake import FakeProvider
from personal_os.projects import items, research
from personal_os.projects.registry import add_project, load_projects
from tests.test_actions import Env

M = 60_000


def r(
    title: str, url: str, minutes: int, topic: str = "", project: str | None = None
) -> dict[str, Any]:
    return {
        "title": title,
        "url": url,
        "ms": minutes * M,
        "topic": topic,
        "project": project,
        "at": 0,
    }


def test_unclaimed_learning_becomes_an_rnd_project(home: Path, config: Config):
    env = Env(home, config)
    got = research.keep_research(env.conn, env.clock, [
        r("REA with Claude, part 1", "https://youtu.be/1", 12, "REA on Claude"),
        r("REA with Claude, part 2", "https://youtu.be/2", 9, "REA on Claude"),
        r("Quick lookup", "https://example.com/q", 3, "Excel tips"),  # a short page: not kept
        r("Chordus fix", "https://youtu.be/4", 30, "Chords", project="chordus"),  # already claimed
    ])  # fmt: skip
    assert [(k.project, k.created, k.added, k.minutes) for k in got] == [
        ("R&D: Rea On Claude", True, 2, 21)
    ]
    pid = next(p.id for p in load_projects(env.conn) if p.name == "R&D: Rea On Claude")
    links = items.items(env.conn, pid, ("link",))
    assert {i.url for i in links} == {"https://youtu.be/1", "https://youtu.be/2"}
    assert {i.source for i in links} == {"auto"}


def test_a_matching_project_gets_the_links_instead(home: Path, config: Config):
    env = Env(home, config)
    p = add_project(env.conn, env.clock, "Claude Agents", [], ["claude agents"])
    got = research.keep_research(env.conn, env.clock, [
        r("Agents 101", "https://youtu.be/a", 15, "Claude agents"),
    ])  # fmt: skip
    assert [(k.project, k.created) for k in got] == [("Claude Agents", False)]
    assert len(items.items(env.conn, p.id, ("link",))) == 1
    again = research.keep_research(
        env.conn, env.clock, [r("Agents 101", "https://youtu.be/a", 15, "Claude agents")]
    )
    assert again == []  # nothing new: nothing reported


def test_titles_without_a_topic_ask_the_model(home: Path, config: Config):
    env = Env(home, config)

    def responder(system: str, user: str, schema: Any) -> Any:
        return {"items": [{"i": 1, "topic": "3D printing"}, {"i": 2, "topic": "3D printing"}]}

    got = research.keep_research(env.conn, env.clock, [
        r("Slicer settings explained", "https://example.com/a", 6),
        r("Bed levelling guide", "https://example.com/b", 6),
    ], FakeProvider(responder))  # fmt: skip
    assert [k.project for k in got] == ["R&D: 3D Printing"]


def test_a_short_video_is_noted_and_donna_decides(home: Path, config: Config):
    """'If I open a video to come back to later and watch under 10 min, it should still count.'"""
    env = Env(home, config)
    asked: list[str] = []

    def responder(system: str, user: str, schema: Any) -> Any:
        asked.append(user)
        return {"items": [{"i": 1, "keep": True}, {"i": 2, "keep": False}]}

    got = research.keep_research(env.conn, env.clock, [
        r("Agentic RAG explained (watch later)", "https://youtu.be/x", 2, "Agentic RAG"),
        r("How to fix pip error", "https://stackoverflow.com/q/1", 2, "Pip errors"),
        r("Barely opened", "https://youtu.be/y", 0, "Something"),  # under a minute: not noted
    ], FakeProvider(responder))  # fmt: skip
    assert [k.project for k in got] == ["R&D: Agentic Rag"]
    assert "Barely opened" not in asked[0]


def test_without_the_model_videos_are_kept_and_pages_are_not(home: Path, config: Config):
    env = Env(home, config)
    got = research.keep_research(env.conn, env.clock, [
        r("Talk on agents", "https://www.youtube.com/watch?v=t", 3, "Agents talk"),
        r("A docs page", "https://docs.example.com/x", 3, "Some docs"),
    ])  # fmt: skip
    assert [k.project for k in got] == ["R&D: Agents Talk"]
