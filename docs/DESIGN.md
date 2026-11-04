# Personal OS — Design

**Status:** v0.7. Phases 0, 1 and 2 accepted (2026-11-03). Phases 3 (project resolver, sessions), 4 (local model: summaries, hybrid search, `ask`) and 5 (action layer) built, awaiting review. Phase 6 (tray, overlay, dashboard, privacy centre) next.
**Date:** 2026-11-02
**Scope of this draft:** overall architecture, and the observation layer in detail (brief §4). Storage, the context engine, the action layer, and interfaces are covered only far enough to show the observation layer fits them. Each gets its own design section before its phase starts.

---

## 0. Summary

Personal OS is one Python daemon running in your Windows login session. Observers watch windows, files, git, the terminal, the browser, and the editor, and emit typed `Event`s onto a bounded in-process bus. Every event passes a **fail-closed privacy filter** before a batched writer stores it in SQLite (WAL, FTS5, sqlite-vec). Background jobs then turn events into sessions, project state, and summaries using a local Ollama model. The daemon serves a token-protected API on `127.0.0.1` that the CLI, editor extension, browser native-messaging host, and later the dashboard all use.

Where this draft departs from the brief, and why:

| Brief suggests | This design | Why |
|---|---|---|
| Shell hooks send to a socket/pipe, spool if the daemon is down | Shell hooks **always** append to a per-shell spool file; the daemon tails it | Prompt latency is the top risk for a shell hook. Appending a line to a file costs about 1 ms in PowerShell 5.1 and can't hang, while a pipe connect can. One code path instead of two. |
| `GitPython` or the git CLI | git CLI only, with `--no-optional-locks` and porcelain v2 output | GitPython launches git processes anyway and adds a dependency. The CLI's porcelain output is a stable contract, and `--no-optional-locks` means we never fight your own git commands for `index.lock`. |
| `pywin32` | `ctypes` against user32/kernel32/wtsapi32 | It's about 10 functions. Skips a heavy dependency with a post-install step. |
| Native messaging or an authenticated WebSocket | Native messaging for the browser; token-authenticated HTTP on `127.0.0.1` for everything else | Native messaging pins the extension ID and opens no port to web pages. The editor extension and CLI need a normal API anyway. |
| Observers as plugins that can crash independently | In-process asyncio tasks under a supervisor, with optional per-observer process isolation later | One process stays well inside the 300 MB budget. A Python exception in one observer can't affect the others. Only a native crash (bad `ctypes` call) could kill the daemon, and the launcher restarts it (ADR-024). |
| A daemon "always on" | Starts at login from your **Startup folder** in your user session, not as a Windows service, under a small launcher that restarts it if it crashes (ADR-024) | Services run in session 0 and can't see your desktop windows, idle state, or lock events. A Task Scheduler logon trigger would need admin rights. |

---

## 1. Target environment (inspected on 2026-11-01)

| Item | Found | Design consequence |
|---|---|---|
| OS | Windows 11 Home 10.0.26200 | Windows-first. Linux and macOS go behind a platform layer with stubs and documented limits. Home edition has no BitLocker management or Group Policy, so encryption at rest is our job (§5.3). |
| CPU / RAM | i5-10300H (4C/8T, laptop), 15.8 GB | Laptop: background LLM jobs run only **on AC power and while you're idle**. 16 GB rules out keeping a 20B model resident next to normal work. |
| GPU | GTX 1650, **4 GB VRAM** (+ Intel UHD) | Only about 4B-parameter quantized models fit fully on the GPU (§6.2). |
| Disk | C: 94 GB free, D: 45 GB free | Data dir defaults to `%LOCALAPPDATA%\PersonalOS` on C:. Expected growth is under 1 GB/month. |
| Python | 3.13.5 (python.org), 3.11 (Store + uv), `uv` installed | Project pinned to **CPython 3.12 via uv** for the widest wheel coverage (voice/ML packages in phase 7). Phases 0–6 also run on 3.13. |
| SQLite | 3.49.1, FTS5 works, `enable_load_extension` available | `sqlite-vec` can load. No custom SQLite build needed. |
| Ollama | 0.32.6, models: `qwen3:4b`, `gemma4:e2b`, `gpt-oss:20b` | **Qwen only** (your answer): `qwen3:4b` for everything, plus `qwen3-embedding:0.6b` for vectors. The other installed models are ignored. |
| Shells | Windows PowerShell 5.1 (no PowerShell 7), Git Bash, Windows Terminal | **PowerShell 5.1 hook first** (most likely in Windows Terminal), Git Bash second. `cmd.exe` has no hook point and isn't supported. |
| Browsers | **Brave (default)**, Chrome, Edge, Firefox | One Manifest V3 extension covers Brave, Chrome, and Edge. **Development and testing happen in Chrome first**, then Brave is verified. Firefox later. |
| Editors | VS Code (54 extensions), Cursor | **VS Code** is the target; the same extension should install in Cursor too. |
| Repos | 24 git repos under `Documents` (within 3 levels) | **`Documents` is the only watched root** for now. |
| Shell profiles | No PowerShell profile or `.bashrc` yet | The hook installer creates them and marks its block clearly. |

---

## 2. Goals and non-goals for v1

**Goals:** accurate, low-overhead capture on this Windows laptop; data you can inspect and delete; grounded answers about your own work; safe tool use.

**Non-goals for v1:** screenshots or OCR of the screen (privacy and resource cost); audio capture; keystroke logging (never); multi-user or multi-machine sync; Linux and macOS observers (interfaces only); cloud LLMs (possible later, per feature, opt-in).

---

## 3. Architecture

### 3.1 Process model

```
 Windows login session (user: addya)
 ┌──────────────────────────────────────────────────────────────────────────┐
 │  personal-os daemon  (pythonw, under the launcher; Startup folder entry) │
 │                                                                          │
 │   asyncio event loop (main thread)                                       │
 │    ├─ Supervisor ── observer tasks: window, idle/session, fs, git,       │
 │    │                 terminal-spool, (clipboard, …)                      │
 │    ├─ EventBus (bounded queue)                                           │
 │    ├─ Pipeline: PrivacyFilter → ProjectHint → BatchWriter (SQLite)       │
 │    ├─ Local API (phase 2: uvicorn, 127.0.0.1, token) ── /ingest /query   │
 │    └─ Job scheduler: retention, sessionizer, summarizer (LLM, gated)     │
 │                                                                          │
 │   worker threads: Win32 message-only window (lock/unlock, sleep/wake,    │
 │                   hotkeys), watchdog FS threads, git subprocesses        │
 └──────────────────────────────────────────────────────────────────────────┘
        ▲ spool files            ▲ HTTP+token             ▲ HTTP+token
        │                        │                        │
  PowerShell / Bash hooks   native-messaging host    VS Code / Cursor ext.
                            (spawned by browser)        CLI, tray, dashboard
                                 ▲ stdio
                            MV3 extension (Brave/Chrome/Edge)

  Ollama (separate process, http://127.0.0.1:11434) ◀── LLM jobs, embeddings
```

- **One process, one event loop.** Blocking OS calls run in threads (`asyncio.to_thread` or dedicated threads) and publish back with `loop.call_soon_threadsafe`.
- **One writer.** Only the BatchWriter writes to SQLite. Readers (API, jobs) use separate read-only connections, which WAL mode allows to run alongside it.
- **The LLM is never on the capture path.** If Ollama is down, capture and storage carry on, and LLM jobs queue with backoff.

### 3.2 Package layout

```
personal_os/
  core/        clock, ids (UUIDv7), event envelope, bus, supervisor, plugin registry, logging
  platform/    probes per OS: windows/ (ctypes), linux/ (stub+notes), macos/ (stub+notes)
  observers/   window.py, session.py (idle/lock/sleep), fs.py, git.py, terminal.py,
               browser_ingest.py, editor_ingest.py, clipboard.py (later)
  privacy/     filter.py, rules.py (blocklists), redact.py (regex+entropy), secrets_paths.py
  storage/     db.py, migrations/NNNN_*.sql, writer.py, repositories (events, pages, …)
  projects/    resolver (phase 3)
  context/     sessionizer, summarizer, retrieval (phases 3–4)
  llm/         LLMProvider interface, ollama provider (phase 4)
  actions/     tool registry, tiers, audit (phase 5)
  interfaces/  cli.py (typer), api.py (FastAPI), tray/overlay/dashboard (phase 6)
  sim/         scenario DSL, synthetic generator, recorder/replayer
  config/      schema (pydantic), defaults.toml, loader/writer (tomlkit)
extensions/browser/     MV3 extension (TypeScript, bundled with esbuild)
extensions/vscode/      VS Code/Cursor extension (TypeScript)
native_host/            native messaging host (Python) + manifests + registry installer
personal_os/hooks/      powershell.ps1, bash.sh, installer (marked profile block)
tests/  docs/  scripts/
```

### 3.3 Ingress channels

| Producer | Channel | Auth | Notes |
|---|---|---|---|
| In-process observers | `EventBus.publish()` | n/a | Non-blocking. |
| Shell hooks | Append JSONL to `spool/terminal/<shell>-<pid>.jsonl` | Filesystem ACL (`%LOCALAPPDATA%` is user-private) | Daemon tails, ingests, and deletes the file. Survives daemon downtime. |
| Browser extension | Native messaging (stdio) → host → `POST /v1/ingest` | Host manifest pins the extension ID; host reads the token file | No port is exposed to web pages. |
| VS Code/Cursor ext. | `POST /v1/ingest` | Bearer token from `runtime/token` | |
| CLI / tray / dashboard | Phase 1: the database (pause setting, `meta.forget` markers) plus a stop-request file. From phase 2: `/v1/query/*` | Filesystem ACL; later bearer token | The daemon polls once a second (ADR-025). |

**API hardening:** binds `127.0.0.1` only. Rejects requests whose `Host` isn't `127.0.0.1:<port>` or `localhost:<port>` (blocks DNS rebinding). Rejects any request carrying a web `Origin` unless it's the dashboard's own origin (blocks pages calling the API from the browser). Constant-time token comparison. Token rotates on each daemon start. Port and token go in `runtime/runtime.json`.

### 3.4 Time and identity

- All timestamps are timezone-aware UTC (`ts_start`, `ts_end`), plus a `tz_offset_min` column so "yesterday" means your local yesterday.
- An injected `Clock` (real or simulated) is used everywhere. Nothing calls `time.time()` directly, so the simulator can run a day in seconds.
- IDs are **UUIDv7**: time-sortable, generated locally, about 15 lines of our own code (no dependency).

### 3.5 Data directory and config

`%LOCALAPPDATA%\PersonalOS\` (configurable): `personal_os.db` (+ `-wal`, `-shm`), `config.toml`, `runtime\` (token, port, pid), `spool\`, `logs\` (rotating, **no event content in logs**, only ids, sources, and counts), `exports\`. It's kept outside `Documents` and OneDrive so the system never observes or syncs itself. The data dir is also on the filesystem observer's hard ignore list.

### 3.6 Failure and degradation matrix

| Failure | Effect | Handling |
|---|---|---|
| Observer raises | That source pauses | Supervisor restarts it with exponential backoff (1 s → 5 min cap, jitter). After 5 failures in 10 min it's marked `degraded` and shown in `doctor`. |
| Daemon crashes or is killed | Capture stops | The launcher restarts it with backoff (1 s up to 60 s). Shell spool keeps terminal events. Browser extension buffers up to 1,000 events in `chrome.storage.local`. Open intervals were checkpointed (§4.1). |
| Bus full | Backpressure | Drop by priority (§4.2) and count drops. Never block the producer. |
| SQLite busy or locked | Writes delayed | Writer retries with backoff while the bus buffers. If the bus is full, drop rules apply. |
| Disk nearly full (< 2 GB free) | | Writer switches to essential-only (window, git, terminal) and `doctor` warns. |
| Ollama down | No summaries or embeddings | Jobs queue and retry. `ask` falls back to keyword search with a notice. |
| Privacy filter raises | | **Event dropped** (fail closed), counter incremented, error logged without content. |
| Sleep/hibernate | | Detected (§4.4). Open intervals are closed at the sleep time, not the wake time. |

---

## 4. Observation layer

### 4.0 Observer contract and supervisor

```python
class Observer(Protocol):
    name: ClassVar[str]  # "window", "fs", …
    version: ClassVar[str]  # semver; stamped into events as observer_version

    async def start(self, ctx: ObserverContext) -> None: ...  # runs until stop() or exception
    async def stop(self) -> None: ...  # must return within 5 s
    def health(self) -> ObserverHealth: ...  # cheap, non-blocking


class ObserverContext:  # handed in by the supervisor
    bus: EventBus
    clock: Clock
    config: ObserverConfig
    log: Logger
    paused: asyncio.Event  # observers check this and also stop emitting while paused


class ObserverHealth(BaseModel):
    state: Literal["starting", "running", "paused", "degraded", "crashed", "disabled"]
    last_event_at: datetime | None
    events_last_hour: int
    restarts: int
    last_error: str | None  # exception type + message, never event content
```

Observers are discovered through a registry (entry points later), enabled per `config.toml`, and each one runs in its own supervised task. Platform-specific code lives behind probes (`WindowProbe`, `IdleProbe`, `SessionProbe`). The Windows implementation is real; Linux and macOS raise `NotSupported` with a clear message.

### 4.1 Event envelope

```python
class Event(BaseModel):
    id: UUID  # UUIDv7
    ts_start: datetime  # UTC
    ts_end: datetime | None  # None = interval still open; point events: ts_end == ts_start
    source: Source  # window|idle|system|browser|fs|git|editor|terminal|clipboard|meta
    type: str  # e.g. "focus", "page_visit", "command"
    payload: Payload  # discriminated union keyed on (source, type), validated
    project_hint: str | None = (
        None  # cheap hint from the observer (repo root, cwd); resolver decides later
    )
    confidence: float = 1.0  # observer's confidence in the payload itself
    privacy_class: Literal["normal", "sensitive", "blocked"] = (
        "normal"  # set/raised by the filter, never lowered
    )
    observer_version: str
    tz_offset_min: int
    retention: RetentionClass  # raw|page_text|terminal_output|clipboard|summary
```

**Interval events are upserted by id.** An observer emits an interval with `ts_end=None` when it opens. Every 60 s it re-emits the same id, still with `ts_end=None`, and an updated `last_seen` (the checkpoint). It emits the event a final time with `ts_end` set when the interval closes. The writer does `INSERT … ON CONFLICT(id) DO UPDATE`, never moves `ts_end` backwards, and never reopens a closed interval. A crash loses at most 60 s of an open interval. On startup, any interval still open from a previous run is closed at its `last_seen` and flagged `truncated=true`.

### 4.2 Event bus, backpressure, batching

- `asyncio.Queue(maxsize=10_000)`. `publish()` never awaits. When the queue is full, drop in priority order: `fs` bursts, then `browser` intent signals, then everything else. Window, git, and terminal events are dropped last. Every drop is counted per source and shown in `doctor`.
- The BatchWriter takes up to **200 events or 1 s**, whichever comes first, and writes them in one transaction (`BEGIN IMMEDIATE`). Expected load is under 2 events/s on average, so batches are small and commits are rare.
- Pipeline order: `bus → PrivacyFilter → ProjectHint (cheap, rules-only) → BatchWriter`. Each stage is a plain function, so the simulator and the tests run the identical pipeline.

### 4.3 Privacy filter (single choke point, fails closed)

Every event passes it, including events from the extensions and from replay. Stages, in order:

1. **Pause gate.** While paused, drop everything except `meta` events (pause and resume markers).
2. **Hard blocks → `blocked`, dropped.** App executables (default: password managers such as `KeePassXC.exe` and `1Password.exe`, Windows credential and UAC prompts, **the WhatsApp desktop app**), domain categories (banking, health, DMs; **`whatsapp.com` and `instagram.com` are blocked entirely**, along with the other web messengers in the default list), incognito, and pages with password fields. Window titles matching a blocklist regex (default: `WhatsApp`, `Instagram`) don't drop the focus event; the title is replaced with `[redacted]`, so time spent stays visible but not what was on screen. **Blocked events are never stored.** Only a per-source counter is incremented, so `doctor` can say "42 events blocked today".
3. **Secret paths → `sensitive`.** `.env*`, `*.pem`, `*.key`, `*.pfx`, `id_rsa*`, `id_ed25519*`, `credentials*`, `*.kdbx`, `.aws/`, `.ssh/`, `.git-credentials`, `.npmrc`, `.pypirc`, `secrets.*`. Only path, kind, and time are kept, and the file is **never read**.
4. **Redaction** of every free-text field (titles, URLs, commands, commit messages, selections, extracts):
   - URLs: tracking parameters stripped (`utm_*`, `fbclid`, `gclid`, `mc_eid`, `ref_src`, …), `user:pass@` removed, query values whose key looks secret (`token`, `key`, `code`, `state`, `sig`, `password`, `auth`) replaced with `[redacted]`.
   - Pattern detectors: AWS (`AKIA…`, secret-key shape), GitHub (`ghp_`, `gho_`, `github_pat_`), GitLab, Slack (`xox[abpr]-`), Stripe (`sk_live_`), OpenAI/Anthropic-style `sk-…`, Google `AIza…`, JWTs, PEM blocks, `Authorization:`/`Bearer`/`Basic`, `--password[= ]`, `-p<pw>` (mysql), `PASSWORD=`/`*_TOKEN=`/`*_SECRET=` assignments, connection strings with credentials.
   - Entropy detector: tokens of 20 characters or more whose Shannon entropy exceeds a per-charset threshold (base64-like > 4.5 bits/char, hex > 3.0 with length ≥ 32), **except** known-benign shapes: 7–40 character git SHAs in git contexts, UUIDs, file paths.
   - Detectors are pluggable (`Redactor` protocol) and live in one registry with a shared golden test corpus.
5. **Classification.** Raise `privacy_class` to `sensitive` when a soft rule matches (for example a title containing "password", "bank", or "diagnosis"). Sensitive events are stored without free text, are excluded from LLM prompts by default, and have short retention.

**Fail closed:** the whole filter is wrapped in a `try` block. Any exception drops the event. A bug here can only lose data, never leak it. Property-based tests (hypothesis) check that no input containing a planted secret survives redaction, and that `privacy_class` never decreases.

### 4.4 Active application, window, idle, lock, sleep

| | |
|---|---|
| **Captured** | Foreground app (exe name, path, product name from version info), window title (normalized), window class, PID, focus interval start/end/duration, process cwd where readable, idle intervals, lock/unlock, sleep/wake, logon/logoff. |
| **Mechanism** | **Window:** 1 Hz poll of `GetForegroundWindow` → `GetWindowThreadProcessId` → `GetWindowTextW` via `ctypes`, plus `psutil.Process(pid)` (cached per PID) for exe and cwd. **UWP apps:** the foreground process is `ApplicationFrameHost.exe`, so we enumerate child windows to find the real app's PID. **Idle:** `GetLastInputInfo` vs `GetTickCount64` (handles the 49.7-day 32-bit wrap). **Lock/unlock, logon/logoff:** `WTSRegisterSessionNotification` on a message-only window in a dedicated thread (`WM_WTSSESSION_CHANGE`). **Sleep/wake:** `WM_POWERBROADCAST` on the same window, plus a backup heuristic: a gap of more than 30 s between 1 Hz ticks means the machine was suspended. |
| **Schema** | `window/focus` (interval): `{app, exe, exe_path, pid, title, title_raw_hash, window_class, cwd?, is_uwp, elevated_unreadable}`. `idle/idle` (interval): `{threshold_s, reason: "no_input"\|"locked"\|"asleep"}`. `system/{lock,unlock,sleep,wake,logon,logoff,daemon_start,daemon_stop}` (point). |
| **Sampling & debounce** | 1 Hz poll; a new interval starts only when `(hwnd, pid, normalized_title)` changes. Title normalization strips unread counters (`^\(\d+\)\s*`), "● unsaved" markers, and trailing app suffixes into separate fields. **Flicker rule:** focus under 2 s (Alt-Tab passing through) isn't its own interval; it's counted as `transient_switches` on the surrounding interval. **Title churn:** title changes in the same window must be stable for 3 s before they split the interval. **Idle:** no input for 5 min (configurable) closes the focus interval **back-dated to the last input time** and opens an `idle` interval. |
| **Failure modes** | Elevated (admin) windows: `OpenProcess` is denied, so exe and cwd come back null and `elevated_unreadable=true` (the title is still readable). Full-screen games: works, but polling a game is pointless, so `doctor` exposes a per-app "ignore" rule. Process exits between calls: the PID cache entry is invalidated and the field left null. A message-window thread crash is caught by the supervisor, and the poll-gap heuristic still detects sleep. |
| **Passive engagement** | Watching a video or reading a long page produces no input and would look idle. The browser extension reports `video_playing`, and while it's true the idle observer marks the interval `idle_but_media` rather than plain idle. |
| **NOT captured** | Keystrokes, mouse coordinates, window contents, screenshots, other monitors' non-foreground windows, titles matching the blocklist (stored as `[redacted]` with the app name kept, unless the app itself is blocked). |
| **OS differences** | **Linux X11:** `python-xlib` `_NET_ACTIVE_WINDOW` + `_NET_WM_PID`, and idle via XScreenSaver. **Linux Wayland:** no portable way to read the foreground window. This needs compositor-specific extensions (a GNOME Shell extension, KWin scripts, `swaymsg`), so we'd ship per-compositor adapters or none. **macOS:** `NSWorkspace.frontmostApplication`; titles need the Accessibility permission; idle via `CGEventSourceSecondsSinceLastEventType`. |

### 4.5 Browser activity (the most important source)

**Extension (Manifest V3, TypeScript, one build for Brave, Chrome, and Edge):**

| Component | Responsibility |
|---|---|
| Service worker | Tracks tab, window, and focus state; computes active dwell; builds visit chains; talks to the native host. **MV3 caveat:** the worker is killed after about 30 s idle, so all state lives in `chrome.storage.session`, timers use `chrome.alarms` (30 s minimum), and dwell is computed from stored timestamps, not running timers. |
| Content script (`document_idle`) | Readability extraction, scroll depth, password-field detection, selection and copy signals, `<video>` watched seconds. Sends results to the service worker on `visibilitychange`/`pagehide`. |
| Bundled `@mozilla/readability` | Main-text extraction on the page, so raw HTML never leaves the tab. |

**Captured per visit**

- `browser/page_visit` (interval = the visit): `{url, url_normalized, domain, title, tab_id, window_id, transition ("link"|"typed"|"search"|"bookmark"|"reload"|"form_submit"|"back_forward"|…), referrer, opener_visit_id, chain_id, active_dwell_ms, scroll_max_pct, video_watched_s, returned_count_7d, bookmarked, search_query?, content_type, lang, extract_id?}`
- `browser/page_extract` (stored in a separate table, 90-day retention): `{title, meta_description, headings[≤20], main_text[≤3000 chars], word_count, content_hash, site_enrichment{…}}`
- Intent signals: `browser/search {engine, query}`, `browser/selection {text≤500}`, `browser/copy {text≤500}` (selection captured at copy time; **the clipboard itself is never read**), `browser/bookmark_added`.

**Mechanisms**

- **Active dwell** accumulates only while (a) the tab is active, (b) its window is focused (`windows.onFocusChanged` → `WINDOW_ID_NONE` pauses it), (c) `chrome.idle` is `active` (detection interval 60 s, matching the desktop idle threshold within a minute), and (d) the page is visible.
- **Transition** comes from `webNavigation.onCommitted` (`transitionType` + `transitionQualifiers`). The referrer comes from the content script, the opener from `tabs.onCreated.openerTabId`, and the visit chain follows link/opener ancestry within the same tab or opened tabs.
- **Search queries** are parsed from SERP URLs: Google, Bing, DuckDuckGo, Brave Search, YouTube (`search_query`), GitHub search, Stack Overflow search.
- **Content type**: rules first (domain plus URL shape), then a cheap DOM heuristic. Categories: docs, Stack Overflow, GitHub issue/PR/repo/code, article, video, search results, chat, mail, other.
- **Enrichers run daemon-side in Python** (pluggable, easy to test with recorded fixtures): GitHub (`owner/repo`, issue or PR number, branch from URL), Stack Overflow (question id and title, whether an accepted answer is present, reported by the content script as a boolean), docs (library and version from known URL shapes, such as `docs.python.org/3.12/`, Read the Docs `/en/<ver>/`, MDN, `learn.microsoft.com`), YouTube (title and channel from the extract, watched seconds from the content script).

**Hard exclusions (enforced twice: in the extension, then again in the privacy filter)**

- Incognito/Private (and Brave Tor) windows: the extension is not enabled for incognito, **and** `tab.incognito` is checked on every event, which drops it if true.
- Pages with `input[type=password]` or payment fields (`autocomplete="cc-number"` etc.) present at any point: no extract, no selections, no visit row. Optional, off by default: a "time-only" mode that records `{domain, dwell}`.
- Default domain categories: banking and payments, health portals, webmail **bodies** (the visit to `mail.google.com` can be logged as "mail, 4 min" with no title or extract), DMs and chat web apps, plus your blocklist. **Allowlist mode** captures only the listed domains.
- `chrome://`, `brave://`, `edge://`, `about:`, `file://` (off by default), extension pages.
- **Dwell under 5 s** is dropped unless the page was bookmarked, reached by a search, or later revisited.

**Native messaging host:** `personal_os/browser/native_host.py`, run by a `.bat` launcher with the venv's Python in isolated mode (`-I`). stdio is binary, messages are length-prefixed JSON (capped at 1 MB). Each visit or signal is appended to `spool/browser/` and the daemon ingests it like the terminal spool (ADR-029); no port is opened. The host answers every message with the pause state so the extension stops sending while paused. Registration uses per-user registry keys for Chrome, Brave (its own key) and Edge, pointing at a manifest whose `allowed_origins` is the pinned extension ID (ADR-030). Built and tested in phase 2.

**Post-processing (phase 4, not on the capture path):** "why this mattered" summaries, keywords, and research-thread clustering run as background LLM jobs over visits with `active_dwell ≥ 30 s` or intent signals.

| | |
|---|---|
| **Failure modes** | Readability fails on single-page apps or canvas pages, so we fall back to title + meta + headings. Some sites block content scripts (the Chrome Web Store, PDF viewer), giving a visit with no extract. MV3 worker restarts lose nothing because state is in session storage. If the extension is uninstalled or disabled, `doctor` shows "browser: no events in 2 h while Brave was focused 40 min". |
| **NOT captured** | Raw HTML, form field values, cookies, full history import (opt-in later), anything in incognito, page text on sensitive or blocked domains, the clipboard. |
| **OS differences** | Extension code is identical. Native host registration differs: Windows uses the registry, Linux `~/.config/<browser>/NativeMessagingHosts/`, macOS `~/Library/Application Support/<browser>/NativeMessagingHosts/`. Firefox needs a `browser_specific_settings` manifest variant and its own host location. |

### 4.6 File activity

| | |
|---|---|
| **Captured** | Create, modify, delete, and move under watched roots: path, root, kind, size, extension, file type (code/text/doc/image/binary/archive), `repo_root`, and for text/code files a **diff summary** `{lines_added, lines_removed, symbols_added/removed/changed}`. In a repo the line counts are the uncommitted totals against HEAD after that save; outside git, the change since the last save seen (ADR-026). |
| **Mechanism** | `watchdog` (backed by `ReadDirectoryChangesW` on Windows), one recursive watch per root. Ignore rules are evaluated cheaply in the callback before anything reaches the bus. **Diff summary inside a git repo:** `git diff --numstat` for that path (vs index/HEAD), via the git observer's queue. **Outside git:** line-count and size delta only; we don't keep shadow copies of your files. **Symbols:** Python via `ast` (function and class names before and after, comparing only the name and signature sets); other languages via `tree-sitter` in a later phase. The previous symbol set is cached as **names only**, not content. |
| **Ignore rules** | Global: `node_modules`, `.venv`/`venv`, `__pycache__`, `.git/objects`, `.git/lfs`, `dist`, `build`, `target`, `.next`, `.cache`, `.gradle`, `.idea`, `*.tmp`, `~$*` (Office lock files), editor swap and backup files, the PersonalOS data dir. Per repo: `.gitignore` via `pathspec`, cached and reloaded when `.gitignore` changes. Per user: config globs. |
| **Debounce** | Per-path quiet window of 2 s (editors write temp file → rename → touch). Rename-and-replace saves collapse into a single `modified`. **Burst rule:** more than 50 events/s under one directory becomes one `fs/bulk_change {dir, count, kinds, sample_paths[≤5]}` (git checkout, npm install, build output). |
| **Schema** | `fs/change` (point): `{path, root, kind, dest_path?, size, ext, file_type, repo_root?, lines_added?, lines_removed?, symbols?, sensitive}`. `fs/bulk_change` as above. |
| **Secrets** | Secret-path patterns (§4.3 step 3) are never opened, read, or diffed. `sensitive=true` with path and kind only. |
| **Failure modes** | `ReadDirectoryChangesW` **buffer overflow drops events silently** during huge bursts. Mitigation: the git observer reconciles repos on any `.git` change and every 5 min. A root on a removable or network drive disappears: the watch is marked degraded and retried every 60 s. **OneDrive cloud-only placeholders:** we check `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS`/`OFFLINE` and **never read such files**, because reading would trigger a download. Antivirus or indexers touching files produce no events (they don't write). Long paths (> 260 characters) use the `\\?\` prefix. |
| **"Files opened"** | Honest answer: **Windows doesn't report file opens to ordinary processes.** Sources, best first: (1) editor extension (exact; VS Code and Cursor); (2) window titles (most editors and Office put the filename there); (3) `%APPDATA%\Microsoft\Windows\Recent\*.lnk` as a fallback (coarse; only apps that use the shell API; parsed with a small `.lnk` reader). Opens in apps that don't show a filename are unknowable. |
| **NOT captured** | File contents (not stored anywhere), reads, files outside watched roots, secret files' contents, cloud-only files. |
| **OS differences** | Linux uses inotify (watch limit `fs.inotify.max_user_watches`, often 8,192 by default, so a large tree needs it raised) and recent files from `~/.local/share/recently-used.xbel`. macOS uses FSEvents (coalesced, path-level). |

### 4.7 Code activity

**Git (source of truth for code progress)**

| | |
|---|---|
| **Captured** | Per repo: branch, HEAD, upstream, ahead/behind, staged/unstaged/untracked counts, changed files with numstat (up to 50), operation in progress (merge/rebase/cherry-pick/revert/bisect), stash count. New commits: sha, parent count, author-is-you flag, subject+body (≤2,000 characters, redacted), numstat summary, files. Branch switches, pulls/fetches (seen as ref changes). |
| **Mechanism** | git CLI via asyncio subprocess with `--no-optional-locks` and `GIT_OPTIONAL_LOCKS=0`, so we never take `index.lock` and never get in the way of your own git commands. `git status --porcelain=v2 --branch -z`, `git log --format=… -z`, `git diff --numstat -z`, `git stash list`. **Repo discovery:** scan watched roots at startup (to depth 4, skipping ignored dirs) plus any new `.git` seen by the fs observer. **Triggers:** fs events under `.git/` (`HEAD`, `index`, `refs/**`, `ORIG_HEAD`, `MERGE_HEAD`, `rebase-merge/`, `logs/HEAD`) schedule a refresh for that repo (3 s debounce). Safety poll every 5 min for repos active in the last 24 h, hourly for others. |
| **Schema** | `git/state` (point, emitted only when the state hash changes), `git/commit`, `git/branch_switch {from,to}`, `git/op {kind, state: started\|finished\|aborted}`, `git/test_result` (from hooks, below). |
| **Test/lint status** | Inferred from terminal events whose command matches known test runners (`pytest`, `npm test`, `npx jest`, `vitest`, `cargo test`, `go test`, `mvn test`, `gradle test`, `ruff`, `eslint`, `mypy`) as `{runner, exit_code, failed_count?}`. The failed count is parsed only when output capture is enabled. Optional opt-in git hooks (`pre-commit`, `pre-push`) can report as well. |
| **Derived facts (context engine, phase 3–4)** | "Worked on `auth/session.py`, added `refresh_token()`, 3 tests failing", "`feature/oauth` is 4 ahead, 5 files uncommitted". These are computed from the events above, not captured as is. |
| **Failure modes** | Repo mid-operation: porcelain still parses. Huge repos: status is capped at 10 s, then marked slow and the repo polled less often. `safe.directory` errors: the repo is reported as not readable, and we never change your git config. Submodules are treated as separate repos. |
| **NOT captured** | File contents, diffs' text (numstat only), other authors' commits beyond counts, credentials (the remote URL has userinfo stripped). |

**Editor extension (VS Code + Cursor; phase 1 stretch, otherwise phase 2)**

- Captures `editor/focus` intervals `{workspace, file (relative), language, symbol_path}`. The symbol comes from `vscode.executeDocumentSymbolProvider` (cached per document version, at most 1 lookup per 2 s). Also `editor/tabs` snapshot (on change, throttled 10 s), `editor/debug {start,end, config_type}`, `editor/task {name, exit_code}`, and **integrated terminal commands** via `window.onDidEndTerminalShellExecution` (shell integration API: command line + exit code).
- Dedupe with shell hooks: if both report the same command (same cwd, start within 1 s, same text), keep the hook version and enrich it with `host=vscode`.
- No document text, no keystrokes, no selections. Talks to `/v1/ingest` with the token. Buffers 500 events while the daemon is down.

### 4.8 Terminal and shell

| | |
|---|---|
| **Captured** | Command line (redacted), cwd, repo root + branch (computed daemon-side from cwd, not in the hook), start time, duration, success flag, exit code, shell, shell session id, host (Windows Terminal/VS Code/conhost, from the parent process). |
| **PowerShell 5.1 mechanism** | `install-hooks` adds a delimited block to `$PROFILE` (`# >>> personal-os >>>` … `# <<< personal-os <<<`). It wraps the existing `prompt` function. The first statement captures `$?` and `$LASTEXITCODE`, then `Get-History -Count 1` gives `Id, CommandLine, StartExecutionTime, EndExecutionTime`. If `Id` is unchanged since the last prompt (empty Enter), nothing is emitted. One JSON line is appended to `spool\terminal\pwsh-<PID>.jsonl` with `[IO.File]::AppendAllText`. Measured cost is about 8–11 ms per prompt in PowerShell 5.1 (first estimate was 3 ms; see BUDGET.md). The record is hand-built JSON, because `ConvertTo-Json` alone costs several ms. Your original prompt output is preserved. The hook body runs in `try/catch`, so a bug can never break your prompt. |
| **Git Bash mechanism** | A small `DEBUG` trap plus `PROMPT_COMMAND` hook (our own, about 60 lines) in `~/.bashrc`, the same marked block. One subshell per prompt (`history 1`); forks are slow on Windows. `$EPOCHREALTIME` for timing. Appends to `spool/terminal/bash-<pid>.jsonl`. |
| **Private commands** | A command line beginning with a space is **never written to the spool** (checked inside the hook, before any I/O). The same holds for any command when `PERSONAL_OS_PAUSE=1` is set in that shell. |
| **Redaction** | Done in the daemon's privacy filter (§4.3), not in the hook (keeps hooks tiny). **Risk, stated:** while the daemon is down, unredacted command lines sit in the spool, in your user-private `%LOCALAPPDATA%`. That's the same exposure PSReadLine already has with its own plain-text history file (`ConsoleHost_history.txt`). The daemon redacts and deletes spool files at startup. |
| **Output capture** | **Not in v1.** PowerShell 5.1 and Windows Terminal have no hook for a command's output without wrapping every command or running `Start-Transcript` (which captures everything). Option for later: an explicit `pos run <cmd>` wrapper that tees the first/last N lines of failed commands. |
| **Schema** | `terminal/command` (interval): `{command, cwd, shell, shell_session, host, exit_code?, success, duration_ms, history_id}`. |
| **Failure modes** | Profile not loaded (`-NoProfile`, or the execution policy blocks profiles): `doctor` checks the execution policy and says so. Very long one-liners are truncated at 4 KB. Multi-line commands are kept with `\n`. Spool files from crashed shells are ingested by mtime and deleted after ingest. |
| **NOT captured** | Keystrokes, output (v1), commands starting with a space, `cmd.exe` commands (no hook point; Clink could add one later), commands inside SSH sessions on remote hosts. |
| **OS differences** | Zsh: native `preexec`/`precmd`. Fish: `fish_preexec`/`fish_postexec` events. Linux and macOS spool to `$XDG_STATE_HOME/personal-os/spool`. |

### 4.9 Optional and later sources (interfaces now, built later)

| Source | Default | Mechanism (Windows) | Captured / NOT captured |
|---|---|---|---|
| Clipboard | **off** | `AddClipboardFormatListener` on the message window; text formats only | ≤1,000 chars, redacted, 24 h retention. Never images or files, never while a blocked app is in the foreground. |
| Calendar | off | Phase 7 integration (provider per Q8) | Titles, times, attendees count. Not bodies. |
| Meeting presence | off | Process/window detection (Teams, Zoom, Meet tab) + `IAudioSessionManager2` "mic in use" flag | "In a meeting, 35 min". **Never audio.** |
| Document titles | on with window | Derived from the window title of Office, Acrobat, SumatraPDF | Filename/title only. |
| Notifications | off | `UserNotificationListener` (WinRT) needs packaged-app identity, **likely infeasible from plain Python**; flagged | — |

### 4.10 Global pause and "forget last N minutes"

- **Pause** (`personal-os pause [--for 30m]`, a global hotkey via `RegisterHotKey` on the message window (default `Ctrl+Alt+Shift+P`, configurable), the tray from phase 6). Pause sets a persisted flag. Observers stop emitting and the filter drops everything as a second guard. Shell hooks check a `paused` marker file (a single `Test-Path`, about 0.1 ms) and skip writing. The extension is notified through the host and stops. Resume is explicit, or automatic after the timer. Pause and resume are recorded as `meta` events (time only).
- **Forget** (`personal-os forget 15m` or `--since/--until`) deletes, in one transaction: events overlapping the window, their page extracts, FTS rows, vector rows, sessions and summaries whose evidence intersects the window (deleted and queued for regeneration from the remaining evidence), and spool lines and queued in-memory events in the window. The database runs with `PRAGMA secure_delete=ON`, and a `wal_checkpoint(TRUNCATE)` follows, so deleted text isn't left in free pages or the WAL. The action audit log is append-only by design: entries in the window keep their tool name and timestamp but have their arguments and results **tombstoned** (ADR-011). A `meta/forget {from,to}` marker is kept so later answers can say "you deleted data from this period".

### 4.11 Health: `personal-os doctor`

Per observer: state, last event time, events in the last hour, drops, restarts, last error. Also: daemon CPU% (1 min/1 h averages) and RSS, DB size and WAL size, spool backlog, filter blocked/sensitive counters, Ollama reachability and loaded models, hook installation status per shell (profile block present? execution policy?), extension last-seen per browser, clock skew between the extension, hooks, and the daemon. Exit code is nonzero if anything is degraded, so it can be scripted.

### 4.12 Resource budget and how it's measured

Target: **idle CPU < 2%** (of one machine, all cores), **RSS < 300 MB** for the daemon, excluding Ollama.

Estimated: 1 Hz window poll (about 50 µs per tick), idle poll merged into the same tick, watchdog threads blocked in the kernel, git refreshes event-driven, Python + FastAPI + pydantic baseline RSS around 80–120 MB.

Measurement: `scripts/measure_budget.py --minutes 60` samples `psutil.Process(daemon).cpu_percent()` and `memory_info().rss` every 5 s, and writes p50/p95/max to `docs/BUDGET.md` along with machine state (on AC/battery, which apps were open). It runs as part of the phase 1 acceptance in a normal 1-hour work session. A synthetic stress run (the simulator at 50 events/s) checks behaviour under load.

### 4.13 Fake desktop simulator

- **Scenario DSL** (TOML, read with the stdlib `tomllib`, so no YAML dependency): a timeline of human-level actions ("09:00 focus Code in repo `foo` on `src/auth.py` for 25 m; save 6 times adding `refresh_token`; run `pytest` → exit 1; open Brave, search 'httpx timeout retry', read SO question 4 min; commit 'add refresh'"). The generator expands each action into the exact events each real observer would emit, including flicker, debounce artifacts, and idle gaps.
- **Recorder/replayer:** `personal-os record --minutes 60` writes post-filter events to JSONL (already redacted). `personal-os replay file.jsonl --speed 60x --clock sim` feeds them through the real pipeline into a scratch DB.
- **Fault injection:** observer crash, bus full, DB locked, Ollama down, clock jump (sleep), duplicate events.
- The simulator publishes through the same `EventBus` and pipeline as live capture. Only the observers are swapped out, so end-to-end tests cover the real filter, writer, and later the context engine.

### 4.14 What the system cannot know (stated up front)

- Which file you **opened** in apps that don't show it in the title (§4.6).
- Terminal **output** (v1), and anything in `cmd.exe`.
- Foreground window details for elevated apps (exe and cwd hidden; title visible).
- Whether you're **reading** or away when there's no input and no video (the 5-minute idle threshold is a guess; configurable).
- Activity in incognito, blocked apps or sites, while paused, and on other devices.
- Brave Tor windows and anything in browsers without the extension (Firefox until it's built).

---

## 5. Storage (overview; full schema in phase 0)

### 5.1 Tables

Migration `0001` creates only what phase 0 uses: `events`, `page_extracts`, both FTS tables, `counters`, and `kv_settings`. Each later table arrives in its own migration in the phase that first needs it, which also exercises the migration path early. Timestamps are stored as integer Unix milliseconds (UTC).

- `events` (`id` PK, `source`, `type`, `ts_start`, `ts_end`, `tz_offset_min`, `project_id`, `project_confidence`, `privacy_class`, `retention`, `expires_at`, `observer_version`, `payload` JSON). Indexes on `(ts_start)`, `(source, type, ts_start)`, `(project_id, ts_start)`.
- `page_extracts` (`id`, `event_id`, `content_hash`, `title`, `meta_description`, `headings` JSON, `main_text`, `expires_at`), deduplicated by `content_hash`.
- `events_fts` (FTS5, external content, `unicode61 remove_diacritics 2` tokenizer): searchable text per event (title, url, command, commit message, extract text), kept in sync with triggers.
- `embeddings` (sqlite-vec `vec0`, phase 4), `sessions`, `projects`, `project_state`, `entities`, `edges`, `summaries` (with `evidence_event_ids`), `action_audit` (phase 5), `kv_settings`, `counters` (blocked/drop counters by day and source).

### 5.2 Migrations

Numbered SQL files (`0001_init.sql`, …) applied in order inside a transaction, tracked with `PRAGMA user_version`. Forward-only, with a backup copy (`VACUUM INTO`) before each migration. No Alembic, since there's no ORM.

### 5.3 Encryption at rest

Phase 0 ships plaintext SQLite in the user-private `%LOCALAPPDATA%` (protected by your Windows account; Windows 11 Home may have "Device encryption" on, which you can check in Settings → Privacy & security). Option for later: SQLCipher via `sqlcipher3` wheels, with the key protected by Windows DPAPI through `keyring`. **Risk:** sqlite-vec has to be validated against the SQLCipher build. Deferred (your answer to Q8): plaintext for now, encryption added in a later phase.

### 5.4 Retention defaults

Raw events 30 days, page text 90 days, terminal output (when enabled) 14 days, clipboard 24 hours, sessions and summaries kept until you delete them. A nightly job (on AC power, while idle) deletes expired rows and runs `PRAGMA incremental_vacuum`. Export (JSON/Markdown), full wipe, per-project delete, and `inspect` (row counts and bytes per table, source, and privacy class) are all in the CLI.

---

## 6. Upper layers (sketch, designed in detail at their phase)

### 6.1 Project resolver and sessions (phase 3)

**Projects.** Every git repo the system has seen becomes a project automatically. Its id is the folder name (with a suffix if two repos share a name), its root is the repo path, and its GitHub `owner/repo` comes from the remote URL (credentials stripped). You can add projects that aren't repos (`personal-os projects add NAME --root PATH --keyword ...`), rename or archive them, and there's a built-in `none` project meaning "not project work" (Netflix, personal mail, chats).

**Resolver.** A background job, every 30 s, gives each new event a project and a confidence. Each signal proposes candidates; the highest confidence wins:

| Signal | Applies to | Confidence |
|---|---|---|
| Your correction of this event | any | 1.0, never overwritten |
| Path under a project root (repo root, file path, terminal cwd, editor workspace) | git, fs, terminal, editor, window cwd | git 1.0, fs 0.98, terminal/editor 0.95, window cwd 0.6 |
| Window title names a project folder ("file - **folder** - Visual Studio Code", "**folder** - File Explorer") | window | editors 0.9, others 0.65 |
| GitHub page of a project's repo (`owner/repo` matches the remote) | browser | 0.9 |
| Rules learned from your corrections (site, GitHub repo, path, app + title, app) | browser, window, terminal, fs | GitHub repo up to 0.95, path 0.9, app + title 0.85, site 0.8; 0.1 less while fewer than two corrections back it. A whole app is weaker: 0.55, then 0.65, then 0.8 as corrections agree |
| Keyword overlap between the page/title and the project's vocabulary (name, plus words from its commit messages, files and confidently assigned pages: TF-IDF across projects) | browser, editor windows | 0.45 + 0.05 per matching word, at most 0.7. Only with at least two words each seen twice in the project, at least one word that few projects use, and a winner 1.6× ahead of the runner-up |
| Page: a browser window follows the page visit it shows (a hidden window pairs with a hidden visit by time) | window | 0.95 × the page's confidence |
| Temporal: what you were working on in the 10 minutes around it (only confident, direct neighbours count) | browser, window, terminal outside repos | 0.6 × the neighbours' agreement + 0.05, at most 0.65, needs 70% agreement |

When two weaker signals agree on the same project (say a keyword match and the temporal context), they combine as independent evidence, 1 − (1 − a)(1 − b), capped at 0.84 so a combination never looks like a direct signal.

Below 0.5 an event stays **unassigned** and goes to the inbox. System, idle and meta events never get a project. The resolver uses words only. Embeddings arrived in phase 4 for retrieval; adding them to this signal is deferred until they've proved themselves there (§6.2).

**Inbox and learning.** `personal-os inbox` groups unassigned items, and guesses below 0.7 that came only from keywords or timing, by what they have in common (a site, an app and title, a folder), with counts, time spent and the current guess, so one `personal-os assign <group> <project>` fixes many. Assigning a group stores a rule for it; assigning one event only fixes that event (add `--learn` for a rule). The resolver then re-runs over the last 30 days except your corrections, so the rule applies straight away and to future events. A site used for several projects (Stack Overflow) gets conflicting corrections, which lowers its rule's confidence, so the keyword and temporal signals win there.

**Sessions.** Rebuilt every 5 minutes for the last 24 hours, and for any range on demand. The day is cut into one-minute buckets, each labelled with the project that had the most time in it (window time by its resolved project, plus a little for each other event). A gap of 15 minutes or more (idle, locked, asleep or nothing happening) ends a session. Within a block, project runs shorter than 10 minutes are absorbed by their neighbours, so a quick Stack Overflow detour doesn't split a coding session. Each session stores its start, end, project, active time and stats: apps and time, files touched, pages read, commands run (and how many failed), commits. Phase 4 summarises sessions with the LLM.

**Privacy.** Learned keyword profiles are recomputed from stored events and never stored themselves, so `forget` removes their source. `forget` also deletes overlapping sessions, which are rebuilt from what's left. Rules hold only what you typed in a correction: a site, a path or an app.

**Acceptance.** Labelled simulated days across three projects plus personal browsing, including deliberately ambiguous research pages. Day 1 is a cold start; simulated corrections are made through the inbox (a group is assigned only if 80% of it belongs to one project, otherwise its items one by one); day 2 must score at least 90% of events correct (an unassigned event counts as wrong), and at least 85% of time. A second, harder day 2 was written after tuning as a hold-out (ADR-042).

### 6.2 Local models, summaries, retrieval and `ask` (phase 4)

**Models** (your answer: Qwen only), measured on this laptop on 2026-11-04:

| Role | Model | Measured |
|---|---|---|
| Summaries and answers | `qwen3:4b`, thinking off | About 45 tokens/s and 680 prompt tokens/s with every layer on the GPU (3.8 GB at an 8k context). Ollama's own estimate put only 2.3 GB on the GPU and ran at 7.5 tokens/s, so the provider asks for all layers (`gpu_layers`) and retries without that if loading fails. |
| Deeper answers | the same model with thinking on, only with `ask --think` | About 60 s per answer (2,000+ thinking tokens). Too slow to be the default. |
| Embeddings | `qwen3-embedding:0.6b` | Background batches on the GPU (about 550 tokens/s). A question is embedded on the CPU (0.5 s), so it doesn't push the chat model out of the 4 GB. |

Everything goes through an `LLMProvider` interface (`chat_json`, `embed`, `health`). The Ollama provider uses the standard library's HTTP client, so there's no new dependency. It accepts only a loopback `base_url`: captured context never leaves the machine. Structured output uses Ollama's JSON-schema `format`, then pydantic validation, with one retry that shows the model its validation error.

**Background jobs** (summaries, embeddings) run only **on AC power, while you've been idle for 2 minutes, with no full-screen app in front, and not while capture is paused** (ADR-014). They work one item at a time, recheck between items, and stop as soon as you come back. Models are loaded with a short `keep_alive`, so the VRAM is freed a couple of minutes later.

**Session summaries.** A session is summarised once it has ended (15 quiet minutes) and had at least 5 active minutes. The model gets an **evidence pack**, not raw events. Each line has a number and stands for one or more events:

- Window time is grouped by app and title.
- File saves are grouped by file.
- Repeated commands are folded together.
- Commits, failed commands, searches and pages read appear with their details: a page's title, site, time spent and description.

The pack is capped at about 80 lines, keeping commits, failures, files and pages before plain window time. The model returns JSON:

- `goal` and a confidence for it.
- `actions`, `outcomes` and `open_threads`, each an item with text and the lines it rests on.
- An optional `next_step`.

Validation drops any item whose line numbers aren't in the pack, so every remaining statement cites real events. The stored summary keeps the event ids behind those lines (`evidence_event_ids`). It also keeps a hash of the pack, so a session whose events change (a correction, a later event) is summarised again, and an unchanged one never is. Sensitive events appear only as "site, time". Their content never reaches the model.

**Embeddings** are stored as float32 vectors in an ordinary table (the first 512 of the model's 1024 dimensions, renormalised; the model is trained for this). They're searched by brute force, with no `sqlite-vec`. Embedded:

- Page visits: title, description, headings and the start of the text.
- Searches.
- Commits.
- Session summaries.

Sensitive events are never embedded. Deleting an event, an extract or a summary deletes its vector through triggers, and `secure_delete` zeroes it, so `forget` and retention keep their guarantees (ADR-044).

**Retrieval** is hybrid. Full-text search (events, page text, summaries) and vector similarity each give a ranked list. They're fused by reciprocal rank, plus a recency term, inside an optional project and time filter. Without the embedding model it falls back to full-text plus recency, and says so.

**`ask`.** A question is parsed without the model, so routing is predictable and testable:

- A project, by id, name or words of its name.
- A time range: today, yesterday, this or last week, the last N days, since a day, between two times.
- An intent: where I left off, what I did, research on a topic, a bug, weekly progress, what changed since I last touched a project, or a general search.

Each intent collects its own evidence. For "where I left off" that's the project's last session and its summary, the last commits, files and commands, the last failing command, and the repo's current git state. The evidence becomes one numbered pack.

- **No evidence:** the answer is "I don't have data on that", with what was looked for. The model isn't asked.
- **Otherwise:** the model answers as JSON sentences, each citing pack lines. Sentences without a valid citation are dropped. If none survive, or Ollama isn't running, `ask` falls back to listing the facts it found.

The answer ends with its confidence and the numbered evidence (time, kind, what, event id).

**Captured text is data.** Page text, titles, commands and commit messages are passed only inside a delimited data block. The prompt says plainly that the block's content is never instructions. In phase 4 the model has no tools at all. The phase 5 injection tests build on this.

**Not in phase 4:** stored daily and weekly rollups (weekly questions are answered from session summaries at question time), the stored "current state" note per project (assembled at question time), conversation memory, and embeddings in the project resolver (later, once the vectors have proved themselves in retrieval).

### 6.3 Action layer (phase 5)

**Tools.** Every action is a typed tool in a registry. A tool has:

- a name and a one-line description;
- a pydantic argument model;
- a risk tier and whether it can be undone;
- a `preview` (exactly what will happen: the path, the command line, the diff) used for dry runs and confirmations;
- `run`, which returns a result and, where possible, an undo recipe.

There is no free-form shell: an LLM can only fill in a tool's arguments.

| Tier | Tools in phase 5 | Policy |
|---|---|---|
| 0, read-only | `search_context`, `read_file`, `list_folder`, `system_status` | Runs without asking |
| 1, low-risk, reversible | `open_path`, `open_url`, `open_app`, `create_note`, `copy_to_clipboard`, `media` (play/pause, next, previous, mute, volume), `screenshot`, `window` (focus, minimize, maximize, restore) | Runs without asking when you asked for it directly (your answer to Q5). Asks when the request was proactive, or when its arguments could have come from captured content |
| 2, confirm every time | `create_file`, `edit_file` (shown as a diff), `move_path`, `run_command` (named commands from config only), `git_commit`, `git_switch`, `git_stash` | Always asks. There's no "always allow" |
| 3, blocked | `delete_path` (to the Recycle Bin), `run_shell` (a command outside the named list) | Blocked. `actions unlock TOOL --minutes N` (confirmed at the keyboard) allows it for a while, and each use still asks. Never for tainted or proactive calls |

Not in phase 5:

- **Need phase 6 notifications:** brightness, do-not-disturb, timers and reminders.
- **Phase 7:** email, calendar and package installs.
- **Web search:** an `open_url` with a search URL does the job without an API.

**Where a request comes from.** Each call carries an origin and a taint flag:

- `user`: you named the tool yourself (`personal-os actions run …`).
- `planned`: the planner chose it from words you typed (`personal-os do "…"`).
- `proactive`: a later phase suggested it.

**Taint.** A call is tainted once anything captured has been put in front of the planner, such as a `search_context` or `read_file` result in the same request. From then on, tier 1 asks, and tier 3 is blocked even if unlocked.

**Safety rails, checked before any confirmation:**

- **Paths:** paths must resolve, after following links, inside your watched folders, the notes and screenshots folders, or `actions.allowed_paths`. They're never secret paths (the privacy filter's globs: `.env`, keys, `.ssh` and so on), and never the data folder itself.
- **URLs:** only http(s), never a blocked domain.
- **Commands:** run as argument lists without a shell, with a timeout.
- **Rate limits:** 30 actions a minute, 10 of tier 2 or above.
- **Exfiltration guard:** a network-capable call (`open_url`) whose arguments contain a long run of text read earlier in the same request is blocked.

**Confirmation** shows the tool, tier, origin and preview, and waits for **y** typed at the keyboard. With no keyboard (a background process, a test), it's declined.

**Audit log.** `action_audit` is append-only (SQLite triggers refuse updates and deletes). Every stage gets its own row: proposed, auto-allowed / confirmed / declined / blocked (with the reason), started, succeeded / failed / cancelled, undone. Each row records the origin, taint, arguments, result and undo recipe. `forget` replaces the arguments in its window with a tombstone instead of deleting rows (ADR-011). `personal-os actions log` shows it. `personal-os actions undo ID` runs the undo recipe through the same policy.

- `edit_file` keeps the original in `undo/` (deleted after 7 days or by `forget`) so it can be restored. An undo is refused if the file changed since.

**Kill switch.** `personal-os actions kill`, or the global hotkey **Ctrl+Alt+Shift+K** registered by the daemon, does three things:

- it cancels whatever is running (a command's process is terminated);
- the planner stops;
- everything goes **observe-only** until `personal-os actions resume`.

**Planner** (`personal-os do "…"`). The model returns a JSON plan, `{calls: [{tool, args}], reply}`, constrained by a schema with thinking off. It took 1.3–3.4 s per plan in testing; native tool calling made `qwen3:4b` think first (5–42 s).

- **What the planner sees:** your request, the tool list (tiers 0–2; tier 3 never), project names and folders, app names and named commands. No captured content.
- **Two-round requests:** if a plan uses a tier-0 lookup, its result comes back inside a data block for one more round, and every call after that is tainted.
- **Checking:** each call is validated against the tool's argument model and then goes through the policy.

Injection tests (planted instructions in page text, commit messages, file names and command output) run in the simulator against a worst-case fake model that obeys them. They also run against `qwen3:4b`.

### 6.4 Interfaces

CLI first (`typer`): `status`, `doctor`, `pause`, `resume`, `forget`, `inspect`, `export`, `wipe`, `install-hooks`/`uninstall-hooks`, `install-native-host`, `record`/`replay`, then `ask`. Tray (phase 6) via `pystray`.

**Dashboard reachable from your phone** (your answer). The core API never leaves `127.0.0.1`. In phase 6 the dashboard gets a **second listener** on your LAN address with these rules:

- **Off until you enable it.** HTTPS with a self-signed certificate generated locally.
- **Device pairing.** You scan a QR code shown on the laptop, and each phone gets its own revocable token.
- **Limited routes.** Only dashboard and query routes, never `/ingest`. Control actions limited to pause and forget.
- **No action confirmations from the phone at first.** Tier 2+ can't be confirmed from it.
- **Firewall.** Windows Firewall needs an inbound rule, which takes admin rights. The app prints the exact command for you to run; it doesn't change firewall settings itself.
- **Away from home.** Reaching it from outside your network (for example over Tailscale) is out of scope until you ask.

### 6.5 Google integrations (phase 7)

Calendar and Gmail come first (your answer); Obsidian and other notes come later. These are the system's **first cloud calls**, so they're opt-in per feature:

- **Sign-in.** OAuth through the installed-app loopback flow, so the sign-in happens in your browser. Refresh tokens are stored with `keyring`, which uses Windows Credential Manager.
- **Read-only scopes first.** Write scopes (create events, send mail) are added only together with their Tier 2 tools.
- **Email bodies stay out of storage and LLM prompts by default.** Headers, sender, and subject are used for triage.
- **Not in `wipe`.** A wipe deletes the local tokens, but revoking access in your Google account is a separate step that you do in Google's settings.

---

## 7. Testing strategy

- **Unit:** each observer's pure logic (normalization, debounce, flicker merge, burst collapse, porcelain parsing, PowerShell spool parsing) is tested with fixtures and a fake clock. No live OS calls in unit tests.
- **Privacy golden corpus:** about 200 strings with planted secrets (every detector) plus about 200 benign look-alikes (git SHAs, UUIDs, paths, base64 images). Must reach 100% recall on planted secrets and keep false positives under 2% on the benign set. Property tests with `hypothesis`.
- **Contract tests:** every observer must satisfy the `Observer` protocol (start/stop within 5 s, health non-blocking, emits valid envelopes).
- **End-to-end:** simulator scenarios → pipeline → DB, with assertions on stored rows, plus `forget` and pause correctness.
- **Live tests** (`-m live`, Windows only, not run by default): open Notepad and check a focus interval; touch a file under a temp root; run a command in a spawned PowerShell with the hook installed.
- **Budget test:** `measure_budget.py` (phase 1 acceptance).

---

## 8. Risks and open technical questions

| Risk | Mitigation / fallback |
|---|---|
| Brave may not read Chrome's native-messaging registry key | Verify first thing in phase 2. Fallback: Brave's own key, or the localhost API with the token pasted into the extension options page. |
| PowerShell execution policy blocks `$PROFILE` | `doctor` detects it; `install-hooks` explains `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` and **does not change it for you**. |
| `ReadDirectoryChangesW` overflow during big builds | Burst collapse plus git reconciliation. Never trust fs events alone for code state. |
| Window title churn floods events (browsers, music players) | 3 s title stability rule plus per-app "ignore title" rules. |
| SQLCipher + sqlite-vec compatibility | Spike before committing to encryption (Q8). |
| 4 GB VRAM limits model quality | Keep prompts small and structured, retrieval-first. Make "deep mode" explicit and slow-but-available. |
| Python native crash takes down all observers | The launcher restarts the daemon; keep the `ctypes` surface minimal and covered by live tests; per-observer subprocess isolation flag if it ever happens. |

---

## 9. Phase 0 build plan (after approval)

1. Scaffold: `uv` project, `pyproject.toml`, ruff + pytest config, package layout, logging (JSON lines, no content).
2. Config schema (pydantic) + `defaults.toml` with safe defaults + loader/writer.
3. `core`: Clock (real/sim), UUIDv7, `Event` envelope + payload union for all §4 sources, EventBus with drop policy, Supervisor.
4. `privacy`: rules, redactors, entropy detector, filter (fail closed), golden corpus + hypothesis tests.
5. `storage`: connection setup (WAL, `secure_delete`, `foreign_keys`), migrations runner, `0001_init.sql`, BatchWriter with upsert intervals, FTS triggers, `forget` and retention primitives.
6. `sim`: scenario DSL, generator, replayer; e2e test: scenario → bus → filter → DB.
7. CLI skeleton: `status`, `inspect`, `forget`, `replay`.

**Acceptance:** a simulator day flows end to end; redaction and fail-closed tests pass; `forget` removes all traces (verified by scanning the DB file bytes for planted markers).

---

## 10. Your answers (2026-11-02)

| # | Question | Answer | Where it changed the design |
|---|---|---|---|
| 1 | Daily tools | Brave is the default; test with Chrome for now. VS Code. Terminal is my call | §1. PowerShell 5.1 hook first, then Git Bash. |
| 2 | Project folders | `Documents` only | §1, `defaults.toml` (`watched_roots`). |
| 3 | Never observe | Messaging sites like WhatsApp and Instagram | §4.3. Domains blocked, desktop app blocked, titles redacted. |
| 4 | Dashboard reach | Reachable from phone too | §6.4. Separate LAN listener, HTTPS, pairing, phase 6. |
| 5 | Action aggressiveness | Allow Tier 1 automatically | §6.3. Only for requests you made directly. |
| 6 | Integrations first | Google (calendar, email); Obsidian etc. later | §6.5. Phase 7. |
| 7 | Models | Qwen only | §6.2. `qwen3:4b` + `qwen3-embedding:0.6b`. |
| 8 | Encryption, location | Later; data on C: | §5.3 unchanged. Data dir `%LOCALAPPDATA%\PersonalOS`. |
