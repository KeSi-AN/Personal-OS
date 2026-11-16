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
| "where did I leave off on personal-os?", "what did I do today?" | Answered from your history exactly as `personal-os ask` answers it; the evidence is in the popup |
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

## The island

Donna lives at the top of the screen, like the Mac's Dynamic Island. Nothing is there while she's idle: when you say "Donna", press the hotkey, ask for a page or a reminder comes due, a black island drops down from the top edge at the centre of the screen your mouse is on, and slides back up into it when you're done. `voice.island_idle = "pill"` keeps a small pill there instead (the time, a dot for Donna, bars while music plays), which also opens when you rest the pointer on it.

- **With the pill kept** (`island_idle = "pill"`), rest the pointer on it and it grows open; move away and it shrinks back.
- **Over an app** (your browser's tabs, a title bar), the pill turns see-through and lets clicks through to what's under it, fading almost away while the pointer is on it. To open it there, push the pointer against the top edge of the screen on it for half a second.
- **Today** has a box beside each to-do to tick it off (click again to undo) and a box to add one; a time at the end ("call mum tomorrow 5pm", "by friday") becomes when it's due. **Notes** shows your latest notes and a box to jot one, into the inbox or a project you pick.
- **Typing:** click in the box on the Donna page and type; Enter sends it.
- **While she's talking**, say "Donna" to stop her and talk, or "stop talking" / "never mind" to just stop her.
- **Say "Donna"** (or press the hotkey) and it opens on the **Donna** page, which is the popup below: what you said, her reply, questions, reminders, the box to type in, Talk, Stop, Pin.
- **Swipe** sideways with two fingers on the touchpad, click the **‹ ›** arrows, the page names or the dots, or press **← →** once you've clicked it, to move between **Donna**, **Today** (time worked, what's coming up, to-dos), **Notes**, **Music** (what's playing in the browser, with play, pause, next) and **Projects** (where you left off, the next to-do). **Open full ↗** opens that page of the dashboard.
- **Say "show my projects"**, "show today", "show my notes", "show the music" or "hide the island".
- It hides while a full-screen app, game or video is on its screen, unless you call her or a reminder comes due.

The cards are read from your history only while you look at them; the pill shows nothing from it. `voice.popup_style = "corner"` brings back the corner popup below, opened only during a turn. Set `POS_ISLAND_DEBUG=1` before running `python -m personal_os.interfaces.island` to see its decisions.

## Taking a break

Say **"Donna, take 10"**, **"take five"**, **"rest up for 20 minutes"**, **"take a breather"** or **"be quiet for half an hour"**, and Donna stops listening for that long: the wake word is off and she doesn't listen for a follow-up after replies. "Take a break" with no time is 10 minutes, and the longest rest is 4 hours. On their own, "be quiet" and "stop listening" still just stop her talking.

Add **"... and remind me once you're ready"** (or "tell me when you're back") and she says **"I'm back"** when the time is up. Woken early by a press, she says nothing, since you're already there.

**Several things at once:** "pause the music and take 10", "remind me in 5 minutes to stretch, then take 5". When every part is something the rules do by themselves, all of them run, in the order said, at once. Otherwise the whole sentence goes to Donna's agent, which can also rest her (`take_rest`) alongside other things ("take a break and play something calm"). A reminder's own "and" ("remind me to call mum and dad") is never split.

To wake her early, press the **hotkey** or **Talk** on the island (it says **Wake & talk** while she rests): she's awake again and listening at once. When the time is up she's back by herself. Reminders still pop up and are said while she rests. The pill shows **Resting · 8 min** with a grey dot.

## The island after you use it

Once you've clicked, typed or scrolled in the island, it closes **5 seconds** after you're done (your last click or keystroke, or the pointer leaving, whichever is later), unless the pointer is still on it, it's pinned, or she's listening, thinking or speaking. Before, using it could keep it open for a whole reply's reading time.

## Your music

Donna plays, pauses and skips **your music source only**, set in the `[music]` section of the config or on the dashboard's Settings page (**Music from**). A video playing in another tab, or a lecture, is left alone.

```toml
[music]
source = "youtube_music"   # youtube_music | spotify | youtube | any (whatever plays in the browser)
open_in = "auto"           # auto: its installed app if there is one, else the website | app | browser
default_query = ""         # "play music" with nothing open plays this search; empty: opens it and asks
```

- **"Play music"**: resumes your source if it's open. If nothing is open, it opens your source (its installed app with `open_in = "auto"`, for example YouTube Music installed from Brave) and asks what to play, or plays `default_query` if you set one.
- **"Pause"**, **"next song"**, **"previous"**: only your source. If it isn't playing, Donna says so ("Nothing's playing on YouTube Music") instead of pausing something else. "Pause the video" still pauses the video in the browser.
- **"Play Circles by Post Malone"**: searches your source and starts the first result, in its installed web app when that's where you listen. Say "on YouTube" for a video.
- **What's playing**, the island's **Music** page and the pill's bars all show only your source. When it isn't open, the Music page has an **Open** button.
- **Spotify (prepared):** its app or website is found, paused, skipped and read like the others, and "play <song>" opens Spotify's search for it. Starting a particular Spotify song needs Spotify's own sign-in (its Web API), which isn't set up yet, so Donna says to press play on the top result.

Players are found two ways: your source's website tabs, through the browser extension, and its installed app, through Windows' media sessions (the same list the volume flyout shows). An installed app's session carries the app's own id, so YouTube Music installed from Brave isn't confused with a YouTube tab in Brave.

## The popup (corner style)

When a turn starts (the hotkey, the wake word, or talking over a reply), a small window opens in the bottom-right corner, on top, without taking the keyboard from what you're doing. It shows:

- **State:** a pulsing dot while it listens, a spinner while it thinks, moving bars while it speaks.
- **What it heard,** as soon as speech is recognised, then **the reply.**
- **"Did you mean …?"** when it isn't sure what you said.
- **Paused** or **kill switch on**, when they apply.

It closes 5 seconds after the reply. While she's listening, thinking or talking, **Stop** stops her there: nothing more is said or done, and a model call in progress is hung up. **Esc** or ✕ does the same and closes the window (click it first, since it opens without focus). **Pin** keeps it open, and **Dashboard** opens the dashboard through the usual one-time login link. A new turn reuses the open window. While she thinks, a line under the state says what she's doing ("Using recall…"), and a yes-or-no question shows as it's asked.

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

Say **"Donna"**, **"Hey Donna"**, **"Hello Donna"** or **"Hi Donna"**, then your request. You can say both in one breath ("Donna, open Brave") or pause after "Donna" and then speak. While she's thinking or answering, saying "Donna" stops her and listens, and **"stop talking"**, **"stop speaking"**, **"be quiet"**, **"shut up"**, **"stop it"**, **"cancel that"** or **"never mind"** just stop her.

Only the last 3 seconds before "Donna" are handed to speech recognition, and what came before "Donna" is dropped from the request. If what was recorded has no "Donna" in it at all, the wake word was set off by something else (a video, people talking) and nothing is done. If only one word that isn't a command is caught ("code."), she asks you to say it again instead of guessing.

**Closing apps:** "close VS Code", "quit Brave" close that app's windows the way their ✕ does, so an app can still ask to save. "Close the tab" closes the browser tab in front. Both are understood by rules, without the model.

**Music while you talk:** for a whole conversation (listening, thinking, talking and the follow-ups), every other app making sound plays at a fifth of its volume, as on a smart speaker, so she can hear you over it; it's put back when the conversation ends. `voice.duck` sets the share (`1` turns this off). If voice stops mid-conversation, the volumes are put back the next time it starts.

This is the wake word from [JARVIS](https://github.com/Namish-Dev/JARVIS): **Vosk** (`vosk-model-small-en-us-0.15`) transcribes what it hears, on this laptop, and voice wakes when a word in `voice.wake_words` (default `["donna"]`) is in it as a whole word, so "Madonna" and "gonna" don't count. It reacts mid-sentence and hands the whole sentence to speech recognition, so nothing you said after "Donna" is lost. Nothing is stored.

```powershell
uv run personal-os voice setup        # once: downloads the models (speech in, wake word, voice)
uv run personal-os voice wake on      # or off
uv run personal-os voice status       # "wake word ... listening", and what it last heard
```

### Talking over a reply, and the speakers

Voice keeps the microphone open and takes **what the speakers are playing out of it**, with the WebRTC echo canceller (Windows' loopback of your default output is the reference). That covers Donna's own voice and any video, music or call on the PC. It measures your speakers' delay by itself (an HDMI TV: about 240 ms). That keeps the wake word from triggering on her own voice or on a video saying "Donna".

While she talks, by default (`voice.barge_in = "wake"`), **"Donna"** stops her and listens, and **"stop talking"**, **"never mind"** and the other stop phrases just stop her, as with a smart speaker; so do the hotkey and the popup. Plain talking over her doesn't, because on laptop speakers enough of her voice leaks past the canceller to be taken for you (seen: her "Did you mean vscode?" heard back as "Did you mean?", cutting the question off). With `voice.barge_in = "voice"`, any speech interrupts her too, and anything heard that matches what she was just saying is thrown away as her own voice.

Only speech while a reply is actually sounding counts, after the first ~0.3 s of each sentence, and it has to last about a third of a second. A reply to a turn that itself began by talking over a reply can't be stopped by just any speech (the wake word, stop phrases, the hotkey and the popup still work), so her own voice leaking through can never set off a loop of her answering herself. The speaker delay starts at 50 ms and is re-measured while she talks; the reference is fed slightly early, since one fed late cancels nothing.

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
