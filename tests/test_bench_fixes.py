"""The four failures of the first live bench (20 Nov): "thanks, that's all" ran project
lookups; "open youtube music and play X there" opened YouTube and searched by hand; "check
inside fmovies" answered "let me check" and stopped; "where did I leave off on shop-app"
missed the latest commits because they sat inside another project's session."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from personal_os.actions import agent
from personal_os.actions.music import open_then_play, song_request
from personal_os.config.schema import Config
from personal_os.context import ask as ask_mod
from personal_os.context.route import chat_reply, route
from personal_os.llm.fake import FakeProvider
from personal_os.projects.registry import load_projects
from tests.test_actions import Env
from tests.test_context import auth_session, sessions_of
from tests.test_projects import AUTH, SITE, World

# --- closings are small talk --------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "reply"),
    [
        ("thanks, that's all", "Okay. I'm here if you need me."),
        ("no thanks", "Okay. I'm here if you need me."),
        ("that's it, thanks donna", "Okay. I'm here if you need me."),
        ("ok thanks", "You're welcome."),
        ("see you later", "Bye."),
        ("perfect", "Okay."),
    ],
)
def test_closings_are_answered_at_once(said: str, reply: str):
    assert route(said) == "chat" and chat_reply(said) == reply


@pytest.mark.parametrize("said", ["thanks for opening it, now close brave", "okay, open brave",
                                  "that's all the tabs I need closed", "what's up with my build"])  # fmt: skip
def test_requests_that_start_like_small_talk_go_on(said: str):
    assert route(said) != "chat"


# --- "play <song>" by rules ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("said", "song"),
    [
        ("play blood sport by sleep token on youtube music",
         ("blood sport by sleep token", "youtube music")),
        ("play music - raaton by aditya rikhari", ("raaton by aditya rikhari", "music")),
        ("can you play circles by post malone please", ("circles by post malone", "music")),
        ("hey donna play numb", ("numb", "music")),
        ("play lofi beats on spotify", ("lofi beats", "spotify")),
        ("play despacito on youtube", ("despacito", "youtube")),
        ("play blood sport there", ("blood sport", "music")),
    ],
)  # fmt: skip
def test_a_song_by_name_is_read_by_rules(said: str, song: tuple[str, str]):
    assert song_request(said) == song


@pytest.mark.parametrize("said", ["play music", "play", "play some music", "play the music",
                                  "play the next episode", "play that video", "play it again",
                                  "play next song", "play something chill", "pause music",
                                  "open youtube music"])  # fmt: skip
def test_not_a_song_by_name(said: str):
    assert song_request(said) is None


def test_opening_the_service_before_playing_on_it_is_one_request():
    assert open_then_play(["open youtube music", "play blood sport there"]) == [
        "play blood sport on youtube music"
    ]
    assert open_then_play(["open spotify", "play numb"]) == ["play numb on spotify"]
    assert open_then_play(["open brave", "play numb"]) == ["open brave", "play numb"]
    # A service named in the play part wins: both parts stay.
    assert open_then_play(["open spotify", "play x on youtube"]) == ["open spotify",
                                                                     "play x on youtube"]  # fmt: skip


def test_voice_plays_a_song_without_the_model(home: Path, config: Config):
    from tests.test_phase7 import voice_for

    env = Env(home, config)

    def never(*_a: Any) -> Any:
        raise AssertionError("the model isn't needed for this")

    v, _h = voice_for(env, provider=FakeProvider(never))
    ran: list[tuple[str, dict[str, Any]]] = []
    v.run_tool = lambda tool, args, text, **_k: ran.append((tool, args)) or "Playing."  # type: ignore[method-assign]
    assert v.fast("open youtube music , and play blood sport there") == "Playing."
    assert ran == [("play_media", {"query": "blood sport", "site": "youtube music"})]
    ran.clear()
    v.fast("play zulfe and then take 5")
    assert ran == [("play_media", {"query": "zulfe", "site": "music"})]  # and a rest, by rules
    assert v.resting()


def test_say_hi_once_youre_back_is_how_the_rest_ends():
    from personal_os.voice.main import split_requests
    from personal_os.voice.rest import parse, tell_when_back, with_back

    parts = with_back(split_requests("take 5, say hi once you're back, and then pause music"))
    assert parts == ["take 5 and tell me when you're back", "pause music"]
    assert parse(parts[0]) == 5 and tell_when_back(parts[0])
    # Without a rest it's left alone (the agent decides what it means).
    assert with_back(["pause music", "tell me when you're back"]) == [
        "pause music", "tell me when you're back"
    ]  # fmt: skip


# --- an answer that says it'll check, and doesn't --------------------------------------------


def test_promises_to_check_are_found():
    assert agent.promises("I see Brave on Fmovies. Let me check what's playing there for you.")
    assert agent.promises("Sure, I'll take a look at it.")
    for fine in ("I'll remind you at 5.", "Let me know if you need anything else.",
                 "I'll remind you at 03:00 today: \"check claude\".",
                 "I can see Dune is playing.", "Playing Numb on YouTube Music."):  # fmt: skip
        assert not agent.promises(fine), fine
    assert agent.unpromised("You're on Fmovies. Let me check what's playing.") == (
        "You're on Fmovies."
    )


def test_a_promise_to_check_gets_one_more_step_to_check(home: Path, config: Config):
    env = Env(home, config)
    steps: list[str] = []

    def model(system: str, user: str, schema: Any) -> Any:
        if schema.__name__ == "FinalAnswer" or "Results so far" in user:
            steps.append("answer")
            return {"decide": "answer", "calls": [], "reply": "It's Dune Part Two."}
        if "nothing happens after your reply" in user:
            steps.append("checked")
            return {"decide": "use_tools", "calls": [{"tool": "now", "args": {}}], "reply": ""}
        steps.append("promised")
        return {"decide": "answer", "calls": [], "reply": "Let me check what you're watching."}

    out = agent.respond("check what i'm watching", env.ex, FakeProvider(model), max_steps=3)
    assert steps == ["promised", "checked", "answer"]
    assert out.reply == "It's Dune Part Two."


def test_a_promise_left_at_the_end_is_dropped(home: Path, config: Config):
    env = Env(home, config)

    def model(system: str, user: str, schema: Any) -> Any:
        return {"decide": "answer", "calls": [], "reply": "Let me look into that for you."}

    out = agent.respond("what's on fmovies", env.ex, FakeProvider(model), max_steps=1)
    assert out.reply == "I couldn't find that out."


# --- where you left off, from the project's own records ------------------------------------


@pytest.fixture
def w(home: Path, config: Config) -> World:
    return World(home, config)


def test_leave_off_finds_work_inside_another_projects_session(w: World):
    auth_session(w)  # 09:00-09:40 on auth-service
    # 10:00-11:40 mostly on the portfolio site, with two auth-service commits in the middle.
    w.window(60, 160, "index.html - portfolio-site - Visual Studio Code")
    for minute in range(62, 160, 6):
        w.edit(minute, f"{SITE}/index.html", SITE)
    w.commit(100, AUTH, "Retry refresh on 401")
    w.commit(104, AUTH, "Log refresh failures")
    w.resolve()
    sessions = sessions_of(w)
    assert [s.project_id for s in sessions][-1] != "auth-service"  # the majority won
    now = w.at(200)
    q = ask_mod.parse_question("where did I leave off on auth-service?", load_projects(w.conn), now)
    g = ask_mod.gather(w.conn, None, q, now)
    assert g.facts[0].startswith("Your latest work on auth-service:")
    texts = " ".join(x.text for x in g.pack.lines)
    assert "Log refresh failures" in texts and "Retry refresh on 401" in texts
    assert "The last session mostly on auth-service was earlier" in " ".join(g.facts)


def test_leave_off_ignores_guessed_records(w: World):
    """A Jetty script and an Fmovies tab were guessed onto shop-app (keyword, page: 0.55), and
    the answer led with them and said it had no data (20 Nov bench)."""
    auth_session(w)
    w.window(60, 160, "index.html - portfolio-site - Visual Studio Code")
    for minute in range(62, 160, 6):
        w.edit(minute, f"{SITE}/index.html", SITE)
    w.commit(100, AUTH, "Retry refresh on 401")
    w.resolve()
    sessions_of(w)
    w.conn.execute(  # the last portfolio edit, guessed onto auth-service
        "UPDATE events SET project_id = 'auth-service', project_confidence = 0.55 "
        "WHERE id = (SELECT id FROM events WHERE source = 'fs' ORDER BY ts_start DESC LIMIT 1)"
    )
    now = w.at(200)
    q = ask_mod.parse_question("where did I leave off on auth-service?", load_projects(w.conn), now)
    g = ask_mod.gather(w.conn, None, q, now)
    texts = " ".join(x.text for x in g.pack.lines)
    assert "Retry refresh on 401" in texts and "index.html" not in texts


def test_leave_off_keeps_the_session_when_it_is_the_latest(w: World):
    auth_session(w)
    sessions_of(w)
    now = w.at(120)
    q = ask_mod.parse_question("where did I leave off on auth-service?", load_projects(w.conn), now)
    g = ask_mod.gather(w.conn, None, q, now)
    assert g.facts[0].startswith("Your last session on auth-service")
