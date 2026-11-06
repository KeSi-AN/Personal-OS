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

## Talking with Donna

Donna answers like a conversation, not a series of one-off commands.

**When she isn't sure, answer however you like.** After "Did you mean Brave?":

| You say | She does |
|---|---|
| "yes", "the second one", "Brave" | Opens the one you picked |
| "No, I wanted the calculator", "I meant Zoom", "actually, Spotify" | Opens that instead. If she's unsure about that too, she asks once more, and gives up after two questions. |
| "No, open Chrome", "forget that, what's the weather?" | Does the new request instead |
| "No", "never mind", or nothing | Leaves it |

Answers the rules can't place ("the one with the lion logo", "the maths thing") go to the local model, which only decides how to read your answer. Whatever it picks still goes through the same checks as any command.

**Changing your mind during a confirmation.** "Shall I set a reminder at 16:34?" → "No, make it 5 pm instead" changes the request rather than dropping it. "Yes but in VS Code" works the same way. As before, only a clear yes approves anything.

**Follow-ups without the hotkey.** After a reply, a soft tone means she's listening for a few more seconds (6 by default), and the popup says *Anything else?* Just keep talking: "and open the calculator", "what about yesterday?". Say "thanks" or "that's all", or stay quiet, and she stops listening. A window that closes unused isn't logged.

**Short-term memory.** For five minutes she remembers what was said, so "open it", "make it 5 pm instead" or "and yesterday?" make sense. This is kept in memory only and forgotten when voice restarts. The action planner sees your words and the results of Donna's own commands, never answers built from your captured activity (ADR-050).

```toml
[voice]
followup_s = 6.0   # seconds to listen after a reply; 0 turns follow-ups off
```

Follow-ups keep the microphone open a few seconds longer after each reply. Audio still stays in memory and is never written to disk, and the tone and popup show when it's listening. Set `followup_s = 0` if you'd rather press the hotkey every time.

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

**Reminders show here too.** When a reminder is due, the popup opens with it and stays until you click **Dismiss**, and Donna says it when she isn't busy talking to you. Windows hides its own notification banners while a full-screen app is running or Do Not Disturb is on, so a Windows notification is used only when the popup can't open. A new briefing shows its title here; the briefing itself is on the dashboard and in the notification centre.

**Setting, changing and cancelling reminders.** These are understood by rules, not guessed by the model, and Donna always says back what she set:

| You say | She does |
|---|---|
| "Remind me in 10 minutes to stretch", "remind me to call mum at 5 p.m." | Sets it: *I'll remind you at 17:00 today: "call mum"* |
| "Remind me in a minute" (and nothing more) | Listens a moment longer, then asks *What should I remind you about?* |
| "Remind me to stretch" | Asks *When should I remind you to stretch?* |
| "Edit that reminder to say go for a stretch", "change it to 5 pm", "push it back 10 minutes" | Changes the reminder you set last |
| "Move the water reminder to 6", "cancel the stretch reminder" | Changes or cancels the one whose words match |
| "Cancel that reminder", "cancel all my reminders" | Cancels the last one, or all of them |

**Asking about reminders.** "What reminders do I have?" or "What did I ask you to remind me?" is answered from your reminders: what's coming up, and what went off in the last day. Times can be said the way speech recognition writes them ("2.10", "5 p.m.").

It needs the WebView2 runtime, which Windows 11 includes. Without it, voice works as before with no popup, and `doctor` says why on its `popup` line.

## Wake word: "Donna"

Say **"Donna"**, **"Hey Donna"**, **"Hello Donna"** or **"Hi Donna"**, then your request. You can say both in one breath ("Donna, open Brave") or pause after "Donna" and then speak. While she's answering, saying "Donna" stops the reply and listens, and **"stop talking"**, **"stop speaking"**, **"be quiet"** or **"shut up"** just stop it.

This is the wake word from [JARVIS](https://github.com/Namish-Dev/JARVIS): **Vosk** (`vosk-model-small-en-us-0.15`) transcribes what it hears, on this laptop, and voice wakes when a word in `voice.wake_words` (default `["donna"]`) is in it as a whole word, so "Madonna" and "gonna" don't count. It reacts mid-sentence and hands the whole sentence to speech recognition, so nothing you said after "Donna" is lost. Nothing is stored.

```powershell
uv run personal-os voice setup        # once: downloads the models (speech in, wake word, voice)
uv run personal-os voice wake on      # or off
uv run personal-os voice status       # "wake word ... listening", and what it last heard
```

### Talking over a reply, and the speakers

Voice keeps the microphone open and takes **what the speakers are playing out of it**, with the WebRTC echo canceller (Windows' loopback of your default output is the reference). That covers Donna's own voice and any video, music or call on the PC. It measures your speakers' delay by itself (an HDMI TV: about 240 ms). So with `voice.barge_in = "voice"` (the default):

- just start talking while she answers, and the reply stops and she listens;
- the wake word doesn't trigger on her own voice or on a video saying "Donna".

`voice status` shows `echo cancel on`, the reference device and the delay. If you switch the default output (TV to headphones), voice notices within 10 s and cancels the new device. If echo cancelling can't start (no loopback device), barge-in falls back to the older echo gate, which is off while other apps play sound, and the hotkey always works.

### Speech recognition

As in JARVIS: **Whisper "small"**, the multilingual model, told the language is English, with no prompt. It was trained on many more accents than the English-only models: here it heard "Can you open Brave for me?" where `small.en` heard "open pray for me". It runs through faster-whisper (the same model weights as JARVIS's openai-whisper, without PyTorch), about 1.7 s a phrase on this laptop. `voice.stt_model` chooses it; `voice.stt_hints = true` adds a prompt of your app and folder names.

After a reply, voice keeps listening for 10 seconds (`voice.followup_s`), as JARVIS does, so a follow-up needs no wake word.

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
