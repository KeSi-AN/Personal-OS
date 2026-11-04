# Phase 6 demo: dashboard, tray, overlay, phone

Phase 6 makes everything controllable and inspectable without the terminal.

| Where | What you can do |
|---|---|
| **Tray icon** (green: capturing, amber: paused, red: actions off) | Open the dashboard, ask, pause/resume, kill switch, restart or stop capture. Reminders appear as notifications from it |
| **Overlay**, Ctrl+Alt+Shift+Space | Ask a question, "do …", or "remember that …", from anywhere. Esc closes it |
| **Dashboard** (`127.0.0.1:47821`) | Today, Timeline, Projects (with a current-state note), Ask, Inbox, Actions, Memory, Privacy |
| **Your phone** (off until you turn it on) | Today, Timeline, Projects and Ask; pause/resume and forget |

## 1. Tests

```powershell
uv run pytest -q tests/test_web.py tests/test_phase6.py   # dashboard, security, phone, memory, reminders, rollups
```

## 2. Start it

```powershell
uv run personal-os daemon stop; uv run personal-os daemon start
uv run personal-os doctor          # shows the tray, the hotkeys, and if another app took one
```

Then click the tray icon (**Open dashboard**), or run `uv run personal-os dashboard`. Each link signs you in once.

The first time, the Today page shows a welcome note. Go to **Privacy → Sources** and turn off anything you don't want captured. `uv run personal-os setup` does the same in the terminal.

## 3. Things to try

- **Today:** pause for 30 minutes; forget the last 15 minutes; ask from the box.
- **Timeline:** step through days. Each session shows its summary, and **Evidence** expands to the events behind it.
- **Projects:** click a project for its current-state note (written in the background once sessions are summarised), recent sessions, export, and delete.
- **Ask:** "where did I leave off on personal-os?", then tick *follow-up* and ask "and yesterday?". Typing "remember that the staging db is on port 5433" asks before storing it.
- **Inbox:** pick a project for a group and click Assign. The choice is learned.
- **Actions:**
  - "remind me in 2 minutes to stretch" (a notification arrives from the tray);
  - "take a screenshot";
  - something that needs confirming (a named command, a file edit), which shows Approve / Decline;
  - the kill switch; the log, with Undo.
- **Memory:** edit or forget what it remembers.
- **Privacy:** sources on/off (then **Restart capture**), retention, blocklists, what's stored, export everything.

## 4. Overlay

Press **Ctrl+Alt+Shift+Space** anywhere. The chat model starts loading straight away, so the first answer is quicker. Then:

- type a question and press Enter;
- or start with "open …", "remind me …", "take …";
- anything that needs confirming asks with Yes/No.

## 5. Phone

1. **Privacy → Phone → Turn on**, then **Restart capture**.
2. Once, in an **administrator** PowerShell, add the firewall rule the page shows (it allows only your private network).
3. On the phone, on the same Wi-Fi, open the address shown (`https://<laptop-ip>:47822`). Accept the certificate warning: it's this laptop's own certificate.
4. On the laptop, click **Show pairing code** and type it on the phone.

Revoke a phone any time under **Privacy → Paired phones**.

## Other new commands

```powershell
uv run personal-os export C:\Users\you\Desktop\pos-export --format markdown
uv run personal-os projects delete old-project
uv run personal-os memory list / add "…" / edit ID "…" / forget ID
uv run personal-os reminders list / cancel ID
uv run personal-os llm warm --for 30       # load the model before a run of questions
uv run personal-os llm unload
```

## Acceptance (brief §10)

> Everything controllable and inspectable without the terminal.
