# Personal OS

A local-first personal context and assistant layer. It observes your computer activity (under your control), remembers it as structured project context, answers questions about your own work, and acts through a permissioned tool registry. Everything runs on your machine.

**Status:** Phase 1 built: live capture of the active window, idle/lock/sleep, files under Documents, git repos and terminal commands, in a background daemon with `doctor`, pause and forget. Accepted 2026-11-03 ([acceptance notes](docs/PHASE1_ACCEPTANCE.md)). Phase 2 (browser extension) accepted 2026-11-03 ([notes](docs/PHASE2_ACCEPTANCE.md)). Phase 3 (projects, inbox, sessions) built and awaiting review ([notes](docs/PHASE3_ACCEPTANCE.md)). Phase 4 (local model: session summaries, hybrid search, `personal-os ask` with cited evidence) built and awaiting review ([notes](docs/PHASE4_ACCEPTANCE.md)). Phase 5 (actions: typed tools with risk tiers, confirmation, audit log, undo, kill switch, `personal-os do`) accepted 2026-11-04 ([notes](docs/PHASE5_ACCEPTANCE.md)).

- Design: [docs/DESIGN.md](docs/DESIGN.md)
- Decisions: [docs/DECISIONS.md](docs/DECISIONS.md)
- Dependencies: [docs/DEPENDENCIES.md](docs/DEPENDENCIES.md)
- Demos: [phase 0 (simulator)](docs/PHASE0_DEMO.md), [phase 1 (live capture)](docs/PHASE1_DEMO.md), [phase 2 (browser)](docs/PHASE2_DEMO.md), [phase 3 (projects)](docs/PHASE3_DEMO.md), [phase 4 (ask)](docs/PHASE4_DEMO.md), [phase 5 (actions)](docs/PHASE5_DEMO.md)
- Resource budget: [docs/BUDGET.md](docs/BUDGET.md)
- Roadmap and post-MVP backlog: [docs/ROADMAP.md](docs/ROADMAP.md)

## Development

```powershell
uv sync                 # create .venv with Python 3.12 and install deps
uv run pytest -q        # tests
uv run ruff check .     # lint
uv run mypy personal_os # strict type check
uv run personal-os --help
```

Set `PERSONAL_OS_HOME` to use a scratch data directory instead of `%LOCALAPPDATA%\PersonalOS`.
