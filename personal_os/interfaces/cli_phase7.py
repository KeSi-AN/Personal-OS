"""Phase 7 commands: voice, briefing, google, mcp, plugins."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Annotated, Any

import typer

from personal_os.config.loader import set_value
from personal_os.core import runtime
from personal_os.core.clock import SystemClock
from personal_os.interfaces.cli import _config, _open, _state, app

voice_app = typer.Typer(
    no_args_is_help=True, help="Voice: push-to-talk, wake word, spoken replies."
)
app.add_typer(voice_app, name="voice")
google_app = typer.Typer(no_args_is_help=True, help="Google Calendar and Gmail (read-only).")
app.add_typer(google_app, name="google")
plugins_app = typer.Typer(no_args_is_help=True, help="Your own tools, loaded from plugins/.")
app.add_typer(plugins_app, name="plugins")


# --- voice ---------------------------------------------------------------------------------------


@voice_app.command("on")
def voice_on() -> None:
    """Turn voice on (push-to-talk Ctrl+Alt+Shift+V); the daemon starts it within 30 s."""
    from personal_os.voice.main import status as voice_proc_status
    from personal_os.voice.stt import Transcriber

    cfg = set_value("voice.enabled", True, _state.home)
    if not Transcriber(cfg.data_dir, cfg.voice.stt_model).available:
        typer.echo("Note: the speech model isn't downloaded yet; run `personal-os voice setup`.")
    if not runtime.daemon_info(cfg.runtime_dir).running:
        typer.echo("Voice on, but the daemon isn't running: `personal-os daemon start` starts it.")
        return
    typer.echo("Voice on. Starting it (up to 30 s)...", nl=False)
    deadline = time.monotonic() + 45
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = voice_proc_status(cfg.runtime_dir)
        if state.get("running") and state.get("state") == "ready":
            break
        time.sleep(1)
        typer.echo(".", nl=False)
    typer.echo("")
    if state.get("error"):
        typer.echo(f"Problem: {state['error']}.")
    elif state.get("running"):
        typer.echo("Ready. Press Ctrl+Alt+Shift+V, wait for the beep, and speak.")
    else:
        typer.echo("It hasn't started yet. Check `personal-os voice status` in a minute.")


@voice_app.command("off")
def voice_off() -> None:
    set_value("voice.enabled", False, _state.home)
    typer.echo("Voice off (the voice process stops within 10 s).")


@voice_app.command("wake")
def voice_wake(state: Annotated[str, typer.Argument(help="on or off")]) -> None:
    """Turn the wake word ("hey Donna") on or off."""
    from personal_os.voice.wake import wake_models

    on = state.lower() in ("on", "true", "yes", "1")
    cfg = set_value("voice.wake_word_enabled", on, _state.home)
    if on and not wake_models(cfg.data_dir):
        folder = cfg.data_dir / "voice" / "wakewords"
        typer.echo(
            f"Wake word on, but there's no model yet: train one (docs/VOICE.md) and put the .onnx in {folder}."
        )
        return
    typer.echo(f"Wake word {'on' if on else 'off'}.")


@voice_app.command("setup")
def voice_setup() -> None:
    """Download the speech model (faster-whisper small.en, about 480 MB) if it's missing."""
    from faster_whisper import download_model

    from personal_os.voice.stt import model_dir

    cfg = _config()
    target = model_dir(cfg.data_dir, cfg.voice.stt_model)
    if (target / "model.bin").exists():
        typer.echo(f"Already there: {target}")
        return
    typer.echo(f"Downloading {cfg.voice.stt_model} to {target} ...")
    download_model(cfg.voice.stt_model, output_dir=str(target))
    typer.echo("Done.")


@voice_app.command("status")
def voice_status() -> None:
    from personal_os.voice.stt import Transcriber
    from personal_os.voice.tts import SpeechifyVoice
    from personal_os.voice.wake import wake_models

    cfg = _config()
    v = cfg.voice
    from personal_os.voice.main import status as voice_proc_status

    running = voice_proc_status(cfg.runtime_dir)
    typer.echo(
        f"voice        {'on' if v.enabled else 'off'}"
        + (f", process {running['state']}" if running else "")
    )
    typer.echo(
        f"speech in    {v.stt_model} ({'ready' if Transcriber(cfg.data_dir, v.stt_model).available else 'not downloaded'})"
    )
    models = wake_models(cfg.data_dir)
    typer.echo(
        f"wake word    {'on' if v.wake_word_enabled else 'off'}; model: {', '.join(m.name for m in models) or 'none yet (docs/VOICE.md)'}"
    )
    key = "key stored" if SpeechifyVoice.key() else "no key"
    typer.echo(
        f"speech out   {v.tts}"
        + (f" (Speechify: {key}; spoken text is sent to Speechify)" if v.tts == "speechify" else "")
    )
    if running.get("error"):
        typer.echo(f"problem      {running['error']}")
    if v.enabled and not running.get("running"):
        if runtime.daemon_info(cfg.runtime_dir).running:
            typer.echo(
                "note         starting: the daemon starts voice within 30 s of turning it on"
            )
        else:
            typer.echo(
                "problem      the daemon isn't running, so voice can't: personal-os daemon start"
            )
    typer.echo(f"mic floor    {v.mic_floor} (`personal-os voice mic` shows your speech level)")
    typer.echo(f"windows voice {v.windows_voice}")


@voice_app.command("log")
def voice_log(last: Annotated[int, typer.Option(help="How many exchanges.")] = 10) -> None:
    """What voice heard, what it did, and how long each step took (kept on this laptop)."""
    from personal_os.voice.main import history

    cfg = _config()
    entries = history(cfg.runtime_dir, last)
    if not entries:
        typer.echo("Nothing yet. Press Ctrl+Alt+Shift+V, wait for the beep, and speak.")
    for x in entries:
        when = x.get("at", "")[11:19]
        times = ", ".join(
            f"{k[:-2]} {x[k]} s" for k in ("listen_s", "handle_s", "speak_s", "total_s") if k in x
        )
        typer.echo(f"{when}  [{x.get('trigger', '?')}] heard: {x.get('heard') or '(nothing)'}")
        typer.echo(f"          as: {x.get('route', '?')}   ({times})")
        if x.get("reply"):
            typer.echo(f"          reply: {x['reply'][:200]}")
        if x.get("error"):
            typer.echo(f"          ERROR: {x['error']}")


@voice_app.command("mic")
def voice_mic() -> None:
    """Test the microphone: say a sentence after the beep; shows your level and what was heard."""
    import numpy as np
    import sounddevice as sd

    from personal_os.voice.audio import CALIBRATE_BLOCKS, record_utterance, threshold
    from personal_os.voice.stt import Transcriber
    from personal_os.voice.tts import beep

    cfg = _config()
    floor = cfg.voice.mic_floor
    try:
        device = sd.query_devices(kind="input")
    except Exception as exc:
        typer.echo(f"No microphone found: {exc}")
        raise typer.Exit(1) from None
    typer.echo(f"microphone   {device['name']}")
    typer.echo("After the beep, say a sentence, e.g. 'what did I do today'.")
    beep()
    levels: list[float] = []
    audio = record_utterance(floor=floor, levels=levels)
    if not levels:
        typer.echo("No sound came in at all: check Windows Settings > Privacy > Microphone.")
        raise typer.Exit(1)
    noise = float(np.median(levels[:CALIBRATE_BLOCKS]))
    loudest = max(levels[CALIBRATE_BLOCKS:] or levels)
    need = threshold(noise, floor)
    typer.echo(f"background   {noise:.4f}")
    typer.echo(f"your voice   {loudest:.4f}  (counts as speech above {need:.4f})")
    if audio is None:
        typer.echo("Heard: nothing. Your voice didn't get above the speech level.")
        if loudest > noise * 1.5:
            suggest = round(max(loudest / 2, noise * 2, 0.0003), 4)
            typer.echo(
                f"Try a lower floor: personal-os config set voice.mic_floor {suggest}, "
                "or raise the mic's input volume (Settings > System > Sound > Input)."
            )
        else:
            typer.echo("The level barely moved: is the right mic the default input, and unmuted?")
        return
    stt = Transcriber(cfg.data_dir, cfg.voice.stt_model)
    if not stt.available:
        typer.echo("Speech was detected, but the speech model isn't downloaded: voice setup.")
        return
    typer.echo(f"Heard: {stt.transcribe(audio) or '(speech, but no words recognised)'}")


@voice_app.command("voices")
def voice_voices() -> None:
    """The Windows voices you can choose (personal-os config set voice.windows_voice NAME)."""
    from personal_os.voice.tts import windows_voices

    cfg = _config()
    for name, info in windows_voices():
        mark = "  <- in use" if name == cfg.voice.windows_voice else ""
        typer.echo(f"{name:<28} {info}{mark}")


@voice_app.command("test")
def voice_test(
    text: Annotated[str, typer.Argument()] = "Hello, this is Donna. Voice is working.",
) -> None:
    """Say something with the configured voice (falls back to local voices)."""
    from personal_os.voice.tts import Speaker, TTSError

    cfg = _config()
    try:
        used = Speaker(cfg.voice, cfg.data_dir).say(text)
    except TTSError as exc:
        typer.echo(f"No voice worked: {exc}")
        raise typer.Exit(1) from None
    typer.echo(f"Spoke with the {used} voice.")


@voice_app.command("speechify-key")
def voice_speechify_key() -> None:
    """Store your Speechify API key in Windows Credential Manager (typed, never echoed)."""
    import keyring

    from personal_os.voice.tts import KEYRING_SERVICE, SPEECHIFY_KEY

    key = typer.prompt("Speechify API key", hide_input=True).strip()
    if not key:
        raise typer.Exit(1)
    keyring.set_password(KEYRING_SERVICE, SPEECHIFY_KEY, key)
    set_value("voice.tts", "speechify", _state.home)
    typer.echo("Stored. Spoken replies now use Speechify (only the spoken text is sent).")
    typer.echo("Try: personal-os voice test")


@voice_app.command("speechify-voices")
def voice_speechify_voices() -> None:
    """List the Speechify voices your key can use."""
    import urllib.request

    from personal_os.voice.tts import SpeechifyVoice

    key = SpeechifyVoice.key()
    if not key:
        typer.echo("No key stored: personal-os voice speechify-key")
        raise typer.Exit(1)
    req = urllib.request.Request(
        "https://api.speechify.ai/v1/voices", headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        voices = json.load(resp)
    for v in voices if isinstance(voices, list) else voices.get("voices", []):
        typer.echo(f"{v.get('id', '?'):<28} {v.get('display_name', '')}  {v.get('gender', '')}")
    typer.echo("Choose one: personal-os config set voice.speechify_voice ID")


@voice_app.command("demo")
def voice_demo(
    phrases: Annotated[
        list[str] | None, typer.Argument(help="What to 'say' (default: a briefing and a reminder).")
    ] = None,
) -> None:
    """Hands-free run without a microphone: each phrase is spoken by the Windows voice into a
    WAV, transcribed by the speech model, handled, and the reply is spoken aloud."""
    import tempfile
    import wave

    import numpy as np
    from scipy.signal import resample_poly

    from personal_os.voice.main import Voice
    from personal_os.voice.tts import WindowsVoice

    cfg = _config()
    phrases = phrases or ["Hey Donna, brief me", "Hey Donna, remind me in two minutes to stretch"]
    voice = Voice(cfg)
    queued: list[str] = []

    def heard(text: str) -> np.ndarray:
        path = Path(tempfile.gettempdir()) / "pos-demo-in.wav"
        WindowsVoice().synthesize(text, path)
        with wave.open(str(path)) as w:
            rate = w.getframerate()
            audio = (
                np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
                / 32768
            )
        resampled: np.ndarray = resample_poly(audio, 16000, rate).astype(np.float32)
        return resampled

    def listen() -> str:  # instead of the microphone: the next queued phrase ("yes" for confirms)
        text = queued.pop(0) if queued else "yes"
        t0 = time.monotonic()
        heard_text = voice.stt.transcribe(heard(text), vocabulary=voice.vocabulary())
        typer.echo(f"  heard ({time.monotonic() - t0:.1f} s): {heard_text}")
        return heard_text

    voice.listen = listen  # type: ignore[method-assign]
    for phrase in phrases:
        typer.echo(f"\nyou: {phrase}")
        queued.append(phrase)
        t0 = time.monotonic()
        reply = voice.handle(voice.listen())
        typer.echo(f"donna ({time.monotonic() - t0:.0f} s): {reply}")
        if reply:
            voice.say(reply)


# --- briefing --------------------------------------------------------------------------------------


@app.command("briefing")
def briefing_cmd(
    evening: Annotated[bool, typer.Option("--evening", help="End-of-day summary instead.")] = False,
    speak: Annotated[bool, typer.Option("--speak", help="Also read it aloud.")] = False,
    enable: Annotated[
        bool | None, typer.Option("--enable/--disable", help="Deliver one each day automatically.")
    ] = None,
) -> None:
    """Your morning briefing (or end-of-day summary) now."""
    from personal_os.context import briefing
    from personal_os.voice.main import google_client

    cfg = _config()
    if enable is not None:
        cfg = set_value("briefings.enabled", enable, _state.home)
        typer.echo(
            f"Daily briefings {'on' if enable else 'off'} (morning after {cfg.briefings.morning_after}, "
            f"evening after {cfg.briefings.evening_after})."
        )
        return
    conn, _ = _open(cfg, None)
    b = briefing.build(
        conn, SystemClock(), "evening" if evening else "morning", google=google_client(cfg)
    )
    typer.echo(b.title)
    for line in b.lines:
        typer.echo(f"  {line}")
    for note in b.notes:
        typer.echo(f"  ({note})")
    if speak:
        from personal_os.voice.tts import Speaker

        Speaker(cfg.voice, cfg.data_dir).say(f"{b.title}. " + " ".join(b.lines))


# --- google --------------------------------------------------------------------------------------------


@google_app.command("connect")
def google_connect(
    client_secret: Annotated[
        Path,
        typer.Argument(
            exists=True, dir_okay=False, help="client_secret_....json from Google Cloud"
        ),
    ],
) -> None:
    """Sign in once in your browser (read-only Calendar and Gmail headers). See docs/GOOGLE.md."""
    import webbrowser

    from personal_os.integrations import google

    cfg = _config()
    typer.echo("Opening Google sign-in in your browser ...")
    try:
        google.connect(cfg.data_dir, client_secret, webbrowser.open)
    except google.GoogleError as exc:
        typer.echo(f"Not connected: {exc}")
        raise typer.Exit(1) from None
    typer.echo("Connected. Try: personal-os google status")


@google_app.command("status")
def google_status() -> None:
    from personal_os.integrations import google

    cfg = _config()
    if not google.connected(cfg.data_dir):
        typer.echo(
            "Not connected. See docs/GOOGLE.md, then: personal-os google connect CLIENT_SECRET.json"
        )
        return
    g = google.Google(cfg.data_dir)
    try:
        start, end = google.today_range(SystemClock().now())
        events = g.calendar(start, end)
        inbox = g.inbox(top=0)
    except google.GoogleError as exc:
        typer.echo(f"Connected, but Google answered: {exc}")
        raise typer.Exit(1) from None
    typer.echo(
        f"Connected. Today: {len(events)} calendar event(s); {inbox.unread} unread in the primary inbox."
    )


@google_app.command("disconnect")
def google_disconnect() -> None:
    """Revoke access at Google and delete the token here."""
    from personal_os.integrations import google

    cfg = _config()
    revoked = google.disconnect(cfg.data_dir)
    typer.echo(
        "Disconnected."
        + (
            ""
            if revoked
            else " (Google didn't confirm the revoke; check myaccount.google.com/permissions)"
        )
    )


# --- mcp, plugins ------------------------------------------------------------------------------------------


@app.command("mcp")
def mcp_cmd(
    show_config: Annotated[
        bool, typer.Option("--config", help="Print a Claude Desktop config entry.")
    ] = False,
) -> None:
    """Serve your context, read-only, over MCP (stdio) for assistants like Claude Desktop."""
    if show_config:
        exe = Path(sys.executable)
        entry: dict[str, Any] = {
            "mcpServers": {
                "personal-os": {
                    "command": str(exe),
                    "args": ["-m", "personal_os.interfaces.cli", "mcp"],
                }
            }
        }
        typer.echo(json.dumps(entry, indent=2))
        typer.echo("\nAdd this to Claude Desktop's config (Settings → Developer → Edit Config).")
        return
    from personal_os.interfaces.mcp_server import run

    run()


@plugins_app.command("list")
def plugins_list() -> None:
    from personal_os.actions.plugins import load_plugins, plugin_dir
    from personal_os.actions.tools import build_registry

    cfg = _config()
    typer.echo(
        f"plugins {'on' if cfg.actions.plugins_enabled else 'off'} (actions.plugins_enabled); folder {plugin_dir(cfg.data_dir)}"
    )
    _tools, report = load_plugins(cfg.data_dir, {t.name for t in build_registry().all()})
    for name in report.loaded:
        typer.echo(f"  loaded {name}")
    for err in report.errors:
        typer.echo(f"  ERROR  {err}")
    if not report.loaded and not report.errors:
        typer.echo("  (none; `personal-os plugins example` writes one to start from)")


@plugins_app.command("example")
def plugins_example() -> None:
    from personal_os.actions.plugins import EXAMPLE, plugin_dir

    cfg = _config()
    path = plugin_dir(cfg.data_dir) / "word_count.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        typer.echo(f"{path} already exists.")
        return
    path.write_text(EXAMPLE, encoding="utf-8")
    typer.echo(
        f"Wrote {path}. Turn plugins on: personal-os config set actions.plugins_enabled true"
    )


_ = runtime
