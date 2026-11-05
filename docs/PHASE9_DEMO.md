# Phase 9a demo: Donna as one assistant

Phase 9a turns Donna from a set of lanes into one assistant (design: [AGENT.md](AGENT.md)):

- she talks with you about anything;
- she looks at what's open right now, or at your history, when the question is about you;
- she does things through the same tools, policy and confirmations as before.

All of it runs locally on `qwen3:4b`.

## 1. Tests

```powershell
uv run pytest -q tests/test_agent.py
```

These cover:
- answering without tools;
- looking, then answering from the results;
- a repeated call running only once;
- the last step only being able to answer;
- `recall` passing straight through;
- an action after reading what's open needing a yes;
- the kill switch;
- tabs going from browser to host to observer to "now", with blocked, incognito and stale tabs left out.

## 2. Talk to her in the terminal

```powershell
uv run personal-os chat "What's the difference between RAM and storage?"
uv run personal-os chat "How many windows do I have open, and what's in front?"
uv run personal-os chat "Do I have any reminders coming up?"
uv run personal-os chat "Remind me in 45 minutes to review the agent"
uv run personal-os chat "Cancel that reminder"
uv run personal-os chat "What did I work on today?"
```

Anything that needs confirming asks in the terminal.

## 3. By voice

Press **Ctrl+Alt+Shift+V** and ask anything: a general question, "what's open?", "what did I do this morning?", "remind me…".

- **Follow-ups work as before:** keep talking after the tone.
- **Instant commands stay instant:** opening apps, volume and reminders are still handled by the fast rules.
- **Everything else goes to the agent.**

## 4. Open tabs (one-time setup for Brave)

Brave needs the connector registered, and the extension reloaded so it picks up the new build:

```powershell
uv run personal-os browser install --browser brave
```

Then, in Brave:
1. Open `brave://extensions`.
2. Click **reload** on Personal OS.
3. Ask "which tabs do I have open?"
