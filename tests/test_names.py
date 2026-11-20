"""Your names put back where speech recognition wrote a sound-alike (voice/names.py)."""

from __future__ import annotations

import pytest

from personal_os.voice.names import correct

NAMES = ["Chordus", "Personal-OS", "roamies", "shop_app", "Jetty", "Brave", "Claude", "vscode",
         "YouTube Music", "Notepad++", "Calculator"]  # fmt: skip


@pytest.mark.parametrize(
    ("heard", "fixed"),
    [
        ("Where did I leave off on Shapa?", "Where did I leave off on shop_app?"),
        ("Where did I leave off on Cordis?", "Where did I leave off on Chordus?"),
        ("open roamy", "open roamies"),
        ("The Shapa project.", "The shop_app project."),
    ],
)
def test_sound_alikes_of_your_names_are_put_back(heard: str, fixed: str):
    assert correct(heard, NAMES)[0] == fixed


@pytest.mark.parametrize(
    "heard",
    [
        "the cards are ready",  # sounds like Chordus, but no name goes there
        "open the shape tool",
        "what did I do on shop app recently",  # already the name: kept whole
        "where did I leave off on personal OS",
        "open YouTube Music",
        "go shopping on sunday",
        "close braille",  # too far from Brave to guess
    ],
)
def test_everything_else_is_left_as_heard(heard: str):
    assert correct(heard, NAMES) == (heard, [])


def test_voice_puts_names_back_and_says_so(home, config):
    from tests.test_actions import Env
    from tests.test_phase7 import voice_for

    v, _h = voice_for(Env(home, config))
    v.index.current = lambda: type("I", (), {"names_for_hints": lambda self, n=60: NAMES})()  # type: ignore[method-assign]
    assert v.own_names("Where did I leave off on Cordis?") == "Where did I leave off on Chordus?"
    assert v.name_fixes == [("Cordis", "Chordus")]
