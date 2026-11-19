"""Stopping Donna, plain-words confirmations, honest replies, closing apps, and the wake
word's hand-over (the voice fixes of 7 Nov: "close VS Code" opened it and said it closed it;
"stop" and Esc didn't stop her thinking; "Donna" picked up what was said before it)."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from personal_os.actions import agent
from personal_os.actions import apps as apps_mod
from personal_os.actions.apps import InstalledApp
from personal_os.actions.base import Origin
from personal_os.actions.desktop import WindowInfo
from personal_os.actions.planner import borrowed
from personal_os.config.schema import Config
from personal_os.llm.fake import FakeProvider
from personal_os.voice import wake
from personal_os.voice.main import (
    _without_wake,
    confirm_question,
    history,
    mentions_wake,
    question_from_prompt,
    request_stop,
    take_stop,
)
from tests.test_actions import Confirm, Env
from tests.test_agent import Script, answer, use
from tests.test_phase7 import voice_for

# --- the agent: announce after approval, stop, honest replies ---------------------------------


def test_actions_are_announced_once_approved_just_before_they_run(home: Path, config: Config):
    env = Env(home, config)
    order: list[str] = []
    real = env.desktop.set_volume

    def set_volume(percent: int) -> None:
        order.append("volume changed")
        real(percent)

    env.desktop.set_volume = set_volume  # type: ignore[method-assign]
    script = Script(use(("set_volume", {"percent": 30})), answer("Volume is 30."))
    agent.respond(
        "volume 30", env.ex, FakeProvider(script), announce=lambda t: order.append(f"say {t}")
    )
    assert order == ["say Volume 30.", "volume changed"]


def test_a_declined_action_is_never_announced(home: Path, config: Config):
    env = Env(home, config, Confirm(False))
    said: list[str] = []
    script = Script(use(("open_url", {"url": "https://example.com/page"})), answer("Okay."))
    r = agent.respond(
        "open it", env.ex, FakeProvider(script), announce=said.append,
        context="User: what's on https://example.com/page\nDonna: A page.",
    )  # fmt: skip
    assert env.confirm.prompts  # it came from the conversation: it was asked about
    assert said == []  # ...and "Opening it." was never said, since you said no
    assert [o.status for o in r.outcomes] == ["declined"]


def test_stopping_ends_the_loop_before_anything_more_runs(home: Path, config: Config):
    env = Env(home, config)
    stop = {"now": False}

    def responder(system: str, user: str, schema: Any) -> Any:
        stop["now"] = True  # "stop" said while the model was answering
        return use(("set_volume", {"percent": 30}))

    r = agent.respond("volume 30", env.ex, FakeProvider(responder), cancelled=lambda: stop["now"])
    assert r.outcomes == [] and r.reply == "" and r.error == "stopped"
    assert env.desktop.volume == 30 or ("set_volume" not in [c[0] for c in env.desktop.calls])


def test_a_claim_of_a_different_action_is_replaced_by_what_ran(home: Path, config: Config):
    env = Env(home, config)
    script = Script(use(("set_volume", {"percent": 40})), answer("I've closed VS Code for you."))
    r = agent.respond("volume 40", env.ex, FakeProvider(script))
    assert r.reply == "Volume 40%."  # what actually happened, not the claim
    # An action that worked is said in its own words, with no second model call (19 Nov), so
    # a model's wrong claim never reaches you at all.
    env2 = Env(home.parent / "h2", config)
    script = Script(use(("set_volume", {"percent": 40})), answer("I've set the volume to 40."))
    assert agent.respond("volume 40", env2.ex, FakeProvider(script)).reply == "Volume 40%."


def test_an_app_name_said_with_different_spacing_is_not_borrowed():
    earlier = "User: open my study plan in vscode\nDonna: Opening My-study-plan in vscode."
    assert not borrowed({"app": "vscode"}, "Close VS Code.", earlier)
    assert borrowed({"app": "vscode"}, "close it", earlier)  # "it": from the conversation


# --- closing an app ---------------------------------------------------------------------------


def test_close_app_closes_only_that_apps_windows(
    home: Path, config: Config, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        apps_mod, "installed", lambda *_a, **_k: [InstalledApp("Visual Studio Code", "vsc")]
    )
    env = Env(home, config)
    env.desktop.open_windows = [
        WindowInfo(1, "main.py - Personal-OS - Visual Studio Code", "normal", "Code.exe"),
        WindowInfo(2, "Claude Code docs - Brave", "normal", "brave.exe"),
        WindowInfo(3, "Program Manager", "normal", "explorer.exe"),
        WindowInfo(4, "notes - Visual Studio Code", "normal", "Code.exe"),
    ]
    out = env.ex.request("close_app", {"app": "vs code"}, origin=Origin.USER)
    assert out.ok and out.message == "Closed vs code"
    assert [c for c in env.desktop.calls if c[0] == "close_window"] == [
        ("close_window", 1), ("close_window", 4),
    ]  # fmt: skip
    assert [w.handle for w in env.desktop.open_windows] == [2, 3]
    again = env.ex.request("close_app", {"app": "vs code"}, origin=Origin.USER)
    assert not again.ok and "isn't open" in again.message


def test_close_is_understood_without_the_model():
    from personal_os.voice import router
    from tests.test_voice_fastpath import INDEX

    intent = router.route("Close VS Code.", INDEX)
    assert intent is not None and intent.sure
    assert intent.calls == [("close_app", {"app": "vscode"})]
    tab = router.route("close this tab", INDEX)  # not an app: the tab in front
    assert tab is not None and tab.calls == [("browser_tab", {"action": "close"})]
    assert router.route("close the netflix thing", INDEX) is None  # unclear: the model's job


# --- asking in plain words --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pending", "question"),
    [
        ({"tool": "open_app", "args": {"app": "vscode"}}, "Open vscode?"),
        ({"tool": "open_app", "args": {"app": "vscode", "target": "E:/OS/"}}, "Open vscode with OS?"),
        ({"tool": "close_app", "args": {"app": "Brave"}}, "Close Brave?"),
        ({"tool": "open_url", "args": {"url": "https://example.com/a?b=1"}}, "Open example.com?"),
        ({"tool": "set_reminder", "args": {}, "preview": "stretch at 16:34"}, "Stretch at 16:34?"),
        ({"tool": "run_command", "args": {}, "preview": 'start "C:\\x\\Code.exe"'}, "Run command?"),
    ],
)  # fmt: skip
def test_confirmations_are_asked_in_plain_words(pending: dict[str, Any], question: str):
    assert confirm_question(pending) == question


def test_the_captured_content_warning_is_never_read_out():
    prompt = (
        "open_app (tier 1, planned from your request; tier 1, after captured content)\n"
        "  ! follows captured content (page text, files, command output)\n"
        '  start "C:\\Code.exe"\nProceed? [y/N] '
    )
    assert "captured" not in question_from_prompt(prompt)


def test_the_question_is_on_screen_while_it_is_asked(home: Path, config: Config):
    env = Env(home, config)
    v, h = voice_for(env, "yes")
    v.turn = {"id": "t", "state": "thinking", "heard": "", "reply": "", "question": ""}
    shown: list[tuple[str, str]] = []
    real = v.write_status

    def spy(state: str, **extra: Any) -> None:
        shown.append((state, str(v.turn["question"]) if v.turn else ""))
        real(state, **extra)

    v.write_status = spy  # type: ignore[method-assign]

    ex = SimpleNamespace(pending={"tool": "open_app", "args": {"app": "vscode"}, "tainted": True})
    assert v.confirm("open_app (tier 1)\n  ! follows captured content\nProceed?", ex) is True
    assert h.said[0] == "Open vscode? Say yes or no."
    assert shown[0][0] == "speaking" and shown[0][1].startswith("Open vscode?")
    assert ("listening", shown[0][1]) in shown
    assert shown[-1] == ("thinking", "")  # cleared once answered


# --- stopping a turn --------------------------------------------------------------------------


def test_stop_while_thinking_says_nothing_and_ends_the_conversation(home: Path, config: Config):
    env = Env(home, config)
    holder: dict[str, Any] = {}

    def responder(system: str, user: str, schema: Any) -> Any:
        holder["v"].cancel_turn()  # Esc pressed while the model was working
        return {"decide": "answer", "calls": [], "reply": "Here's a long answer."}

    v, h = voice_for(
        env, "explain how a thread differs from a process", provider=FakeProvider(responder)
    )
    holder["v"] = v
    v.converse("wake")
    assert h.said == []  # nothing said after the stop, not even "something went wrong"
    log = history(env.config.runtime_dir)
    assert len(log) == 1 and log[0]["route"].endswith("(stopped)")
    assert v.turn is not None and v.turn["reply"] == "Stopped."
    assert v.convo.recent() == []  # not remembered as something to build on


def test_a_stop_request_is_taken_once_and_an_old_one_is_ignored(tmp_path: Path):
    assert not take_stop(tmp_path)
    request_stop(tmp_path)
    assert take_stop(tmp_path) and not take_stop(tmp_path)
    request_stop(tmp_path)
    assert not take_stop(tmp_path, max_age_s=-1.0)  # too old to act on
    assert not (tmp_path / "voice_stop.json").exists()


def test_the_popup_stop_reaches_the_voice_process(home: Path, config: Config):
    from personal_os.interfaces.popup import Api

    api = Api(config, demo=False)
    assert api.stop() is True
    assert take_stop(config.runtime_dir)


def test_one_word_heard_after_the_wake_word_is_asked_again(home: Path, config: Config):
    env = Env(home, config)
    v, h = voice_for(env, "Donna, code.", provider=FakeProvider(lambda *_a: answer("No.")))
    v.converse("wake")
    assert h.said == ["Sorry, I only caught “code”. Please say it again."]
    assert history(env.config.runtime_dir)[-1]["route"] == "asked again (one word)"


# --- the wake word's hand-over ----------------------------------------------------------------


def test_what_came_before_the_wake_word_is_not_the_request():
    assert _without_wake("so yeah, and then. Hey Donna, open Brave.") == "open Brave"
    assert _without_wake("Hey Donna, open Brave.") == "open Brave"
    assert _without_wake("Open Brave") == "Open Brave"  # no wake word: unchanged


def test_a_recording_without_the_wake_word_is_a_false_wake():
    assert mentions_wake("Donna, open Brave", ["donna"])
    assert mentions_wake("Dana, open Brave", ["donna"])  # how Whisper may write it
    assert not mentions_wake("and you open it.", ["donna"])
    assert not mentions_wake("Come at me.", ["donna"])


def test_only_the_last_few_seconds_are_handed_over_on_wake():
    chunks = [np.zeros(512, np.float32) for _ in range(250)]  # 8 s of 32 ms frames
    kept = wake.recent(chunks, 3.0)
    assert 3.0 <= sum(len(c) for c in kept) / 16000 < 3.1
    assert wake.recent(chunks[:10], 3.0) == chunks[:10]


def test_stop_phrases_work_while_she_is_thinking(home: Path, config: Config):
    v, _h = voice_for(Env(home, config))
    v.state = "thinking"
    stopped: list[bool] = []
    w = wake.VoskWake.__new__(wake.VoskWake)
    w.speaking = lambda: v.state in ("speaking", "thinking")
    w.on_stop = lambda: stopped.append(True)
    w.last = {}
    w.ring = __import__("collections").deque()

    class Rec:
        def Reset(self) -> None: ...

    w._rec = Rec()
    w.heard("okay never mind")
    assert stopped == [True]


def test_the_status_time_is_recent_enough_for_a_stop(tmp_path: Path):
    request_stop(tmp_path)
    time.sleep(0.01)
    assert take_stop(tmp_path, max_age_s=3.0)


# --- "stop" as the request (7 Nov, later: "Stop." paused the music for 16 s) ---------------


@pytest.mark.parametrize(
    "said", ["Stop.", "stop it", "Okay, stop.", "Never mind.", "that's all", "Cancel that."]
)
def test_stop_as_the_request_ends_it_without_doing_anything(home: Path, config: Config, said: str):
    env = Env(home, config)
    v, h = voice_for(env, said, "open brave")  # "open brave" must never be listened for
    ran: list[Any] = []
    v.run_intent = lambda intent, text: ran.append(intent) or "Okay."  # type: ignore[method-assign,assignment,func-returns-value]
    v.converse("followup")
    assert ran == [] and h.said == []  # no "Pausing.", no media key, nothing said
    assert v.turn is not None and v.turn["dismissed"] and v.turn["reply"] == "Stopped."
    assert history(env.config.runtime_dir)[-1]["route"].startswith("stopped by you")


def test_pausing_the_music_still_works():
    from personal_os.voice import router
    from tests.test_voice_fastpath import INDEX

    intent = router.route("stop the music", INDEX)
    assert intent is not None and intent.calls == [("media", {"action": "play_pause"})]


def test_a_stopped_turn_closes_the_popup_at_once():
    from personal_os.voice.popup_view import AutoClose, View

    a = AutoClose(5)
    a.step(View(True, "thinking", "t1"), 0.0)
    close, _ = a.step(View(True, "done", "t1", dismissed=True), 0.1)
    assert close


def test_stop_ends_a_slow_browser_wait(home: Path, config: Config, monkeypatch):
    """The extension didn't answer, so "pause" waited the full 15 s, and neither "stop" nor
    the popup's Stop could end it."""
    import threading

    from personal_os.browser import control

    monkeypatch.setattr(control, "connected", lambda *_a: ["brave"])
    env = Env(home, config)
    env.ex.stop_signal = threading.Event()
    threading.Timer(0.3, env.ex.stop_signal.set).start()
    t = time.monotonic()
    out = env.ex.request("browser_media", {"action": "pause"}, origin=Origin.USER)
    assert time.monotonic() - t < 2.0
    assert out.status == "cancelled" and out.message == "stopped"
    assert not list((env.config.runtime_dir / "browser" / "cmd").glob("*.json"))  # withdrawn


# --- her own voice heard as you (7 Nov, 22:15: "Did you mean?" cut her question off) ---------


def test_talking_over_her_needs_donna_or_stop_by_default(home: Path, config: Config, monkeypatch):
    import personal_os.voice.bargein as bargein

    assert config.voice.barge_in == "wake"
    v, _h = voice_for(Env(home, config))
    del v.say  # the real say(), with a fake speaker
    v.engine = SimpleNamespace(aec_active=True)
    armed: list[str] = []
    monkeypatch.setattr(bargein, "CleanBargeIn", lambda *a, **k: armed.append("clean"))
    monkeypatch.setattr(bargein, "BargeIn", lambda *a, **k: armed.append("raw"))
    v.speaker.say = lambda text, interrupt=None: True  # type: ignore[method-assign]
    v.turn_trigger = "wake"
    v.say("Did you mean vscode?")
    assert armed == []  # any speech doesn't interrupt; "Donna" and "stop" (Vosk) still do


def test_a_barge_in_that_is_her_own_words_is_ignored(home: Path, config: Config):
    from personal_os.voice.main import own_words

    assert own_words("Did you mean?", "Did you mean vscode, or Microsoft VS Code? Say yes or no.")
    assert own_words("I'm here", "I'm here to help. What would you like me to do?")
    assert not own_words("No, open Chrome", "Did you mean vscode?")
    env = Env(home, config)
    v, h = voice_for(env, "Did you mean?")
    v.said_last = "Did you mean vscode? Say yes or no."
    v.barge_frames = [np.zeros(512, np.float32)]
    assert v.respond("barge") is False
    assert (
        h.said == [] and history(env.config.runtime_dir)[-1]["route"] == "her own voice (ignored)"
    )


def test_stopped_is_logged_once(home: Path, config: Config):
    env = Env(home, config)
    v, _h = voice_for(env, "stop")
    v.converse("wake")
    assert history(env.config.runtime_dir)[-1]["route"] == "stopped by you"


# --- 16 Nov: Settings couldn't be closed; real requests dropped as "no wake word" -----------


def test_close_settings_finds_windows_settings(home: Path, config: Config, monkeypatch):
    monkeypatch.setattr(apps_mod, "installed", lambda *_a, **_k: [InstalledApp("Settings", "ms")])
    env = Env(home, config)
    env.desktop.open_windows = [
        WindowInfo(7, "Settings", "normal", "SystemSettings.exe"),  # a UWP app's real program
        WindowInfo(8, "Settings - Google Chrome", "normal", "chrome.exe"),  # not this one
    ]
    out = env.ex.request("close_app", {"app": "Settings"}, origin=Origin.USER)
    assert out.ok, out.message
    assert [w.handle for w in env.desktop.open_windows] == [8]


def test_a_clear_request_is_not_thrown_away_for_missing_the_name(home: Path, config: Config):
    env = Env(home, config)
    v, _h = voice_for(env, "Open calculator.")
    from tests.test_voice_fastpath import INDEX

    v.index._index = INDEX
    v.barge_frames = [np.zeros(512, np.float32)]  # the wake word's audio was handed over
    ran: list[Any] = []
    v.run_intent = lambda intent, text: ran.append(intent.calls) or "Opening Calculator."  # type: ignore[method-assign,assignment,func-returns-value]
    v.respond("wake")
    assert ran == [[("open_app", {"app": "Calculator"})]]
    v2, _h = voice_for(Env(home.parent / "h3", config), "and you open it")
    v2.barge_frames = [np.zeros(512, np.float32)]
    v2.respond("wake")
    assert history(v2.cfg.runtime_dir)[-1]["route"] == "no wake word in it"  # a stray phrase


def test_donna_heard_over_her_reply_unless_she_said_it(home: Path, config: Config):
    """While she talks the wake word listens to the raw mic (the echo canceller squashes you
    too); her own "I'm Donna" mustn't wake her."""
    v, _h = voice_for(Env(home, config))
    v.state, v.said_last = "speaking", "I'm Donna, your assistant."
    v._woke([])
    assert v.triggers.empty() and not v.interrupt.is_set()
    v.said_last = "Here's what I found."
    v._woke([])
    assert v.triggers.get_nowait() == "wake" and v.interrupt.is_set()


def test_the_wake_word_hears_the_raw_mic_while_she_talks():
    import contextlib
    import queue
    import threading

    from personal_os.voice.engine import Frame

    clean, raw = np.zeros(512, np.float32), np.ones(512, np.float32)
    talking = [True, False]  # she's talking for the first frame, not the second
    frames: queue.Queue[Frame] = queue.Queue()
    for t in (1.0, 2.0):
        frames.put(Frame(clean, 0.1, 0.1, t, raw))

    class Engine:
        @contextlib.contextmanager
        def subscribe(self) -> Any:
            yield frames

    w = wake.VoskWake.__new__(wake.VoskWake)
    w.engine, w.stop = Engine(), threading.Event()
    fed: list[float] = []

    def feed(audio: np.ndarray) -> None:
        fed.append(float(audio[0]))
        talking.pop(0)
        if not talking:
            w.stop.set()

    w.feed = feed  # type: ignore[method-assign]
    w.speaking = lambda: talking[0]
    w._run()  # the real loop: it picks the stream
    assert fed == [1.0, 0.0]  # raw mic while she talks, echo-cancelled otherwise
    assert w.heard_until == 2.0


# --- 16 Nov: "opens smart meter clean in VS code" looked only in E:/Personal-OS --------------


def test_opens_as_whisper_writes_it_is_still_open():
    from personal_os.voice import router
    from tests.test_voice_fastpath import INDEX

    for said in ("Opens Brave.", "opened brave", "opening brave"):
        intent = router.route(said, INDEX)
        assert intent is not None and intent.calls == [("open_app", {"app": "Brave"})], said


def test_a_folder_name_said_with_spaces_is_found_in_your_folders(home: Path, config: Config):
    from personal_os.actions.base import ActionError

    env = Env(home, config)
    (env.work / "smartmeter-clean").mkdir()
    paths = env.ex.paths
    for said in ("smart meter clean", "Smart Meter-Clean", "smartmeter-clean"):
        assert Path(paths.locate(said)) == (env.work / "smartmeter-clean").resolve(), said
    with pytest.raises(ActionError, match="nothing called 'no such thing' in your folders"):
        paths.check(paths.locate("no such thing"), must_exist=True)


def test_your_folders_come_before_the_folder_voice_started_in(
    home: Path, config: Config, monkeypatch: pytest.MonkeyPatch
):
    env = Env(home, config)
    (env.work / "notes").mkdir()
    elsewhere = home.parent / "voice-started-here"
    (elsewhere / "notes").mkdir(parents=True)
    monkeypatch.chdir(elsewhere)
    assert Path(env.ex.paths.locate("notes")) == (env.work / "notes").resolve()
