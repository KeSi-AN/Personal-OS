"""Projects you curate (phase 9k): sources, notes, to-dos, the inbox, resume, and what they
teach the tracker. A fake browser stands in for the extension."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from personal_os.actions.base import Origin
from personal_os.browser.native_host import Host
from personal_os.config.schema import Config
from personal_os.core.clock import SimClock
from personal_os.projects import items
from personal_os.projects.registry import add_project
from tests.conftest import T0
from tests.test_actions import Env
from tests.test_browser_control import ok

pytest_plugins = ["tests.test_browser_control"]


def project(env: Env, name: str = "Personal OS") -> str:
    return add_project(env.conn, env.clock, name, [], []).id


TABS = [
    {"id": 1, "index": 0, "window": 1, "title": "Agent design", "url": "https://github.com/me/personal-os/issues/9", "active": True, "groupTitle": "Personal OS"},
    {"id": 2, "index": 1, "window": 1, "title": "MV3 docs", "url": "https://developer.chrome.com/docs/extensions", "groupTitle": "Personal OS"},
    {"id": 3, "index": 2, "window": 1, "title": "Lo-fi - YouTube", "url": "https://www.youtube.com/watch?v=x", "groupTitle": ""},
]  # fmt: skip


# --- the store ------------------------------------------------------------------------------


def test_links_are_kept_once_and_teach_the_tracker(home: Path, config: Config):
    env = Env(home, config)
    pid = project(env)
    a = items.add(
        env.conn, env.clock, pid, "link", url="https://github.com/me/personal-os", title="repo"
    )
    b = items.add(
        env.conn, env.clock, pid, "link", url="https://github.com/me/personal-os", body="the repo"
    )
    assert a == b and items.get(env.conn, a).body == "the repo"  # type: ignore[union-attr]
    items.add(env.conn, env.clock, pid, "tab", url="https://www.youtube.com/watch?v=x")
    items.add(env.conn, env.clock, pid, "tab", url="https://developer.chrome.com/docs")
    sql = "SELECT kind, key FROM assignment_rules WHERE project_id = ?"
    rules = {tuple(r) for r in env.conn.execute(sql, (pid,))}
    assert ("github", "me/personal-os") in rules
    assert ("domain", "developer.chrome.com") in rules
    assert not any(k == "youtube.com" for _, k in rules)  # a common site never claims a project


def test_todos(home: Path, config: Config):
    env = Env(home, config)
    pid = project(env)
    items.add(env.conn, env.clock, pid, "todo", title="fix popup timing")
    items.add(env.conn, env.clock, None, "todo", title="buy milk", due=T0 + timedelta(hours=2))
    assert [t.title for t in items.todos(env.conn)] == ["buy milk", "fix popup timing"]  # due first
    t = items.find_todo(env.conn, "popup")
    assert t is not None and items.set_done(env.conn, env.clock, t.id)
    assert [t.title for t in items.todos(env.conn)] == ["buy milk"]
    assert "todos" not in items.counts(env.conn).get(pid, {})  # done ones aren't counted


def test_tab_groups_named_like_a_project_sync(home: Path, config: Config):
    env = Env(home, config)
    pid = project(env)
    assert items.sync_groups(env.conn, env.clock, TABS) == 2
    assert items.sync_groups(env.conn, env.clock, TABS) == 2  # refreshed, not duplicated
    got = items.items(env.conn, pid, ("tab",))
    assert (
        sorted(i.title for i in got) == ["Agent design", "MV3 docs"] and got[0].grp == "Personal OS"
    )


def test_resolve_by_close_name(home: Path, config: Config):
    env = Env(home, config)
    project(env, "Portfolio Site")
    assert items.resolve(env.conn, "portfolio site").name == "Portfolio Site"  # type: ignore[union-attr]
    assert items.resolve(env.conn, "portfolo site").name == "Portfolio Site"  # type: ignore[union-attr]
    assert items.resolve(env.conn, "something else") is None


# --- the tools ------------------------------------------------------------------------------


def test_add_current_tab_and_tab_group(home: Path, config: Config, browser):
    browser(lambda op, args: ok(TABS))
    env = Env(home, config)
    pid = project(env)
    o = env.ex.request("project_add_source", {"project": "personal os"}, origin=Origin.USER)
    assert o.ok and o.message == 'Added 1 source to "Personal OS"'
    o = env.ex.request(
        "project_add_source",
        {"project": "Personal OS", "what": "tab_group", "which": "personal os"},
        origin=Origin.USER,
    )
    assert o.ok and o.message == 'Added 2 sources to "Personal OS"'
    assert len(items.items(env.conn, pid, ("tab",))) == 2  # the current tab was in the group too
    o = env.ex.request("project_add_source", {"project": "Nope"}, origin=Origin.USER)
    assert not o.ok and 'no project called "Nope"' in o.message


def test_notes_todos_and_listing(home: Path, config: Config):
    env = Env(home, config)
    project(env)
    assert env.ex.request(
        "project_note",
        {"project": "Personal OS", "text": "popup needs a bigger card"},
        origin=Origin.USER,
    ).ok
    o = env.ex.request(
        "todo_add",
        {"text": "fix popup timing", "project": "Personal OS", "at": "5pm"},
        origin=Origin.USER,
    )
    assert o.ok and "due at 17:00" in o.message
    o = env.ex.request("project_items", {"project": "Personal OS"}, origin=Origin.USER)
    assert o.ok and "- [ ] fix popup timing" in str(o.result.data)  # type: ignore[union-attr]
    assert "note: popup needs a bigger card" in str(o.result.data)  # type: ignore[union-attr]
    done = env.ex.request("todo_done", {"which": "popup"}, origin=Origin.USER)
    assert done.ok and items.todos(env.conn) == []
    assert env.ex.undo(done.action_id).ok and len(items.todos(env.conn)) == 1


def test_file_the_inbox(home: Path, config: Config):
    env = Env(home, config)
    pid = project(env)
    items.add(
        env.conn,
        env.clock,
        None,
        "link",
        url="https://docs.python.org/3/",
        title="Python docs",
        source="extension",
    )
    items.add(
        env.conn,
        env.clock,
        None,
        "link",
        url="https://example.com/x",
        title="Other",
        source="extension",
    )
    o = env.ex.request(
        "project_file_inbox", {"project": "Personal OS", "which": "python"}, origin=Origin.USER
    )
    assert o.ok and [i.title for i in items.items(env.conn, pid)] == ["Python docs"]
    assert [i.title for i in items.items(env.conn, None)] == ["Other"]


def test_resume_reopens_sources_as_a_group(home: Path, config: Config, browser):
    b = browser(lambda op, args: ok({"opened": len(args.get("urls", []))}))
    env = Env(home, config)
    pid = project(env)
    items.add(
        env.conn, env.clock, pid, "tab", url="https://github.com/me/personal-os", title="repo"
    )
    items.add(env.conn, env.clock, pid, "note", body="next: the dashboard")
    items.add(env.conn, env.clock, pid, "todo", title="write tests")
    o = env.ex.request("project_resume", {"project": "Personal OS"}, origin=Origin.USER)
    assert o.ok and o.message == (
        'Reopened 1 tab for "Personal OS". Last note: next: the dashboard. Open to-dos: write tests'
    )
    assert b.seen == [
        ("tabs.open_group", {"urls": ["https://github.com/me/personal-os"], "title": "Personal OS"})
    ]


# --- from the browser's right-click menu ------------------------------------------------------


def test_add_to_project_from_the_browser_lands_in_the_inbox(home: Path, config: Config):
    host = Host(home, "brave")
    assert host.handle({"type": "inbox", "url": "https://docs.python.org/3/", "title": "Docs"})[
        "ok"
    ]
    host.handle({"type": "inbox", "url": "javascript:alert(1)", "title": "x"})  # never kept
    from personal_os.daemon import Daemon

    env = Env(home, config)
    fake = SimpleNamespace(config=env.config, control_conn=env.conn, clock=SimClock(T0))
    Daemon._sync_project_sources(fake)  # type: ignore[arg-type]
    assert [(i.title, i.url) for i in items.items(env.conn, None)] == [
        ("Docs", "https://docs.python.org/3/")
    ]
    assert list((env.config.runtime_dir / "browser" / "inbox").iterdir()) == []


# --- your data controls ---------------------------------------------------------------------


def test_export_delete_and_forget_cover_project_items(home: Path, config: Config, tmp_path: Path):
    from personal_os.storage.export import TABLES, delete_project_data

    assert "project_items" in TABLES
    env = Env(home, config)
    pid = project(env)
    items.add(env.conn, env.clock, pid, "note", body="secret plan")
    assert delete_project_data(env.conn, pid)["project_items"] == 1
    items.add(env.conn, env.clock, None, "note", body="inbox note")
    from personal_os.storage.maintenance import forget

    forget(env.conn, T0 - timedelta(minutes=1), T0 + timedelta(minutes=1), env.clock, marker=False)
    assert items.items(env.conn, None) == []
