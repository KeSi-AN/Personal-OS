# Testing Personal OS: commands to try

Run everything from `E:\Personal-OS` in PowerShell. Every command starts with `uv run personal-os`.

## Already set up

| Piece | State |
|---|---|
| Python deps (`uv sync`) | installed; 397 tests pass |
| Daemon | running (restarted on 4 Nov with the latest code) |
| Watched folders | `~/Documents` and all of `E:/` (also in `actions.allowed_paths`, so actions can open anything there) |
| Apps | `vscode` points at `E:\Microsoft VS Code\Code.exe` |
| PowerShell terminal hook | installed |
| Browser native host | registered for Chrome, Brave, Edge; extension is sending visits |
| Ollama | `qwen3:4b` (chat) and `qwen3-embedding:0.6b` (search) |
| Voice speech model | `small.en` downloaded (voice itself is off until you turn it on) |

**Needs you:** Google sign-in (see docs/GOOGLE.md), the "hey Donna" wake-word model (see docs/VOICE.md), and plugins (off by default).

---

## 0. Health check (start here)

```powershell
uv run personal-os doctor          # daemon, observers, hooks, browser, ollama: look for "problems:"
uv run personal-os status
uv run personal-os llm status      # both models "installed"
```

## 1. Capture (phases 1–2)

```powershell
uv run personal-os events --last 20                    # recent activity with its project
uv run personal-os events --source browser --last 20
uv run personal-os events --source terminal --last 20  # run a few commands in PowerShell first
uv run personal-os events --source git                 # make a commit in this repo first
uv run personal-os search "something you read today"
uv run personal-os inspect                             # what's stored, by table and privacy class
```

Privacy controls:

```powershell
uv run personal-os pause --for 10m     # or Ctrl+Alt+Shift+P anywhere
uv run personal-os resume
uv run personal-os forget 15m          # deletes the last 15 minutes everywhere (irreversible)
```

## 2. Projects, inbox, sessions (phase 3)

```powershell
uv run personal-os projects            # projects with time in the last 7 days
uv run personal-os sessions            # today's work sessions
uv run personal-os inbox               # unassigned items, grouped
uv run personal-os assign 5 personal-os      # inbox line 5 (github.com) is this project
uv run personal-os assign 1 none             # line 1 (YouTube) isn't project work
uv run personal-os resolve
```

## 3. Ask questions (phase 4, local model)

```powershell
uv run personal-os llm warm --for 30   # optional: skip the ~10 s cold start
uv run personal-os llm embed           # index what's waiting now instead of when idle
uv run personal-os summarize           # summarise today's finished sessions now
uv run personal-os ask "what did I do today?"
uv run personal-os ask "where did I leave off on personal-os?"
uv run personal-os ask "what was I researching on AWS?"
uv run personal-os ask "which commands failed today?"
uv run personal-os ask "where did I leave off on the moon-base project?"   # should say no data
```

## 4. Actions (phase 5)

Try a dry run first: it shows the plan and runs nothing.

```powershell
uv run personal-os actions list
uv run personal-os do "pause the music and take a screenshot" --dry-run
uv run personal-os do "take a screenshot"
uv run personal-os do "note down: try the phone dashboard"
uv run personal-os do "remind me to stretch in 2 minutes"     # Windows notification in 2 min
uv run personal-os do "set brightness to 70"
uv run personal-os do "open E:/Personal-OS in vscode"
uv run personal-os actions log                                # everything that ran
uv run personal-os actions undo <id from the log>             # e.g. remove the note
uv run personal-os actions run delete_path --arg path=~/Documents/x.txt   # tier 3: BLOCKED
```

Kill switch: press **Ctrl+Alt+Shift+K** (or `actions kill`), then `actions resume`.

## 5. Dashboard, tray, overlay, phone (phase 6)

```powershell
uv run personal-os dashboard           # opens the dashboard in your browser, signed in
```

- **Ctrl+Alt+Shift+Space**: quick-ask overlay from anywhere
- Tray icon: open dashboard, pause, and other quick actions
- Dashboard → **Privacy**: sources, export, delete. **Memory**: remembered facts
- Phone: turn on LAN access and pair from the dashboard (`config set api.lan.enabled true`, then restart the daemon)

```powershell
uv run personal-os memory add "my main project is personal-os"
uv run personal-os memory list
uv run personal-os export $env:USERPROFILE\Desktop\pos-export --format markdown
uv run personal-os setup               # first-run wizard in the terminal
```

## 6. Voice, briefings, MCP, plugins (phase 7)

```powershell
uv run personal-os voice test          # says something in the Zira voice
uv run personal-os voice on            # starts within 30 s; hold Ctrl+Alt+Shift+V and speak
uv run personal-os voice status
uv run personal-os voice log           # what it heard and how long each step took
uv run personal-os voice off

uv run personal-os briefing            # morning briefing
uv run personal-os briefing --evening  # end-of-day summary
uv run personal-os briefing --speak

uv run personal-os mcp --config        # entry to paste into Claude Desktop's config

uv run personal-os config set actions.plugins_enabled true
uv run personal-os plugins example     # writes plugins\word_count.py
uv run personal-os plugins list
uv run personal-os do "count the words in: the quick brown fox"
```

Google (after the one-time setup in docs/GOOGLE.md):

```powershell
uv run personal-os google connect "C:\path\to\client_secret_....json"
uv run personal-os google status
uv run personal-os do "what's on my calendar today?"
```

## Safe playground (fake data, no effect on your real database)

```powershell
$env:PERSONAL_OS_HOME = "$env:TEMP\pos-sim"
uv run personal-os sim run tests/fixtures/scenarios/workday.toml --fresh
uv run personal-os search "refresh token" --db "$env:PERSONAL_OS_HOME\sim.db"
Remove-Item Env:PERSONAL_OS_HOME
```

## Dev checks

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy personal_os
```

## Daemon control

```powershell
uv run personal-os daemon stop; uv run personal-os daemon start   # after any config change
uv run personal-os autostart enable                               # start at login (currently off)
```
