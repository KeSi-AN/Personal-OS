# Voice: push-to-talk, "hey Donna", and the voice that answers

## Using it

```powershell
uv run personal-os voice on        # the daemon starts the voice process within 30 s
uv run personal-os voice status
```

Press **Ctrl+Alt+Shift+V**, wait for the beep, and speak. It stops listening at the first pause (at most 15 s). Things to say:

| Say | What happens |
|---|---|
| "brief me", "good morning" | Your morning briefing |
| "end of day", "how was my day" | The end-of-day summary |
| "where did I leave off on personal-os?" | `ask`; the answer is spoken (the dashboard has the evidence) |
| "remind me in 20 minutes to stretch", "take a screenshot", "pause the music" | Done straight away (tier 1) |
| "commit my changes in auth-service" | Read back to you; only a spoken **yes** does it |
| "remember that standup moved to 10:30" | Read back; stored only after **yes** |
| "stop" | Stops speaking |

Speech is recognised **on this laptop** with faster-whisper `small.en`, on the CPU, so the GPU stays free for the chat model. That takes about 3–5 s per phrase here. Audio stays in memory; only the recognised command text is kept, in the action log, when it triggers an action. `uv run personal-os voice demo` runs the whole path without a microphone, using a synthesized voice.

## Is it working?

```powershell
uv run personal-os voice status    # "process ready" means it's listening for the hotkey
uv run personal-os voice log       # what it heard, what it took that as, the reply, how long each step took
```

The dashboard's Today page shows the same in a **Voice** card.

**No beep when you press the hotkey?** Then the voice process isn't running. It runs under the daemon, so check `personal-os status`. The tray's **Stop capture and voice** stops both until you start them again (`personal-os daemon start`, or log in again). `doctor` reports *voice is on but its process isn't running*.

## The popup

When a turn starts (the hotkey, the wake word, or talking over a reply), a small window opens in the bottom-right corner, on top, without taking the keyboard from what you're doing. It shows:

- **State:** a pulsing dot while it listens, a spinner while it thinks, moving bars while it speaks.
- **What it heard,** as soon as speech is recognised, then **the reply.**
- **"Did you mean …?"** when it isn't sure what you said.
- **Paused** or **kill switch on**, when they apply.

It closes 5 seconds after the reply. **Pin** keeps it open, **Esc** or ✕ closes it (click it first, since it opens without focus), and **Dashboard** opens the dashboard through the usual one-time login link. A new turn reuses the open window.

It shows only what voice already keeps in its status file, never anything from captured activity, and it writes nothing. It reads the runtime folder about 5 times a second while it's open, and isn't running at all otherwise.

```powershell
uv run personal-os voice popup          # open it now
uv run personal-os voice popup --demo   # cycle through the states with fake data
```

Settings, in `config.toml`:

```toml
[voice]
popup = true                      # false: no popup
popup_position = "bottom-right"   # bottom-right | top-right | bottom-left | top-left
popup_autoclose_s = 5.0           # seconds after the reply, unless pinned
```

It needs the WebView2 runtime, which Windows 11 includes. Without it, voice works as before with no popup, and `doctor` says why on its `popup` line.

## Wake word: "hey Donna"

openWakeWord has no ready-made model for "hey Donna", so you train one once. It takes about 1.5 hours on Google Colab, mostly waiting. A two-word phrase with "hey" is more reliable than "Donna" alone.

1. Open the community notebook [openwakeword-colab-2026](https://github.com/alfiedennen/openwakeword-colab-2026). It's the maintained one; the original openWakeWord notebook no longer runs.
2. In its settings cell, set the target phrase to `hey donna` and the model name to `hey_donna`. If you like, add `hey dona` as a second spelling, which helps the synthetic voices.
3. **Runtime → Run all.** At the end, download `hey_donna.onnx`.
4. Put it in `%LOCALAPPDATA%\PersonalOS\voice\wakewords\`, then:

```powershell
uv run personal-os voice wake on     # or: tray → Wake word "hey Donna"; or dashboard → Privacy
uv run personal-os voice wake off
```

With the wake word on, the microphone is read continuously and scored on this laptop, 80 ms at a time. Nothing is recorded or transcribed until it hears the phrase. If it triggers too often or too rarely, adjust `voice.wake_threshold` (default 0.5; higher means fewer false starts).

## The voice that answers

| Voice | Where it runs | Setup |
|---|---|---|
| **Windows** (default) | This laptop | None. Female by default (Zira, US English); `personal-os voice voices` lists the others (Hazel is UK English), and `personal-os config set voice.windows_voice "Microsoft Hazel Desktop"` switches. |
| **Piper** (natural, local) | This laptop | `uv add piper-tts`, then put a voice's `.onnx` and `.onnx.json` in `voice\piper\` ([voices](https://huggingface.co/rhasspy/piper-voices)), then `personal-os config set voice.tts piper` |
| **Speechify** (natural, cloud) | Speechify's servers: **the sentence being spoken is sent to them** | See below |

If the chosen voice fails, the next local one speaks instead, so a reply is never lost.

### Speechify

A Speechify **Premium** subscription is the reading app. The **API** is a separate developer product with its own key; it has a free monthly allowance ([speechify.ai/api](https://speechify.ai/api)).

1. Sign up at the Speechify API console and create an API key.
2. Store it; you type it at a hidden prompt and it goes into Windows Credential Manager:

   ```powershell
   uv run personal-os voice speechify-key
   uv run personal-os voice speechify-voices          # list voices
   uv run personal-os config set voice.speechify_voice <id>
   uv run personal-os voice test "Hello, this is Donna."
   ```

Only the reply being spoken is sent: never page text, evidence or anything else stored. To go back to local voices: `personal-os config set voice.tts windows`.
