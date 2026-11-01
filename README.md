# Personal OS

A local-first personal context and assistant layer. It observes your computer activity (under your control), remembers it as structured project context, answers questions about your own work, and acts through a permissioned tool registry. Everything runs on your machine.

**Status:** Phase 0 complete (event schema, bus, privacy filter, storage, simulator, CLI skeleton). No live capture yet.

- Design: [docs/DESIGN.md](docs/DESIGN.md)
- Decisions: [docs/DECISIONS.md](docs/DECISIONS.md)
- Dependencies: [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md)
- Phase 0 demo: [docs/PHASE0_DEMO.md](docs/PHASE0_DEMO.md)

## Development

```powershell
uv sync                 # create .venv with Python 3.12 and install deps
uv run pytest -q        # tests
uv run ruff check .     # lint
uv run mypy personal_os # strict type check
uv run personal-os --help
```

Set `PERSONAL_OS_HOME` to use a scratch data directory instead of `%LOCALAPPDATA%\PersonalOS`.
