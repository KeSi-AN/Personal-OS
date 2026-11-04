# Dependencies

Every dependency, with a one-line reason. Anything not listed here doesn't get added. Exact versions are pinned in `uv.lock`. Phases 0–1 use only the phase 0 and 1 rows plus the dev tools (the local API moved to phase 2, ADR-025); the rest are added in the phase that first needs them.

Transitive packages pulled in by the ones below (for example `rich`, `click` and `shellingham` via `typer`, and `pydantic-core` via `pydantic`) aren't listed separately.

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
| `fastapi` | 4+ | Local ingest, control, and query API, plus the phase 6 dashboard. Validation through pydantic. |
| `uvicorn` | 4+ | ASGI server for FastAPI, run inside the daemon's own event loop. |
| ~~`httpx`~~ | - | Not needed after all: Ollama is called with the standard library's `urllib` (ADR-043), and the native host doesn't need it either (ADR-029). |
| ~~`sqlite-vec`~~ | - | Not used: vectors are plain rows searched by brute force, so `forget` can guarantee they're overwritten (ADR-044). |
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
| `types-psutil` | Type stubs so mypy can check the psutil calls (process info, CPU/memory). |
| `hatchling` (build backend) | Builds the package so `uv` can install the `personal-os` command. No configuration beyond one line. |

## Bundled into extensions (JavaScript, not Python)

| Package | Where | Why |
|---|---|---|
| `@mozilla/readability` | browser extension | Battle-tested main-content extraction (Firefox Reader View), runs on the page so no HTML leaves the tab. |
| `esbuild` (dev) | both extensions | Single-binary bundler for TypeScript. No webpack config. |
| `typescript` (dev) | both extensions | Type-checking only (`tsc --noEmit`); esbuild does the compiling. |
| `@types/chrome`, `@types/node`, `@types/vscode` (dev) | extensions | Type definitions only. |

## Vendored

Nothing. The bash hook is about 60 lines of our own (a `DEBUG` trap plus `PROMPT_COMMAND`) instead of vendoring `bash-preexec`, which is much larger than this needs.

## Explicitly not used

`pywin32` (ctypes is enough, ADR-006), `GitPython` (git CLI, ADR-005), SQLAlchemy/Alembic (plain SQL, ADR-009), LangChain and similar (thin `LLMProvider` interface instead).
