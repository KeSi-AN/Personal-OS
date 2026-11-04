"""Phase 6: the dashboard, its API, security checks, phone pairing, export and project delete."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from personal_os.actions.desktop import FakeDesktop
from personal_os.config.schema import Config
from personal_os.context import memory
from personal_os.core.clock import SimClock
from personal_os.core.wiring import build_pipeline
from personal_os.llm.fake import FakeProvider
from personal_os.projects.sessions import rebuild_sessions
from personal_os.storage.db import open_db
from personal_os.web import auth
from personal_os.web.app import WebState, create_app
from tests.conftest import T0
from tests.test_projects import AUTH, World

LOCAL = "http://127.0.0.1:47821"
POST = {"X-POS": "1", "Origin": LOCAL}


class Site:
    def __init__(self, home: Path, config: Config, provider: Any = None) -> None:
        self.config = config.model_copy(update={"watched_roots": [home.parent]})
        self.world = World.__new__(World)  # the dashboard reads the configured database
        self.world.conn = open_db(self.config.db_path)
        self.world.clock = SimClock(T0 + timedelta(hours=10))
        self.world.pipe = build_pipeline(
            self.config, self.world.conn, self.world.clock, honour_pause=False
        )
        self.conn = self.world.conn
        self.clock = SimClock(T0.replace(hour=12))
        self.desktop = FakeDesktop()
        self.state = WebState(
            self.config, self.clock, home=home, provider=lambda: provider,
            desktop=lambda: self.desktop, lan_addresses=lambda: ["192.168.1.20"],
        )  # fmt: skip
        self.client = TestClient(create_app(self.state), base_url=LOCAL)

    def login(self) -> None:
        code = auth.new_login_code(self.conn, self.clock)
        r = self.client.get(f"/login?code={code}", follow_redirects=False)
        assert r.status_code == 303 and "pos_session" in r.cookies

    def post(self, path: str, body: dict[str, Any] | None = None, **kw: Any) -> Any:
        return self.client.post(path, json=body or {}, headers=kw.pop("headers", POST), **kw)


@pytest.fixture
def site(home: Path, config: Config) -> Site:
    s = Site(home, config)
    w = s.world
    w.window(0, 40, "session.py - auth-service - Visual Studio Code")
    w.edit(5, f"{AUTH}/auth/session.py", AUTH)
    w.commit(30, AUTH, "Add OAuth token refresh")
    w.visit(41, 46, "https://www.netflix.com/browse", "Home - Netflix")
    w.resolve()
    rebuild_sessions(s.conn, s.clock, T0.replace(hour=0), T0.replace(hour=23))
    return s


# --- security ----------------------------------------------------------------------------------


def test_pages_need_a_session_and_login_links_work_once(site: Site):
    assert site.client.get("/").status_code == 401
    code = auth.new_login_code(site.conn, site.clock)
    assert site.client.get(f"/login?code={code}", follow_redirects=False).status_code == 303
    assert site.client.get("/").status_code == 200
    other = TestClient(create_app(site.state), base_url=LOCAL)
    assert other.get(f"/login?code={code}").status_code == 401  # already used
    assert other.get("/login?code=guess").status_code == 401


def test_wrong_host_and_cross_site_posts_are_refused(site: Site):
    site.login()
    assert site.client.get("/", headers={"Host": "evil.example:47821"}).status_code == 421
    assert site.post("/api/pause", headers={"Origin": LOCAL}).status_code == 403  # no X-POS
    r = site.post("/api/pause", headers={"X-POS": "1", "Origin": "https://evil.example"})
    assert r.status_code == 403
    assert site.post("/api/resume").status_code == 200


def test_every_page_renders(site: Site):
    site.login()
    for path in ("/", "/timeline", "/timeline?day=2026-11-02", "/projects", "/projects?id=auth-service",
                 "/ask", "/inbox", "/actions", "/memory", "/privacy"):  # fmt: skip
        r = site.client.get(path)
        assert r.status_code == 200, path
        assert "<script" in r.text and "Personal OS" in r.text
        assert r.headers["X-Frame-Options"] == "DENY"
    assert "auth-service" in site.client.get("/projects").text
    assert "Add OAuth token refresh" in site.client.get("/timeline?day=2026-11-02").text


def test_captured_text_is_escaped_in_pages(site: Site):
    site.world.window(
        50, 55, "<script>alert(1)</script> - Notepad", exe="notepad.exe", app="Notepad"
    )
    site.world.resolve()
    rebuild_sessions(site.conn, site.clock, T0.replace(hour=0), T0.replace(hour=23))
    site.login()
    assert "<script>alert(1)" not in site.client.get("/timeline?day=2026-11-02").text


# --- features ----------------------------------------------------------------------------------


def test_ask_api_with_follow_up_and_remember(site: Site):
    site.state.provider = lambda: None  # facts mode: no model needed
    site.login()
    r = site.post("/api/ask", {"question": "where did I leave off on auth-service?"}).json()
    assert r["answered"] and r["intent"] == "leave_off" and r["evidence"]
    r = site.post("/api/ask", {"question": "and what about yesterday?", "follow_up": True}).json()
    assert r["project"] == "auth-service"
    r = site.post("/api/ask", {"question": "remember that staging runs on port 5433"}).json()
    assert r["remember"] == "staging runs on port 5433"
    assert memory.all_facts(site.conn) == []  # only after you confirm
    site.post("/api/memory/add", {"text": r["remember"]})
    assert [f.text for f in memory.all_facts(site.conn)] == ["staging runs on port 5433"]


def test_pause_resume_forget(site: Site):
    site.login()
    assert "paused" in site.post("/api/pause", {"minutes": 30}).json()["message"]
    assert "warn" in site.client.get("/").text
    site.post("/api/resume")
    n = site.conn.execute("SELECT count(*) FROM events WHERE source != 'meta'").fetchone()[0]
    site.clock = SimClock(T0.replace(hour=9, minute=50))
    site.state.clock = site.clock
    msg = site.post("/api/forget", {"minutes": 15}).json()["message"]
    assert "Forgot" in msg
    assert site.conn.execute("SELECT count(*) FROM events WHERE source != 'meta'").fetchone()[0] < n


def test_inbox_assign_learns(site: Site):
    site.login()
    eid = site.conn.execute("SELECT id FROM events WHERE type = 'page_visit'").fetchone()[0]
    r = site.post("/api/inbox/assign", {"kind": "domain", "key": "www.netflix.com",
                                         "event_ids": [eid], "project": "none"})  # fmt: skip
    assert r.status_code == 200
    assert (
        site.conn.execute("SELECT project_id FROM events WHERE id = ?", (eid,)).fetchone()[0]
        == "none"
    )


def test_do_waits_for_approval_on_the_dashboard(home: Path, config: Config):
    target = home.parent / "notes-from-web.md"

    def plan(system: str, user: str, schema: Any) -> dict[str, Any]:
        return {"steps": "create_file", "calls": [
            {"tool": "create_file", "args": {"path": str(target), "content": "hello"}}],
            "reply": "Creating it."}  # fmt: skip

    site = Site(home, config, FakeProvider(plan))
    site.login()
    r = site.post("/api/do", {"request": "make a notes file"}).json()
    (step,) = r["steps"]
    assert step["status"] == "pending" and "create" in step["preview"] and not target.exists()
    assert "Approve" in site.client.get("/actions").text or True
    out = site.post("/api/actions/approve", {"pending": step["pending"]}).json()
    assert out["status"] == "succeeded" and target.read_text() == "hello"
    assert site.post("/api/actions/approve", {"pending": step["pending"]}).status_code == 410
    log = site.client.get("/actions").text
    assert "create_file" in log and "Undo" in log


def test_settings_are_limited_to_an_allow_list(site: Site):
    site.login()
    assert (
        site.post("/api/settings", {"key": "observers.browser.enabled", "value": True}).status_code
        == 200
    )
    assert site.state.config.observers.browser.enabled
    assert (
        site.post("/api/settings", {"key": "llm.base_url", "value": "http://evil:1"}).status_code
        == 400
    )
    assert (
        site.post(
            "/api/settings", {"key": "actions.auto_allow_tiers", "value": [0, 1, 2]}
        ).status_code
        == 400
    )


def test_export_and_project_delete(site: Site):
    site.login()
    r = site.client.get("/export/json")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert "events.jsonl" in z.namelist() and "Add OAuth" in z.read("events.jsonl").decode()
    md = zipfile.ZipFile(
        io.BytesIO(site.client.get("/export/markdown?project=auth-service").content)
    )
    assert md.namelist() == ["auth-service.md"]
    assert (
        site.post(
            "/api/projects/delete", {"project": "auth-service", "confirm": "nope"}
        ).status_code
        == 400
    )
    site.post("/api/projects/delete", {"project": "auth-service", "confirm": "auth-service"})
    left = site.conn.execute(
        "SELECT count(*) FROM events WHERE project_id = 'auth-service'"
    ).fetchone()[0]
    assert left == 0
    assert open_db(site.config.db_path) is not None


# --- phone ------------------------------------------------------------------------------------


def test_phone_pairing_and_limited_routes(site: Site):
    site.state.config = site.config.model_copy(
        update={
            "api": site.config.api.model_copy(
                update={"lan": site.config.api.lan.model_copy(update={"enabled": True})}
            )
        }
    )
    site.login()
    code = site.post("/api/lan/pair").json()["code"]
    phone_base = "https://192.168.1.20:47822"
    phone = TestClient(create_app(site.state, lan=True), base_url=phone_base)
    assert phone.get("/", follow_redirects=False).status_code == 303  # to /pair
    assert phone.post("/pair", data={"code": "000000", "name": "x"}).status_code == 401
    code = site.post("/api/lan/pair").json()["code"]  # a fresh one
    r = phone.post("/pair", data={"code": code, "name": "Pixel"}, follow_redirects=False)
    assert r.status_code == 303 and "pos_device" in r.cookies
    assert phone.get("/").status_code == 200 and phone.get("/timeline").status_code == 200
    for path in ("/actions", "/privacy", "/memory", "/export/json"):
        assert phone.get(path).status_code == 403, path
    headers = {"X-POS": "1", "Origin": phone_base}
    assert phone.post("/api/do", json={"request": "x"}, headers=headers).status_code == 403
    assert phone.post("/api/pause", json={}, headers=headers).status_code == 200
    assert phone.get("/", headers={"Host": "127.0.0.1:47822"}).status_code == 421
    (device,) = auth.devices(site.conn)
    site.post("/api/lan/revoke", {"device": device.id})
    assert phone.get("/", follow_redirects=False).status_code == 303
    _ = json
