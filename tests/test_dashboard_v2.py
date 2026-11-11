"""Dashboard v2 (phase 9l): pages, endpoints, escaping and the phone's read-only view."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from personal_os.config.schema import Config
from personal_os.llm.fake import FakeProvider
from personal_os.projects import items
from personal_os.projects.registry import add_project, load_projects
from personal_os.web.app import create_app
from tests.test_web import POST, Site, site  # noqa: F401  (fixture)

PAGES = ["/", "/p", "/donna", "/notes", "/settings", "/old"]


def test_every_v2_page_renders_and_needs_a_session(site: Site):  # noqa: F811
    for path in PAGES:
        assert site.client.get(path).status_code == 401
    site.login()
    for path in PAGES:
        r = site.client.get(path)
        assert r.status_code == 200, path
    pid = next(p.id for p in load_projects(site.conn) if p.id != "none")
    r = site.client.get(f"/p/{pid}")
    assert r.status_code == 200 and "Sources" in r.text and "Resume" in r.text
    assert site.client.get("/p/no-such-project").status_code == 404


def test_today_shows_time_by_project(site: Site):  # noqa: F811
    site.login()
    r = site.client.get("/")
    assert "Today" in r.text and "Timeline" in r.text and "auth-service" in r.text


def test_your_text_is_escaped(site: Site):  # noqa: F811
    site.login()
    p = add_project(site.conn, site.clock, "Evil", [], [])
    items.add(site.conn, site.clock, p.id, "note", body="<script>alert(1)</script>")
    items.add(
        site.conn, site.clock, p.id, "link", url="https://a.example/", title="<img src=x onerror=1>"
    )
    page = site.client.get(f"/p/{p.id}").text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    assert "<img src=x" not in page


def test_notes_todos_links_through_the_api(site: Site):  # noqa: F811
    site.login()
    p = add_project(site.conn, site.clock, "Portfolio", [], [])
    assert (
        site.post(
            "/api/v2/items", {"project_id": p.id, "kind": "note", "body": "hero image"}
        ).status_code
        == 200
    )
    assert (
        site.post(
            "/api/v2/items", {"project_id": p.id, "kind": "todo", "title": "ship it"}
        ).status_code
        == 200
    )
    assert (
        site.post(
            "/api/v2/items", {"project_id": p.id, "kind": "link", "url": "javascript:x"}
        ).status_code
        == 400
    )
    assert (
        site.post(
            "/api/v2/items", {"project_id": p.id, "kind": "link", "url": "https://a.example/"}
        ).status_code
        == 200
    )
    assert (
        site.post("/api/v2/items", {"project_id": "nope", "kind": "note", "body": "x"}).status_code
        == 404
    )
    todo = items.todos(site.conn)[0]
    assert site.post(f"/api/v2/items/{todo.id}/done", {"done": True}).status_code == 200
    assert items.todos(site.conn) == []
    inbox_id = items.add(
        site.conn, site.clock, None, "link", url="https://b.example/", title="later"
    )
    assert site.post(f"/api/v2/items/{inbox_id}/move", {"project_id": p.id}).status_code == 200
    assert items.get(site.conn, inbox_id).project_id == p.id  # type: ignore[union-attr]
    assert site.post(f"/api/v2/items/{inbox_id}/delete").status_code == 200
    assert items.get(site.conn, inbox_id) is None


def test_create_project(site: Site):  # noqa: F811
    site.login()
    r = site.post("/api/v2/projects", {"name": "Side quest"})
    assert r.status_code == 200 and any(p.name == "Side quest" for p in load_projects(site.conn))


def test_changes_need_the_header(site: Site):  # noqa: F811
    site.login()
    r = site.client.post(
        "/api/v2/items", json={"kind": "note", "body": "x"}, headers={"Origin": POST["Origin"]}
    )
    assert r.status_code == 403


def test_settings_only_change_what_the_page_offers(site: Site, home: Path):  # noqa: F811
    from personal_os.config.loader import load_config

    site.login()
    assert site.post("/api/v2/setting", {"key": "voice.popup", "value": False}).status_code == 200
    assert load_config(home).voice.popup is False
    assert site.post("/api/v2/setting", {"key": "voice.followup_s", "value": 8}).status_code == 200
    assert load_config(home).voice.followup_s == 8
    assert (
        site.post("/api/v2/setting", {"key": "actions.auto_allow_tiers", "value": 3}).status_code
        == 400
    )
    assert site.post("/api/v2/setting", {"key": "voice.followup_s", "value": 99}).status_code == 400


def test_chat_on_the_dashboard(home: Path, config: Config):
    def responder(system: str, user: str, schema: Any) -> Any:
        return {"decide": "answer", "calls": [], "reply": "Hello from Donna.", "details": "- one"}

    s = Site(home, config, provider=FakeProvider(responder))
    s.login()
    r = s.post("/api/v2/chat", {"text": "hi there"})
    assert (
        r.status_code == 200
        and r.json()["reply"] == "Hello from Donna."
        and r.json()["details"] == "- one"
    )
    page = s.client.get("/donna").text
    assert "hi there" in page and "Hello from Donna." in page  # the conversation is kept


def test_the_phone_sees_today_and_projects_only(site: Site):  # noqa: F811
    from personal_os.web import auth

    phone = TestClient(create_app(site.state, lan=True), base_url="http://192.168.1.20:47822")
    token = auth.add_device(site.conn, site.clock, "phone")
    phone.cookies.set("pos_device", token)
    assert phone.get("/").status_code == 200
    assert "Ask Donna" not in phone.get("/").text  # read-only there
    assert phone.get("/p").status_code == 200
    assert phone.get("/notes").status_code == 403
    assert phone.get("/settings").status_code == 403
    assert (
        phone.post(
            "/api/v2/items", json={"kind": "note", "body": "x"}, headers={"X-POS": "1"}
        ).status_code
        == 403
    )


def test_show_log_under_each_reply(home: Path, config: Config):
    import json as _json

    calls = iter(
        [
            {
                "decide": "act",
                "calls": [{"tool": "no_such_tool", "args": {}}],
                "reply": "",
                "details": "",
            },
            {"decide": "answer", "calls": [], "reply": "Done <b>it</b>.", "details": ""},
        ]
    )

    def responder(system: str, user: str, schema: Any) -> Any:
        return next(calls, {"decide": "answer", "calls": [], "reply": "Done.", "details": ""})

    s = Site(home, config, provider=FakeProvider(responder))
    s.login()
    r = s.post("/api/v2/chat", {"text": "do the thing"})
    assert r.status_code == 200 and any("step 1" in t for t in r.json()["trace"])
    logs = config.data_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    at = s.clock.now().isoformat()
    (logs / "personal_os.log").write_text(
        _json.dumps({"ts": at, "level": "WARNING", "logger": "x.agent", "msg": "agent step unusable", "error": "<bad>"})
        + "\n" + _json.dumps({"ts": at, "level": "INFO", "logger": "x", "msg": "fine"}) + "\n",
        "utf-8",
    )  # fmt: skip
    page = s.client.get("/donna").text
    assert "Show log" in page and "route: agent" in page and "step 1" in page
    assert "agent step unusable" in page and "&lt;bad&gt;" in page and "<bad>" not in page
    assert "fine" not in page.split("Show log", 1)[1]  # info lines are left out
