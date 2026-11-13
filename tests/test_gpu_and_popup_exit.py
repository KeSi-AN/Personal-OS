"""The popup can't outlive its use, and the model can be kept loaded on the GPU from the tray."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from personal_os.config.loader import load_config, set_value
from personal_os.core import modes
from personal_os.daemon import Daemon
from personal_os.interfaces import popup
from personal_os.interfaces.tray import TrayActions
from personal_os.llm.ollama import KEEP_FOREVER, OllamaProvider
from personal_os.llm.provider import Health
from personal_os.voice.popup_view import AutoClose, View
from tests.test_daemon import FakeProbe, FakeWatcher

# --- the popup's process ends when its window can't close ------------------------------------


def test_the_process_ends_when_closing_hangs_or_the_page_stops():
    assert popup.exit_reason(10, last_poll=9, closing_at=None) is None
    assert popup.exit_reason(10, last_poll=9, closing_at=8) is None  # closing, give it a moment
    assert popup.exit_reason(11.5, last_poll=11, closing_at=8) == "closing took too long"
    hidden = popup.exit_reason(200, last_poll=200 - popup.STALL_S, closing_at=None)
    assert hidden == "the window stopped updating"  # a hidden window pauses the page's timers


def test_voice_gone_closes_it_even_pinned_or_held():
    a = AutoClose(5)
    a.pin(True)
    a.hold(True)
    gone = View(False, "not running")
    a.step(gone, 0)
    assert a.step(gone, 4)[0] is False
    assert a.step(gone, 5)[0] is True  # was: open forever, holding the lock


def test_nothing_is_held_once_closing(tmp_path: Path):
    cfg = load_config(tmp_path)
    api = popup.Api(cfg, demo=True)
    api._closing = True
    assert api.hold(True) is False and api._auto.held is False


def test_polling_marks_the_page_alive(tmp_path: Path):
    api = popup.Api(load_config(tmp_path), demo=True)
    api.last_poll = 0.0
    api.view()
    assert api.last_poll > 0


def test_the_page_stops_calling_once_closed():
    html = popup.page()
    assert "let closing = false" in html and "if (closing ||" in html
    assert '$("close").onclick = closeNow' in html


# --- keeping the model on the GPU ---------------------------------------------------------------


def calls_of(provider: OllamaProvider) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    def fake(path: str, body: dict[str, Any], on_text: Any = None) -> dict[str, Any]:
        sent.append(body)
        return {"message": {"content": '{"ok": true}'}}

    provider._with_gpu_fallback = fake  # type: ignore[method-assign,assignment]
    return sent


def test_kept_on_gpu_every_call_says_never_unload(tmp_path: Path):
    from pydantic import BaseModel

    class Ok(BaseModel):
        ok: bool

    cfg = load_config(tmp_path).llm
    p = OllamaProvider(cfg)
    sent = calls_of(p)
    p.warm("15m")
    p.chat_json("s", "u", Ok)
    assert [b["keep_alive"] for b in sent] == ["15m", cfg.keep_alive]  # off: as before
    p.cfg = cfg.model_copy(update={"keep_on_gpu": True})
    sent.clear()
    p.warm("15m")  # the overlay's warm-up can't shorten it either
    p.chat_json("s", "u", Ok)
    assert [b["keep_alive"] for b in sent] == [KEEP_FOREVER, KEEP_FOREVER]
    assert sent[0]["options"]["num_gpu"] == cfg.gpu_layers  # all layers asked for on the GPU


class FakeLLM:
    def __init__(self) -> None:
        self.did: list[str] = []
        self.cfg: Any = None

    def warm(self, keep_alive: str = "15m") -> None:
        self.did.append("warm")

    def unload(self) -> None:
        self.did.append("unload")

    def health(self) -> Health:
        return Health(True, loaded=[("qwen3:4b", 3_800_000_000)])


class FakeTray:
    def __init__(self) -> None:
        self.notes: list[str] = []

    def notify(self, message: str, title: str = "") -> None:
        self.notes.append(message)


def daemon_with(home: Path) -> tuple[Daemon, FakeLLM, FakeTray]:
    cfg = load_config(home)
    llm = FakeLLM()
    d = Daemon(cfg, window_probe=FakeProbe(), session_watcher=FakeWatcher(), use_platform=False,
               llm_provider=llm)  # type: ignore[arg-type]  # fmt: skip
    tray = FakeTray()
    d.tray = tray  # type: ignore[assignment]
    return d, llm, tray


def test_the_tray_toggle_loads_and_keeps_then_unloads(
    home: Path,
):  # load_config() reads PERSONAL_OS_HOME
    d, llm, tray = daemon_with(home)
    assert d._gpu_state() is False
    set_value("llm.keep_on_gpu", True, home)
    d._apply_gpu()
    assert llm.did == ["warm"] and llm.cfg.keep_on_gpu is True  # its own calls keep it now
    assert "on the GPU and stays loaded (3.8 GB" in tray.notes[-1]
    set_value("llm.keep_on_gpu", False, home)
    d._apply_gpu()
    assert llm.did == ["warm", "unload"] and "unloaded" in tray.notes[-1]


def test_gaming_mode_keeps_the_gpu_for_the_game(home: Path):  # load_config() reads PERSONAL_OS_HOME
    d, llm, tray = daemon_with(home)
    set_value("llm.keep_on_gpu", True, home)
    modes.set_mode(d.config.runtime_dir, "gaming", by="dashboard")
    d._apply_gpu()
    assert llm.did == [] and "when gaming mode ends" in tray.notes[-1]


def test_a_failed_load_is_said_plainly(home: Path):  # load_config() reads PERSONAL_OS_HOME
    d, llm, tray = daemon_with(home)
    set_value("llm.keep_on_gpu", True, home)

    def fail(keep_alive: str = "15m") -> None:
        raise ConnectionError("ollama down")

    llm.warm = fail  # type: ignore[method-assign]
    d._apply_gpu()
    assert "Is Ollama running?" in tray.notes[-1]


def test_the_tray_has_the_checkbox():
    actions = TrayActions(
        state=lambda: "capturing", toggle_pause=lambda: None, open_dashboard=lambda: None,
        open_overlay=lambda: None, toggle_kill=lambda: None, restart=lambda: None,
        stop=lambda: None, gpu_state=lambda: True,
    )  # fmt: skip
    assert actions.gpu_state() is True
    import inspect

    from personal_os.interfaces import tray

    assert "Keep model loaded on GPU" in inspect.getsource(tray.Tray.__init__)
