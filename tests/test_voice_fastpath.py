"""The voice fast path: index, fuzzy router, end of turn, echo gate, near-miss handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from personal_os.actions import apps
from personal_os.actions.apps import InstalledApp
from personal_os.config.schema import Config
from personal_os.voice import router
from personal_os.voice.audio import Endpointer
from personal_os.voice.bargein import TRIGGER_FRAMES, EchoGate
from personal_os.voice.index import Entry, Index, build, norm
from personal_os.voice.vad import FRAME
from tests.test_actions import Env
from tests.test_phase7 import voice_for


def e(kind: str, name: str, target: str) -> Entry:
    return Entry(kind, name, target, norm(name))  # type: ignore[arg-type]


INDEX = Index(
    [
        e("app", "Brave", "Brave"),
        e("app", "Google Chrome", "Google Chrome"),
        e("app", "chrome", "Google Chrome"),
        e("app", "Visual Studio Code", "Visual Studio Code"),
        e("app", "vscode", "vscode"),
        e("app", "Calculator", "Calculator"),
        e("app", "Claude", "Claude"),
        e("folder", "typewriter", "E:/typewriter"),
        e("folder", "Personal-OS", "E:/Personal-OS"),
        e("file", "resume", "E:/portfolio-site/resume.pdf"),
        e("file", "resume.pdf", "E:/portfolio-site/resume.pdf"),
    ],
    built_at=1.0,
)


@pytest.mark.parametrize(
    ("said", "tool", "args"),
    [
        ("open brave", "open_app", {"app": "Brave"}),
        ("Can you open Brave?", "open_app", {"app": "Brave"}),
        ("Hey Donna, open Brave.", "open_app", {"app": "Brave"}),
        ("open chrome", "open_app", {"app": "Google Chrome"}),
        ("launch the calculator", "open_app", {"app": "Calculator"}),
        ("open my resume", "open_path", {"path": "E:/portfolio-site/resume.pdf"}),
        ("open resume dot pdf", "open_path", {"path": "E:/portfolio-site/resume.pdf"}),
        ("open the typewriter folder", "open_path", {"path": "E:/typewriter"}),
        ("open typewriter in vs code", "open_app", {"app": "vscode", "target": "E:/typewriter"}),
        ("show resume in explorer", "reveal_path", {"path": "E:/portfolio-site/resume.pdf"}),
        ("set volume to 40", "set_volume", {"percent": 40}),
        ("volume sixty", "set_volume", {"percent": 60}),
        ("turn the volume up", "media", {"action": "volume_up", "times": 5}),
        ("mute", "media", {"action": "mute"}),
        ("lock the screen", "lock_screen", {}),
        ("take a screenshot", "screenshot", {}),
        ("brightness 70", "set_brightness", {"percent": 70}),
        ("minimize chrome", "window", {"title": "chrome", "action": "minimize"}),
    ],
)
def test_common_commands_run_without_the_llm(said: str, tool: str, args: dict[str, Any]):
    intent = router.route(said, INDEX)
    assert intent is not None and intent.sure, said
    assert intent.calls == [(tool, args)]


REPOS = Index(
    [
        *INDEX.entries,
        e("folder", "macos-rag-finder", "E:/macos-rag-finder"),
        e("folder", "Agentic_EDA", "E:/Agentic_EDA"),
        e("folder", "OS", "E:/OS"),
        e("folder", "site", "E:/x/site"),
        e("file", "no.pdf", "E:/no.pdf"),
        e("file", "no", "E:/no.pdf"),
        e("app", "Node.js", "Node.js"),
        e("app", "node", "Node.js"),
    ],
    built_at=1.0,
)


@pytest.mark.parametrize(
    ("said", "call"),
    [  # heard by voice on 5 Nov
        ("Open Personal OS on vscode.", ("open_app", {"app": "vscode", "target": "E:/Personal-OS"})),
        ("Open macOS RACFINDER on vscode,", ("open_app", {"app": "vscode", "target": "E:/macos-rag-finder"})),
        ("Open Agentic Underscore EDA on vscode.", ("open_app", {"app": "vscode", "target": "E:/Agentic_EDA"})),
        ("open macos dash rag dash finder in vs code", ("open_app", {"app": "vscode", "target": "E:/macos-rag-finder"})),
        ("Open Node.pdf", ("open_path", {"path": "E:/no.pdf"})),
    ],
)  # fmt: skip
def test_names_with_hyphens_underscores_and_on_an_app(said: str, call: tuple[str, Any]):
    intent = router.route(said, REPOS)
    assert intent is not None and intent.sure, (said, intent and intent.question)
    assert intent.calls == [call]


def test_a_name_inside_what_you_said_is_not_a_match():
    """ "personal site" isn't the "site" folder, "personal os" isn't the "OS" folder."""
    site = router.route("open personal site", REPOS)
    assert site is None or not site.sure or site.calls[0][1].get("path") != "E:/x/site"
    pos = router.route("open personal os", REPOS)
    assert pos is not None and pos.sure and pos.calls == [("open_path", {"path": "E:/Personal-OS"})]
    assert router.score("chrome", e("app", "Google Chrome", "x")) >= 0.9  # the other way is fine


def f(kind: str, name: str, target: str, penalty: float = 0.0) -> Entry:
    where = target.replace("\\", "/").rsplit("/", 2)[-2] if "/" in target.replace("\\", "/") else ""
    return Entry(kind, name, target, norm(name), penalty, where)  # type: ignore[arg-type]


DRIVE = Index(
    [
        f("app", "Claude", "Claude"),
        f("app", "What is new in the latest version", "What is new in the latest version"),
        f("folder", "New App", "E:/New App"),
        f("folder", "cadence-app", "E:/New App/cadence-app", 0.012),
        f("folder", "My-study-plan", "E:/My-study-plan"),
        f("file", "my-study-plan", "E:/My-study-plan/Logs/my-study-plan.md", 0.024),
        f("folder", "Tutedude", "E:/Tutedude"),
        f("file", "Tutedude", "E:/Tutedude.zip"),
        f("file", "resume", "E:/portfolio-site/resume.pdf", 0.012),
        f("file", "resume", "E:/pos/resume.py", 0.104),
        f("file", "hero", "E:/cadence-app/src/assets/hero.png", 0.1),
        f("file", "hero", "E:/macos-rag-finder/src/assets/hero.png", 0.1),
        f("folder", "assets", "E:/a/raw/assets", 0.03),
        f("folder", "assets", "E:/b/site/assets", 0.03),
    ],
    built_at=1.0,
)


@pytest.mark.parametrize(
    ("said", "target"),
    [
        ("open cadence app", "E:/New App/cadence-app"),  # a name with "app" in it isn't an app
        ("open new app", "E:/New App"),
        ("open my study plan", "E:/My-study-plan"),  # the folder, not the file inside it
        ("open tutedude", "E:/Tutedude"),  # the folder, not Tutedude.zip
        ("open my resume", "E:/portfolio-site/resume.pdf"),  # the document, not resume.py
    ],
)
def test_the_existing_name_that_fits_best_opens(said: str, target: str):
    intent = router.route(said, DRIVE)
    assert intent is not None and intent.sure, (said, intent and intent.question)
    assert router.targets(intent) == {target}


def test_same_names_in_different_places_ask_with_their_folders():
    hero = router.route("open hero", DRIVE)
    assert hero is not None and not hero.sure
    assert "hero.png in cadence-app/src/assets" in hero.question
    assert "hero.png in macos-rag-finder/src/assets" in hero.question
    assets = router.route("open the assets folder", DRIVE)
    assert assets is not None and "assets in raw" in assets.question
    picked = router.choose("the one in macos rag finder", hero)
    assert picked is not None and router.targets(picked) == {
        "E:/macos-rag-finder/src/assets/hero.png"
    }


def test_the_index_covers_deep_folders_repos_and_same_names(
    home: Path, config: Config, monkeypatch: pytest.MonkeyPatch
):
    disk = home.parent / f"{home.name}-drive"
    deep = disk / "Projects" / "client" / "2026" / "reports" / "final"
    deep.mkdir(parents=True)
    (deep / "invoice.pdf").write_bytes(b"%PDF")
    for repo in ("one", "two"):
        (disk / repo / ".git").mkdir(parents=True)
        (disk / repo / "docs").mkdir()
        (disk / repo / "docs" / "notes.md").write_text("x")
    install = disk / "Some App"
    (install / "resources" / "extensions").mkdir(parents=True)
    (install / "SomeApp.exe").write_bytes(b"MZ")
    (disk / "abcdef0123456789").mkdir()  # a content hash
    monkeypatch.setattr(apps, "installed", lambda *_a, **_k: [])
    actions = config.actions.model_copy(
        update={"allowed_paths": [disk], "act_in_watched_roots": False, "apps": {}}
    )
    index = build(config.model_copy(update={"actions": actions}))
    targets = {x.target for x in index.entries}
    assert str(deep / "invoice.pdf") in targets  # five folders down
    assert str(disk / "one" / "docs" / "notes.md") in targets  # inside a repo
    assert str(disk / "two" / "docs" / "notes.md") in targets  # same name, kept
    assert str(install) in targets and str(install / "resources") not in targets
    assert str(disk / "abcdef0123456789") not in targets
    inner = next(x for x in index.entries if x.target == str(disk / "one" / "docs"))
    top = next(x for x in index.entries if x.target == str(disk / "one"))
    assert inner.penalty > top.penalty  # deeper and inside the repo: ranks lower


def test_questions_and_unknown_requests_go_to_the_llm():
    for said in ("what did I do today?", "remind me in 5 minutes to stretch", "how are you"):
        assert router.route(said, INDEX) is None, said


def test_a_near_miss_is_a_guess_not_an_action():
    intent = router.route("open brat", INDEX)
    assert intent is not None and not intent.sure and intent.calls == []
    assert intent.guesses[0].entry.name == "Brave" and intent.question.startswith(
        "Did you mean Brave"
    )
    assert intent.phrase(intent.guesses[0].entry) == "open Brave"


def test_answers_to_did_you_mean():
    intent = router.route("open brat", INDEX)
    assert intent is not None
    yes = router.choose("yes", intent)
    assert yes is not None and yes.calls == [("open_app", {"app": "Brave"})]
    assert router.choose("no", intent) is None
    assert router.choose("", intent) is None
    named = router.choose("Brave", intent)
    assert named is not None and named.calls == [("open_app", {"app": "Brave"})]
    if len(intent.guesses) > 1:
        second = router.choose("the second one", intent)
        assert second is not None and second.calls[0][1]["app"] == intent.guesses[1].entry.target


def test_time_is_answered_directly():
    intent = router.route("what time is it", INDEX)
    assert intent is not None and intent.calls == [] and intent.reply.startswith("It's ")


# --- end of turn and the echo gate (no audio device) ---------------------------------------


class FakeVAD:
    """Returns the probabilities it was given, one per frame."""

    def __init__(self, probs: list[float]) -> None:
        self.probs = list(probs)

    def reset(self) -> None:
        pass

    def speech_prob(self, frame: np.ndarray, *, gain: float = 1.0) -> float:
        return self.probs.pop(0) if self.probs else 0.0


def frames(n: int, level: float = 0.01) -> list[np.ndarray]:
    return [np.full(FRAME, level, dtype=np.float32) for _ in range(n)]


def test_the_turn_ends_after_the_silence_and_keeps_the_start():
    probs = [0.0] * 5 + [0.9] * 10 + [0.0] * 30
    ep = Endpointer(FakeVAD(probs), silence_ms=320)  # type: ignore[arg-type]
    states = [ep.feed(f) for f in frames(45)]
    assert "speaking" in states and "done" in states
    done_at = states.index("done")
    assert done_at == 5 + 10 + 10 - 1  # 10 quiet frames of 32 ms
    audio = ep.audio()
    assert audio is not None and len(audio) >= 10 * FRAME  # speech plus the pre-roll


def test_nobody_speaking_times_out():
    ep = Endpointer(FakeVAD([0.0] * 400), start_timeout_s=1.0)  # type: ignore[arg-type]
    states = [ep.feed(f) for f in frames(40)]
    assert states[-1] == "timeout" and ep.audio() is None


def test_a_cough_is_not_a_turn():
    ep = Endpointer(FakeVAD([0.9, 0.0] * 20), start_timeout_s=1.0)  # type: ignore[arg-type]
    states = [ep.feed(f) for f in frames(40)]
    assert "speaking" not in states


def test_the_echo_gate_ignores_its_own_reply_and_hears_you():
    gate = EchoGate(FakeVAD([0.95] * 200), floor=0.002, warmup_frames=12)  # type: ignore[arg-type]
    out = 0.2
    echo = [np.full(FRAME, 0.02, dtype=np.float32)] * 60  # the reply leaking in: 0.1 x playback
    assert not any(gate.feed(f, out) for f in echo)  # "speech" by VAD, but only echo
    you = [np.full(FRAME, 0.09, dtype=np.float32)] * TRIGGER_FRAMES
    results = [gate.feed(f, out) for f in you]
    assert results[-1] and not any(results[:-1])


def test_barge_in_is_never_armed_for_a_reply_to_a_barge_in(home: Path, config: Config, monkeypatch):
    """The loop seen with a video playing: its speech barged in, was answered, barged in..."""
    import personal_os.voice.bargein as bargein_mod
    from personal_os.voice.main import Voice

    armed: list[str] = []

    class Spy:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            pass

        def __enter__(self) -> Spy:
            armed.append("armed")
            return self

        def __exit__(self, *_a: Any) -> None:
            pass

    monkeypatch.setattr(bargein_mod, "BargeIn", Spy)
    cfg = config.model_copy(update={"voice": config.voice.model_copy(update={"barge_in": "voice"})})
    v = Voice(cfg)
    v.speaker.say = lambda *_a, **_k: True  # type: ignore[method-assign]
    v.turn_trigger = "hotkey"
    v.say("hello")
    assert armed == ["armed"]
    v.turn_trigger = "barge"
    v.say("hello again")
    assert armed == ["armed"]  # not armed: a stray sound can't chain replies
    hotkey_only = Voice(
        config.model_copy(update={"voice": config.voice.model_copy(update={"barge_in": "hotkey"})})
    )
    hotkey_only.speaker.say = lambda *_a, **_k: True  # type: ignore[method-assign]
    hotkey_only.turn_trigger = "hotkey"
    hotkey_only.say("hi")
    assert armed == ["armed"]


def test_other_apps_playing_sound_is_measurable():
    from personal_os.voice.bargein import other_audio_level

    level = other_audio_level()
    assert isinstance(level, float) and 0.0 <= level <= 1.0


# --- the voice loop's handling of near misses ---------------------------------------------------


def test_a_near_miss_is_decoded_again_before_asking(home: Path, config: Config):
    env = Env(home, config)
    v, h = voice_for(env)
    v.index._index = INDEX
    v.last_audio = np.zeros(1600, dtype=np.float32)
    v.stt.recheck = lambda audio, phrases: "open Brave"  # type: ignore[method-assign]
    ran: list[Any] = []
    v.run_intent = lambda intent, text: ran.append(intent.calls) or intent.reply  # type: ignore[method-assign]
    assert v.fast("open brat") == "Opening Brave."
    assert ran == [[("open_app", {"app": "Brave"})]] and h.said == []  # nothing asked


def test_a_biased_recheck_never_turns_a_weak_guess_into_an_action(home: Path, config: Config):
    """Saying something that matches nothing well: the biased re-decode lands on a weak
    candidate ("open read"), which must not run; it asks instead."""
    brave, read = INDEX.entries[0], e("file", "readme", "E:/notes/readme.md")
    index = Index([*INDEX.entries, read], built_at=1.0)
    first = router.Intent(
        [], "", 0.67, guesses=[router.Match(brave, 0.67), router.Match(read, 0.6)], question="?"
    )
    opens_read = router.route("open readme", index)
    opens_brave = router.route("open brave", index)
    assert opens_read is not None and opens_brave is not None
    assert not router.confirms(opens_read, first)  # a weak, second-place guess
    assert router.confirms(opens_brave, first)  # the top guess
    env = Env(home, config)
    v, h = voice_for(env, "no")
    v.index._index = index
    v.last_audio = np.zeros(1600, dtype=np.float32)
    v.stt.recheck = lambda audio, phrases: "open readme"  # type: ignore[method-assign]
    v.fast("open brat")
    assert h.said and h.said[0].startswith("Did you mean Brave")  # asked, didn't open readme


def test_still_unsure_it_asks_and_acts_on_yes(home: Path, config: Config):
    env = Env(home, config)
    v, h = voice_for(env, "yes")
    v.index._index = INDEX
    v.last_audio = np.zeros(1600, dtype=np.float32)
    v.stt.recheck = lambda audio, phrases: "open brat"  # type: ignore[method-assign]
    ran: list[Any] = []
    v.run_intent = lambda intent, text: ran.append(intent.calls) or intent.reply  # type: ignore[method-assign]
    assert v.fast("open brat") == "Opening Brave."
    assert h.said and h.said[0].startswith("Did you mean Brave")
    v2, _ = voice_for(env, "no")
    v2.index._index = INDEX
    v2.last_audio = None
    assert v2.fast("open brat") == "Okay, I'll leave it."


def test_fast_path_commands_run_through_the_executor(home: Path, config: Config, monkeypatch):
    import personal_os.actions.desktop as desktop_mod

    env = Env(home, config)
    monkeypatch.setattr(desktop_mod, "default_desktop", lambda: env.desktop)
    v, _h = voice_for(env)
    v.index._index = INDEX
    assert v.fast("set the volume to 35") == "Volume 35."
    assert v.fast("lock the screen") == "Locking."
    assert ("set_volume", 35) in env.desktop.calls and ("lock_screen", None) in env.desktop.calls


# --- the index ---------------------------------------------------------------------------------


def test_the_index_has_apps_folders_and_openable_files_only(
    home: Path, config: Config, monkeypatch: pytest.MonkeyPatch
):
    disk = home.parent / f"{home.name}-disk"
    (disk / "typewriter" / "notes").mkdir(parents=True)
    (disk / "typewriter" / "resume.pdf").write_bytes(b"%PDF")
    (disk / "holiday.jpg").write_bytes(b"jpg")
    (disk / "tool.exe").write_bytes(b"MZ")
    (disk / "id_rsa.pdf").write_bytes(b"secret")  # matches a secret pattern: never indexed
    (disk / "node_modules" / "x").mkdir(parents=True)
    monkeypatch.setattr(
        apps, "installed",
        lambda *_a, **_k: [InstalledApp("Brave", "Brave", "C:/Brave/brave.exe")],
    )  # fmt: skip
    actions = config.actions.model_copy(
        update={"allowed_paths": [disk], "act_in_watched_roots": False, "apps": {}}
    )
    index = build(config.model_copy(update={"actions": actions}))
    names = {(x.kind, x.name) for x in index.entries}
    assert ("app", "Brave") in names
    assert ("folder", "typewriter") in names and ("folder", "notes") in names
    assert ("file", "resume") in names and ("file", "holiday.jpg") in names
    assert not any(n in ("tool", "tool.exe", "id_rsa", "x", "node_modules") for _, n in names)
    assert "Brave" in index.names_for_hints()
