"""The 24-hour brief: the page and its charts, the startup step, safe login redirects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from personal_os.config.schema import Config
from personal_os.context import activity
from personal_os.core import modes
from personal_os.web import auth, brief
from tests.test_web import POST, Site, site  # noqa: F401  (fixture)


def test_the_brief_renders_with_charts(site: Site):  # noqa: F811
    site.login()
    r = site.client.get("/brief")
    assert r.status_code == 200
    page = r.text
    assert "Where the time went" in page and "Hour by hour" in page and "<svg" in page
    assert ("Productive" in page and "Coding &amp; building" in page) or "Coding & building" in page
    assert "/brief" in site.client.get("/").text  # linked from Today


def test_charts():
    b = activity.Breakdown(
        start=__import__("datetime").datetime(2026, 11, 11),
        end=__import__("datetime").datetime(2026, 11, 12),
    )
    b.by_category.update({"building": 60 * 60_000, "chill": 30 * 60_000, "idle": 10 * 60_000})
    svg = brief.donut(dict(b.by_category))
    assert (svg.count("<circle") == 3 and "1h 30m" in svg) or "1h30m" in svg or "90" in svg
    assert (
        "Coding &amp; building" not in brief.legend({})
        and brief.legend(dict(b.by_category)).count("class='row'") == 3
    )
    assert "a light day" in brief.narrative(b, {}) or "%" in brief.narrative(b, {})


def test_login_goes_only_to_pages_of_this_dashboard(site: Site):  # noqa: F811
    for nxt, where in [
        ("/brief", "/brief"),
        ("//evil.example/x", "/"),
        ("https://evil.example", "/"),
        (r"/\evil", "/"),
    ]:
        code = auth.new_login_code(site.conn, site.clock)
        r = site.client.get(f"/login?code={code}&next={nxt}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == where, nxt


def test_mode_buttons(site: Site):  # noqa: F811
    site.login()
    assert site.post("/api/v2/mode", {"mode": "leisure"}).status_code == 200
    assert modes.current(site.config.runtime_dir) == "chill"
    assert site.post("/api/v2/mode", {"mode": "party"}).status_code == 400
    assert "Mode:" in site.client.get("/").text


def test_prepare_judges_videos_and_keeps_research(home: Path, config: Config, monkeypatch: Any):
    from personal_os.core.clock import SimClock
    from tests.conftest import T0

    b = activity.Breakdown(T0, T0)
    b.unknown_titles = ["Monday"]
    b.research = [
        {
            "title": "REA part 1",
            "url": "https://youtu.be/1",
            "ms": 15 * 60_000,
            "topic": "REA",
            "project": None,
            "at": 0,
        }
    ]
    monkeypatch.setattr(activity, "breakdown", lambda *_a, **_k: b)
    asked: list[list[str]] = []
    monkeypatch.setattr(
        activity, "classify_titles", lambda _p, titles, _c: asked.append(titles) or 0
    )
    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    out = brief.prepare(config, object(), SimClock(T0))
    assert "error" not in out, out
    assert asked == [["Monday"]]
    assert [k["project"] for k in out["kept"]] == ["R&D: Rea"] and out["kept"][0]["created"]


def test_opened_at_most_every_few_hours(config: Config):
    from personal_os.core.clock import SimClock
    from tests.conftest import T0

    config.runtime_dir.mkdir(parents=True, exist_ok=True)
    clock = SimClock(T0)
    assert brief.due(config, clock)
    brief.mark_opened(config, clock)
    clock.advance(2 * 3600)
    assert not brief.due(config, clock)
    clock.advance(5 * 3600)
    assert brief.due(config, clock)
    off = config.model_copy(update={"brief": config.brief.model_copy(update={"on_start": False})})
    assert not brief.due(off, clock)
