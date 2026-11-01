# Personal OS

A local-first personal context and assistant layer. It observes your computer activity (under your control), remembers it as structured project context, answers questions about your own work, and acts through a permissioned tool registry. Everything runs on your machine.

**Status:** Phase 1 built: live capture of the active window, idle/lock/sleep, files under Documents, git repos and terminal commands, in a background daemon with `doctor`, pause and forget. Awaiting the 1-hour acceptance run.

- Design: [docs/DESIGN.md](docs/DESIGN.md)
- Decisions: [docs/DECISIONS.md](docs/DECISIONS.md)
- Dependencies: [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md)
- Demos: [phase 0 (simulator)](docs/PHASE0_DEMO.md), [phase 1 (live capture)](docs/PHASE1_DEMO.md)
- Resource budget: [docs/BUDGET.md](docs/BUDGET.md)

## Development

```powershell
uv sync                 # create .venv with Python 3.12 and install deps
uv run pytest -q        # tests
uv run ruff check .     # lint
uv run mypy personal_os # strict type check
uv run personal-os --help
```

Set `PERSONAL_OS_HOME` to use a scratch data directory instead of `%LOCALAPPDATA%\PersonalOS`.
