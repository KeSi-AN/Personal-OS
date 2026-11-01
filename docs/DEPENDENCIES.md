# Dependencies

Every dependency, with a one-line reason. Anything not listed here doesn't get added. Status is **proposed** until the design is approved; versions are pinned in `uv.lock` when each one is first used.

## Runtime

| Package | Phase | Why |
|---|---|---|
| `pydantic` | 0 | Typed, validated event envelope, payloads, config, and LLM JSON outputs. One schema library for everything. |
| `typer` | 0 | CLI with subcommands and help text from type hints. Small and well maintained. |
| `tomlkit` | 0 | Writes `config.toml` without losing your comments (stdlib `tomllib` only reads). |
| `platformdirs` | 0 | Correct per-OS data, config, and log dirs, so the platform layer doesn't hard-code paths. |
| `watchdog` | 1 | Mature cross-platform filesystem events (ReadDirectoryChangesW, inotify, FSEvents). |
| `psutil` | 1 | Process exe, cwd, and parent lookup; daemon CPU/RSS for `doctor` and the budget script; battery/AC state. |
| `pathspec` | 1 | Exact `.gitignore` semantics. Hand-rolled gitignore matching is a known source of bugs. |
| `fastapi` | 1 | Local ingest, control, and query API, plus the phase 6 dashboard. Validation through pydantic. |
| `uvicorn` | 1 | ASGI server for FastAPI, run inside the daemon's own event loop. |
| `httpx` | 1 | Async HTTP client for the native host → daemon relay and Ollama calls (no need for the `ollama` SDK). |
| `sqlite-vec` | 4 | Vector search inside the same SQLite file. No separate vector database. |
| `keyring` | 4+ | DPAPI / OS keychain storage for an optional encryption key (only if encryption is enabled). |
| `pystray` + `Pillow` | 6 | System tray icon (Pillow is required by pystray for the icon image). |

## Development and test only

| Package | Why |
|---|---|
| `pytest` | Test runner. |
| `pytest-asyncio` | Testing asyncio observers, the bus, and the pipeline. |
| `hypothesis` | Property tests for redaction (no planted secret survives) and debounce logic. |
| `ruff` | Lint + format in one fast tool. |
| `mypy` | Static checking of the event and payload types, the core contract of the system. |

## Bundled into extensions (JavaScript, not Python)

| Package | Where | Why |
|---|---|---|
| `@mozilla/readability` | browser extension | Battle-tested main-content extraction (Firefox Reader View), runs on the page so no HTML leaves the tab. |
| `esbuild` (dev) | both extensions | Single-binary bundler for TypeScript. No webpack config. |
| `@types/vscode`, `@types/chrome` (dev) | extensions | Type definitions only. |

## Vendored

| Item | License | Why |
|---|---|---|
| `bash-preexec` | MIT | Standard `preexec`/`precmd` for Bash (used by Git Bash hooks). |

## Explicitly not used

`pywin32` (ctypes is enough, ADR-006), `GitPython` (git CLI, ADR-005), SQLAlchemy/Alembic (plain SQL, ADR-009), LangChain and similar (thin `LLMProvider` interface instead).
