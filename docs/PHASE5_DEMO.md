# Phase 5 demo: actions

Phase 5 lets the assistant do things through a fixed set of typed tools. You can call a tool directly (`personal-os actions run …`) or ask in words (`personal-os do "…"`), and the local model plans the calls. Each tool has a risk tier:

| Tier | What runs | Examples |
|---|---|---|
| 0 | Runs | `search_context`, `read_file`, `list_folder`, `system_status` |
| 1 | Runs when you asked for it directly; asks otherwise | `open_path`, `open_url`, `open_app`, `create_note`, `copy_to_clipboard`, `media`, `screenshot`, `window` |
| 2 | Always asks, showing exactly what will happen (a diff, the command line) | `create_file`, `edit_file`, `move_path`, `run_command`, `git_commit`, `git_switch`, `git_stash` |
| 3 | Blocked | `delete_path`, `run_shell` |

Files can only be touched inside your watched folders (and the notes and screenshots folders). Secret files are never touched. Every step is in an audit log that can't be edited.

## 1. Tests

```powershell
uv run pytest -q tests/test_actions.py        # policy, rails, undo, audit, kill switch, injection
uv run pytest -q -m llm tests/test_actions.py # the planner and an injection attempt with qwen3:4b
```

## 2. Look before anything runs

```powershell
uv run personal-os actions list
uv run personal-os do "open my Personal-OS folder in vscode" --dry-run
uv run personal-os do "pause the music and take a screenshot" --dry-run
```

## 3. Run things

```powershell
uv run personal-os do "take a screenshot"
uv run personal-os do "note down: compare httpx timeouts tomorrow"
uv run personal-os actions run open_path --arg path=~/Documents/Personal-OS
uv run personal-os actions log
uv run personal-os actions undo <id from the log>      # removes the note, if unchanged
```

To try tier 2, add a named command to `%LOCALAPPDATA%\PersonalOS\config.toml`:

```toml
[actions.commands.tests]
argv = ["uv", "run", "pytest", "-q"]
cwd = "~/Documents/Personal-OS"
```

```powershell
uv run personal-os do "run my tests command"     # shows the command, waits for y
```

## 4. Blocked and unlocked

```powershell
uv run personal-os actions run delete_path --arg path=~/Documents/scratch.txt   # BLOCKED
uv run personal-os actions unlock delete_path --minutes 5                         # asks you first
uv run personal-os actions run delete_path --arg path=~/Documents/scratch.txt   # asks; Recycle Bin
uv run personal-os actions lock
```

## 5. Kill switch

Start a long named command, then press **Ctrl+Alt+Shift+K**, or run `personal-os actions kill` in another terminal:

- the command is stopped;
- nothing new runs;
- `actions log` shows it as cancelled.

`personal-os actions resume` turns actions back on.

## Acceptance (brief §10)

> Tier 1–2 tools work; blocked actions are blocked; injected page text cannot trigger a tool.
