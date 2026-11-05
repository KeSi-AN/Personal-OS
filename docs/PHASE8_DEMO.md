# Phase 8 demo: the voice popup

Phase 8 adds a small window that shows each voice turn as it happens: listening, what it heard, thinking, the reply being spoken, and done. It opens by itself when you talk to voice and closes a few seconds later. Details: [VOICE.md](VOICE.md#the-popup), decision: ADR-071.

## 1. Tests

```powershell
uv run pytest -q tests/test_popup.py
```

These cover:

- the live turn in the status file, and that the old fields are unchanged;
- atomic writes while a reader polls;
- one popup at a time;
- falling back without pywebview or WebView2;
- the view;
- auto-close and pin.

No window opens and no sound plays.

## 2. See every state, with fake data

```powershell
uv run personal-os voice popup --demo
```

A window opens in the bottom-right corner, on top, without taking focus. Over about 18 seconds it shows:

1. **Listening** (a pulsing dot), then **Thinking** (a spinner) with what was "heard", then **Speaking** (moving bars) with the reply, then **Done**.
2. A second turn from the wake word, with **"Did you mean Brave?"**
3. **Capture is paused**, then **Actions are off (kill switch)**.
4. A last **Done**, after which it closes by itself 5 seconds later.

While it runs:

- Click **Pin**: it stops closing (unpin to give it the full 5 seconds again).
- Click the window, then press **Esc**: it closes.
- Run the command twice: the second says *already open*.

## 3. The real thing

Press **Ctrl+Alt+Shift+V** and say something ("what time is it?"). The popup opens as the beep sounds, shows your words as soon as they're recognised, then the reply, and closes 5 seconds after the reply is spoken. Press the hotkey again while it's open: the same window clears and follows the new turn.

```powershell
uv run personal-os voice popup     # open it by hand; it shows the last turn
uv run personal-os doctor          # the "popup" line: on, the WebView2 version, any last error
```

To turn it off, set `[voice] popup = false` in `config.toml`. Voice works exactly as before.

## 4. What it costs

```powershell
uv run python scripts/measure_budget.py --popup 60
```

This opens the popup, measures it for a minute, closes it, and checks that every process it started has gone. Results are in [BUDGET.md](BUDGET.md#voice-popup-phase-8).
