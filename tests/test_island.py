"""The island (ADR-082): when it opens, on what page, how big; the voice commands; the cards'
data; and the voice popup's behaviour carried over (an auto-close ends the turn, not a page
you asked for). Nothing here opens a window."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from personal_os.config.schema import Config
from personal_os.interfaces import island, island_data
from personal_os.interfaces import island_logic as L
from tests.conftest import T0
from tests.test_actions import Env
from tests.test_phase7 import voice_for
from tests.test_projects import AUTH, World


def inputs(**over: object) -> L.Inputs:
    base = dict(
        turn_active=False, alerts=0, hovered_s=None, left_s=None, request=None,
        request_age_s=99.0, pinned=False, typing=False, fullscreen=False, music=False,
        voice_running=True, idle_pill=True,
    )  # fmt: skip
    base.update(over)
    return L.Inputs(**base)  # type: ignore[arg-type]


# --- deciding -------------------------------------------------------------------------------


def test_by_default_nothing_shows_until_donna_is_active():
    """You asked for it only when called or active, not always at the top (16 Nov)."""
    assert L.decide(inputs(idle_pill=False), "today", None)[0].mode == "hidden"
    assert L.decide(inputs(idle_pill=False, turn_active=True), "today", None)[0].mode == "open"
    asked = inputs(idle_pill=False, request="projects", request_age_s=1)
    assert L.decide(asked, "today", None)[0].mode == "open"


def test_with_the_pill_kept_idle_is_a_pill_and_a_full_screen_app_hides_it():
    assert L.decide(inputs(), "today", None)[0].mode == "pill"
    assert L.decide(inputs(fullscreen=True), "today", None)[0].mode == "hidden"


def test_resting_on_the_pill_opens_it_but_passing_over_does_not():
    assert L.decide(inputs(hovered_s=0.05), "today", None)[0].mode == "pill"
    target, by = L.decide(inputs(hovered_s=0.3), "projects", None)
    assert (target.mode, target.page, by) == ("open", "projects", "hover")  # the last page


def test_it_closes_a_moment_after_the_mouse_leaves():
    assert L.decide(inputs(left_s=0.2), "today", "hover")[0].mode == "open"
    assert L.decide(inputs(left_s=0.6), "today", "hover")[0].mode == "pill"


def test_calling_donna_opens_it_on_donna_and_you_can_swipe_away_meanwhile():
    target, by = L.decide(inputs(turn_active=True), "music", None)
    assert (target.mode, target.page, by) == ("open", "donna", "turn")
    target, _ = L.decide(inputs(turn_active=True), "projects", "turn")  # you swiped
    assert target.page == "projects"
    target, _ = L.decide(inputs(turn_active=True, fullscreen=True), "donna", None)
    assert target.mode == "open"  # you called her: shown over a full-screen app too


def test_a_page_asked_for_stays_a_while_then_goes():
    t, by = L.decide(inputs(request="projects", request_age_s=2), "today", None)
    assert (t.mode, t.page, by) == ("open", "projects", "request")
    assert (
        L.decide(inputs(request="projects", request_age_s=30), "projects", "request")[0].mode
        == "pill"
    )
    held = inputs(request="projects", request_age_s=30, hovered_s=2.0)
    assert L.decide(held, "projects", "request")[0].mode == "open"  # the mouse is on it


def test_a_due_reminder_opens_it():
    t, by = L.decide(inputs(alerts=1), "today", None)
    assert (t.mode, t.page, by) == ("open", "donna", "alert")


def test_typing_or_pin_keeps_it_open():
    assert L.decide(inputs(typing=True), "donna", "hover")[0].mode == "open"
    assert L.decide(inputs(pinned=True), "donna", "turn")[0].mode == "open"


# --- sizing and placing ---------------------------------------------------------------------


def test_sizes():
    pill = L.Target("pill", "today", "")
    assert L.size(pill, inputs(), 0) == L.PILL
    assert L.size(pill, inputs(music=True), 0) == L.PILL_WIDE
    donna = L.Target("open", "donna", "")
    assert L.size(donna, inputs(), 90) == (L.OPEN_W, L.VOICE_MIN_H)
    assert L.size(donna, inputs(), 9999) == (L.OPEN_W, L.VOICE_MAX_H)
    assert L.size(L.Target("open", "music", ""), inputs(), 0) == (L.OPEN_W, L.CARD_H)
    assert L.radius(pill, 34) == 17 and L.radius(donna, 300) == L.OPEN_RADIUS


def test_it_sits_at_the_top_centre_of_the_screen_scaled_for_its_dpi():
    assert L.place((0, 0, 1920, 1080), 1.0, 196, 34) == (862, 0, 196, 34)
    # a TV to the right, at 150%
    assert L.place((1920, 0, 5760, 2160), 1.5, 560, 250) == (1920 + (3840 - 840) // 2, 0, 840, 375)


def test_the_spring_settles_on_its_target_and_keeps_its_speed_when_the_target_moves():
    s = island.Spring(0.0)
    s.target = 100.0
    for _ in range(30):
        s.step(1 / 120)
    mid_speed = s.v
    s.target = 50.0  # a change of mind mid-way: no jump in position
    before = s.x
    s.step(1 / 120)
    assert abs(s.x - before) < abs(mid_speed) / 60 + 1
    for _ in range(600):
        s.step(1 / 120)
    assert s.settled and abs(s.x - 50.0) < 0.5


# --- over an app: the ghost pill (ADR-083) ----------------------------------------------------


def test_a_pill_over_an_app_is_a_see_through_click_through_ghost():
    """The browser's tabs under the pill couldn't be clicked (16 Nov)."""
    pill, opened = L.Target("pill", "today", ""), L.Target("open", "today", "")
    assert L.ghosted(pill, app_under=True) and not L.ghosted(pill, app_under=False)
    assert not L.ghosted(opened, app_under=True)  # open, you're using it: solid
    assert L.alpha(False, False) == 255
    assert L.alpha(True, False) == L.GHOST_ALPHA
    assert L.alpha(True, True) == L.NEAR_ALPHA < L.GHOST_ALPHA  # pointer on it: see under it


def test_a_ghost_opens_only_when_held_against_the_top_edge_a_while():
    """Resting on a tab under it (as long as a normal hover) mustn't open it."""
    assert L.decide(inputs(ghost=True, hovered_s=0.3), "today", None)[0].mode == "pill"
    target, by = L.decide(inputs(ghost=True, hovered_s=L.EDGE_OPEN_S), "notes", None)
    assert (target.mode, target.page, by) == ("open", "notes", "hover")
    assert L.decide(inputs(hovered_s=0.3), "today", None)[0].mode == "open"  # not a ghost


def test_overlap():
    assert L.overlaps((0, 0, 100, 40), (50, 0, 150, 34))
    assert not L.overlaps((0, 100, 100, 200), (0, 0, 100, 34))  # a window lower down
    assert not L.overlaps((0, 0, 50, 40), (50, 0, 150, 34))  # touching isn't overlapping


# --- voice: "show my projects" ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "page"),
    [
        ("Show my projects.", "projects"),
        ("Donna, show today", "today"),
        ("show me my day", "today"),
        ("can you show the music", "music"),
        ("open now playing", "music"),
        ("Hide the island.", "hide"),
        ("show my notes", "notes"),
        ("open the notes", "notes"),
        ("show my resume", None),  # a file: the router's job
        ("open brave", None),
    ],
)
def test_page_requests(said: str, page: str | None):
    assert L.page_request(said) == page


def test_saying_show_my_projects_opens_the_island_there(home: Path, config: Config):
    cfg = config.model_copy(update={"voice": config.voice.model_copy(update={"enabled": True})})
    env = Env(home, cfg)
    v, _h = voice_for(env)
    assert v.fast("show my projects") == "Here are your projects."
    assert island.take_page(env.config.runtime_dir) == "projects"
    assert island.take_page(env.config.runtime_dir) is None  # taken once


def test_without_the_island_show_is_left_to_the_router(home: Path, config: Config):
    v, _h = voice_for(Env(home, config))  # voice off in the test config: no island
    assert v.show_page("show my projects") is None


def test_an_old_page_request_is_ignored(tmp_path: Path):
    island.request_page(tmp_path, "today")
    assert island.take_page(tmp_path, max_age_s=-1) is None


def test_launch_starts_one_island(tmp_path: Path, no_real_island: list[str], monkeypatch):
    monkeypatch.setattr(island.popup, "unavailable_reason", lambda: None)
    cfg = Config(data_dir=tmp_path)
    assert island.launch(cfg) == "opened" and no_real_island == ["island"]


# --- the popup's behaviour, carried over ------------------------------------------------------


def test_the_turn_auto_close_ends_the_turn_but_not_a_page_you_asked_for(config: Config):
    isl = island.Island(config)
    isl.request, isl.turn_up = "projects", True
    isl.api._in_view = True
    isl.api.close()  # what the popup's auto-close calls from inside view()
    assert isl.request == "projects" and not isl.turn_up
    isl.api._in_view = False
    isl.api.close()  # ✕ or Esc
    assert isl.request is None


# --- the cards' data -----------------------------------------------------------------------


def test_today_and_projects_cards(home: Path, config: Config):
    from personal_os.actions import reminders
    from personal_os.projects import items
    from personal_os.projects.sessions import rebuild_sessions

    w = World(home, config)
    w.commit(0, AUTH, "auth work")
    w.window(0, 50, "session.py - auth-service - Visual Studio Code")
    w.resolve()
    now = T0 + timedelta(hours=2)
    rebuild_sessions(w.conn, w.clock, T0 - timedelta(hours=1), now)
    reminders.add(w.conn, w.clock, "stretch", now + timedelta(minutes=30), "a1")
    items.add(w.conn, w.clock, "auth-service", "todo", title="write the refresh test",
              due=now - timedelta(hours=1))  # fmt: skip
    t = island_data.today(w.conn, now)
    assert t["worked"] and t["projects"][0]["name"] == "auth-service"
    assert [r["text"] for r in t["reminders"]] == ["stretch"]
    assert t["todos"][0]["text"] == "write the refresh test" and t["todos"][0]["overdue"]
    (p,) = island_data.projects(w.conn, now)
    assert p["name"] == "auth-service" and p["next_todo"] == "write the refresh test"


def test_todos_can_be_ticked_off_and_back_on(home: Path, config: Config):
    """The to-dos on Today couldn't be ticked (16 Nov)."""
    from personal_os.projects import items

    env = Env(home, config)
    tid = items.add(env.conn, env.clock, None, "todo", title="water the plants")
    (shown,) = island_data.today(env.conn, env.clock.now())["todos"]
    assert shown["id"] == tid
    assert island_data.set_todo(env.conn, env.clock, tid, True)
    assert island_data.today(env.conn, env.clock.now())["todos"] == []
    assert island_data.set_todo(env.conn, env.clock, tid, False)  # a mis-click, undone
    assert [t["text"] for t in island_data.today(env.conn, env.clock.now())["todos"]] == [
        "water the plants"
    ]


def test_a_typed_todo_with_a_time_gets_it_as_its_due_time(home: Path, config: Config):
    from personal_os.projects import items

    env = Env(home, config)
    assert island_data.add_todo(env.conn, env.clock, "call mum tomorrow at 5pm") == ""
    assert island_data.add_todo(env.conn, env.clock, "buy 2") == ""  # not a time
    assert island_data.add_todo(env.conn, env.clock, "   ") == "Type what to do."
    got = {t.title: t.due for t in items.todos(env.conn)}
    assert got["call mum"] is not None and got["buy 2"] is None


def test_notes_are_added_and_listed_newest_first(home: Path, config: Config):
    from personal_os.projects import items

    env = Env(home, config)
    assert island_data.add_note(env.conn, env.clock, "idea: dark mode for the brief") == ""
    env.clock.advance(60)
    assert island_data.add_note(env.conn, env.clock, "ask Ravi about the invoice",
                                project="no-such-project") == ""  # fmt: skip
    items.add(env.conn, env.clock, None, "note", title="found it", body="x", source="auto")
    assert island_data.add_note(env.conn, env.clock, "") == "Type a note."
    n = island_data.notes(env.conn, env.clock.now())
    assert [x["text"] for x in n["notes"]] == [
        "ask Ravi about the invoice", "idea: dark mode for the brief",
    ]  # newest first; Donna's own ('auto') aren't shown  # fmt: skip
    assert all(x["project"] == "" for x in n["notes"])  # an unknown project: the inbox


def test_the_island_api_writes_and_reads_notes(home: Path, config: Config):
    isl = island.Island(config)
    assert isl.api.add("note", "remember the milk") == ""
    assert isl.api.add("todo", "send the deck by friday") == ""
    assert isl.api.add("photo", "x") == "Can't add that here."
    assert [n["text"] for n in isl.api.cards("notes")["notes"]] == ["remember the milk"]
    (todo,) = isl.api.cards("today")["todos"]
    assert todo["text"] == "send the deck" and todo["due"]
    assert isl.api.todo(todo["id"], True) and isl.api.cards("today")["todos"] == []


def test_music_without_the_browser_says_so(tmp_path: Path):
    m = island_data.music(tmp_path, "brave")
    assert not m["available"] and "extension" in m["why"]


def test_typing_has_a_window_to_focus(config: Config):
    """The island's focus() reused the popup's, which needs the window; it had none, so a
    click in the box never took the keyboard (16 Nov)."""
    isl = island.Island(config)
    calls: list[str] = []

    class Win:
        class native:
            class Handle:
                @staticmethod
                def ToInt32() -> int:
                    calls.append("handle")
                    return 0

    isl.api._window = Win()
    isl.api.focus()
    assert calls == ["handle"] and isl.focused
