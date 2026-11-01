# Phase 1 demo: live capture on your machine

Phase 1 adds the real observers: active window and idle/lock/sleep, files under `Documents`, your git repos, and terminal commands. They run in a background daemon. Everything stays in `%LOCALAPPDATA%\PersonalOS`, and nothing leaves the machine.

Run the commands from the repo root in **PowerShell** (`uv run personal-os …`).

## 1. Tests

```powershell
uv run pytest -q             # 187 tests, no live OS access
uv run pytest -q -m live     # 3 tests that drive real PowerShell, Git Bash and the file watcher
```

## 2. Turn on file capture (optional)

File capture is off by default. To watch `Documents`:

```powershell
uv run personal-os config set observers.fs.enabled true
```

## 3. Install the terminal hook

```powershell
uv run personal-os install-hooks --dry-run      # see exactly what would change
uv run personal-os install-hooks                 # PowerShell (add --shell bash for Git Bash)
```

This adds a marked block to `Documents\WindowsPowerShell\profile.ps1` and backs up the original first. If PowerShell's execution policy blocks profiles, the command tells you so and prints the one-line fix for you to run. Open a **new** terminal afterwards.

Commands typed with a leading space are never recorded.

## 4. Start the daemon

```powershell
uv run personal-os daemon start
uv run personal-os doctor
```

`doctor` shows each observer's state, when it last captured something, events per hour, the daemon's CPU and memory, hook status, and any problems (it exits nonzero if something is wrong).

## 5. Use the machine normally, then look

```powershell
uv run personal-os events --last 40
uv run personal-os events --source git
uv run personal-os search "something you worked on"
uv run personal-os inspect
```

Things to check:

- Window focus intervals look right (no rows for quick Alt-Tabs; `transient_switches` counts them).
- Idle time and screen lock (Win+L) show as `idle.idle`.
- A commit in any repo under Documents shows as `git.commit`.
- Terminal commands show with duration and exit code; secrets are `[redacted]`.
- WhatsApp and Instagram never appear (the app is blocked, browser titles are `[redacted]`).

## 6. Privacy controls

```powershell
uv run personal-os pause --for 10m    # or press Ctrl+Alt+Shift+P anywhere (you'll hear a beep)
uv run personal-os resume
uv run personal-os forget 15m         # deletes the last 15 minutes everywhere
```

## 7. Acceptance: the 1-hour session

Work normally for an hour with the daemon running, then:

```powershell
uv run python scripts/measure_budget.py --minutes 60 --out docs/BUDGET.md
```

The budget is under 2% CPU and under 300 MB of memory. Then review `events` for accuracy.

## 8. Start at logon (optional)

```powershell
uv run personal-os autostart enable     # adds PersonalOS.vbs to your Startup folder
uv run personal-os autostart disable
```

## Stop or undo

```powershell
uv run personal-os daemon stop
uv run personal-os uninstall-hooks
uv run personal-os autostart disable
```
