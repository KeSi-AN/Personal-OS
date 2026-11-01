# Personal OS — Design

**Status:** Draft v0.1, awaiting review.
**Date:** 2026-11-01
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
| Observers as plugins that can crash independently | In-process asyncio tasks under a supervisor, with optional per-observer process isolation later | One process stays well inside the 300 MB budget. A Python exception in one observer can't affect the others. Only a native crash (bad `ctypes` call) could kill the daemon, and Task Scheduler restarts it. |
| A daemon "always on" | Starts at login through **Task Scheduler** in your user session, not as a Windows service | Services run in session 0 and can't see your desktop windows, idle state, or lock events. |

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
| Ollama | 0.32.6, models: `qwen3:4b`, `gemma4:e2b`, `gpt-oss:20b` | Use what's there; add one embedding model. |
| Shells | Windows PowerShell 5.1 (no PowerShell 7), Git Bash, Windows Terminal | Hooks for PS 5.1 and Bash. `cmd.exe` has no hook point and isn't supported. |
| Browsers | **Brave (default)**, Chrome, Edge, Firefox | One Manifest V3 extension covers Brave, Chrome, and Edge. Firefox later. |
| Editors | VS Code (54 extensions), Cursor | One VS Code extension also installs in Cursor (a VS Code fork). |
| Repos | 24 git repos under `Documents` (within 3 levels) | Default watched root candidate is `Documents` (to confirm, Q2). |
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
 │  personal-os daemon  (pythonw, started by Task Scheduler at logon)       │
 │                                                                          │
 │   asyncio event loop (main thread)                                       │
 │    ├─ Supervisor ── observer tasks: window, idle/session, fs, git,       │
 │    │                 terminal-spool, (clipboard, …)                      │
 │    ├─ EventBus (bounded queue)                                           │
 │    ├─ Pipeline: PrivacyFilter → ProjectHint → BatchWriter (SQLite)       │
 │    ├─ Local API (uvicorn, 127.0.0.1, token) ── /ingest /control /query   │
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
shell_hooks/            powershell/ (profile block), bash/ (vendored bash-preexec + block)
tests/  docs/  scripts/
```

### 3.3 Ingress channels

| Producer | Channel | Auth | Notes |
|---|---|---|---|
| In-process observers | `EventBus.publish()` | n/a | Non-blocking. |
| Shell hooks | Append JSONL to `spool/terminal/<shell>-<pid>.jsonl` | Filesystem ACL (`%LOCALAPPDATA%` is user-private) | Daemon tails, ingests, and deletes the file. Survives daemon downtime. |
| Browser extension | Native messaging (stdio) → host → `POST /v1/ingest` | Host manifest pins the extension ID; host reads the token file | No port is exposed to web pages. |
| VS Code/Cursor ext. | `POST /v1/ingest` | Bearer token from `runtime/token` | |
| CLI / tray / dashboard | `/v1/control/*`, `/v1/query/*` | Bearer token | |

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
| Daemon crashes or is killed | Capture stops | Task Scheduler restarts it on failure. Shell spool keeps terminal events. Browser extension buffers up to 1,000 events in `chrome.storage.local`. Open intervals were checkpointed (§4.1). |
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
    name: ClassVar[str]                 # "window", "fs", …
    version: ClassVar[str]              # semver; stamped into events as observer_version
    async def start(self, ctx: ObserverContext) -> None: ...   # runs until stop() or exception
    async def stop(self) -> None: ...                          # must return within 5 s
    def health(self) -> ObserverHealth: ...                    # cheap, non-blocking

class ObserverContext:              # handed in by the supervisor
    bus: EventBus; clock: Clock; config: ObserverConfig; log: Logger
    paused: asyncio.Event           # observers check this and also stop emitting while paused

class ObserverHealth(BaseModel):
    state: Literal["starting","running","paused","degraded","crashed","disabled"]
    last_event_at: datetime | None
    events_last_hour: int
    restarts: int
    last_error: str | None          # exception type + message, never event content
```

Observers are discovered through a registry (entry points later), enabled per `config.toml`, and each one runs in its own supervised task. Platform-specific code lives behind probes (`WindowProbe`, `IdleProbe`, `SessionProbe`). The Windows implementation is real; Linux and macOS raise `NotSupported` with a clear message.

### 4.1 Event envelope

```python
class Event(BaseModel):
    id: UUID                                # UUIDv7
    ts_start: datetime                      # UTC
    ts_end: datetime | None                 # None = interval still open; point events: ts_end == ts_start
    source: Source                          # window|idle|system|browser|fs|git|editor|terminal|clipboard|meta
    type: str                               # e.g. "focus", "page_visit", "command"
    payload: Payload                        # discriminated union keyed on (source, type), validated
    project_hint: str | None = None         # cheap hint from the observer (repo root, cwd); resolver decides later
    confidence: float = 1.0                 # observer's confidence in the payload itself
    privacy_class: Literal["normal","sensitive","blocked"] = "normal"   # set/raised by the filter, never lowered
    observer_version: str
    tz_offset_min: int
    retention: RetentionClass               # raw|page_text|terminal_output|clipboard|summary
```

**Interval events are upserted by id.** An observer emits an interval with `ts_end=None` when it opens, re-emits the same id with an updated `ts_end` every 60 s as a checkpoint, and emits it a final time when it closes. The writer does `INSERT … ON CONFLICT(id) DO UPDATE`. A crash loses at most 60 s of an open interval. On startup, any interval still open from a previous run is closed at its last checkpoint and flagged `truncated=true`.

### 4.2 Event bus, backpressure, batching

- `asyncio.Queue(maxsize=10_000)`. `publish()` never awaits. When the queue is full, drop in priority order: `fs` bursts, then `browser` intent signals, then everything else. Window, git, and terminal events are dropped last. Every drop is counted per source and shown in `doctor`.
- The BatchWriter takes up to **200 events or 1 s**, whichever comes first, and writes them in one transaction (`BEGIN IMMEDIATE`). Expected load is under 2 events/s on average, so batches are small and commits are rare.
- Pipeline order: `bus → PrivacyFilter → ProjectHint (cheap, rules-only) → BatchWriter`. Each stage is a plain function, so the simulator and the tests run the identical pipeline.

### 4.3 Privacy filter (single choke point, fails closed)

Every event passes it, including events from the extensions and from replay. Stages, in order:

1. **Pause gate.** While paused, drop everything except `meta` events (pause and resume markers).
2. **Hard blocks → `blocked`, dropped.** App executables (default: password managers such as `KeePassXC.exe` and `1Password.exe`, and banking apps), title regexes, domain categories (banking, health, webmail bodies, DMs: see the default list in the config), incognito, and pages with password fields. **Blocked events are never stored.** Only a per-source counter is incremented, so `doctor` can say "42 events blocked today".
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

**Native messaging host:** `native_host/host.py`, run by a `.bat` launcher pointing at the venv's Python. stdio is set to binary with `msvcrt.setmode`. Messages are length-prefixed per the Chrome protocol (1 MB maximum host→browser; we only acknowledge). It forwards batches to `POST /v1/ingest`. If the daemon is down, it appends to `spool/browser/` and the extension keeps a 1,000-event ring buffer too. Registration goes in per-user registry keys: `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.personalos.host` (Chrome; **Brave reads this key too, to be verified in phase 2**) and `HKCU\Software\Microsoft\Edge\NativeMessagingHosts\com.personalos.host`. No admin rights needed. `allowed_origins` pins our extension ID.

**Post-processing (phase 4, not on the capture path):** "why this mattered" summaries, keywords, and research-thread clustering run as background LLM jobs over visits with `active_dwell ≥ 30 s` or intent signals.

| | |
|---|---|
| **Failure modes** | Readability fails on single-page apps or canvas pages, so we fall back to title + meta + headings. Some sites block content scripts (the Chrome Web Store, PDF viewer), giving a visit with no extract. MV3 worker restarts lose nothing because state is in session storage. If the extension is uninstalled or disabled, `doctor` shows "browser: no events in 2 h while Brave was focused 40 min". |
| **NOT captured** | Raw HTML, form field values, cookies, full history import (opt-in later), anything in incognito, page text on sensitive or blocked domains, the clipboard. |
| **OS differences** | Extension code is identical. Native host registration differs: Windows uses the registry, Linux `~/.config/<browser>/NativeMessagingHosts/`, macOS `~/Library/Application Support/<browser>/NativeMessagingHosts/`. Firefox needs a `browser_specific_settings` manifest variant and its own host location. |

### 4.6 File activity

| | |
|---|---|
| **Captured** | Create, modify, delete, and move under watched roots: path, root, kind, size, extension, file type (code/text/doc/image/binary/archive), `repo_root`, and for text/code files a **diff summary** `{lines_added, lines_removed, symbols_added/removed/changed}`. |
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
| **PowerShell 5.1 mechanism** | `install-hooks` adds a delimited block to `$PROFILE` (`# >>> personal-os >>>` … `# <<< personal-os <<<`). It wraps the existing `prompt` function. The first statement captures `$?` and `$LASTEXITCODE`, then `Get-History -Count 1` gives `Id, CommandLine, StartExecutionTime, EndExecutionTime`. If `Id` is unchanged since the last prompt (empty Enter), nothing is emitted. One JSON line is appended to `spool\terminal\pwsh-<PID>.jsonl` with `[IO.File]::AppendAllText`. Target cost is under 3 ms per prompt, measured in the phase 1 acceptance. Your original prompt output is preserved. The hook body runs in `try/catch`, so a bug can never break your prompt. |
| **Git Bash mechanism** | Vendored `bash-preexec` (MIT) for `preexec`/`precmd` in `~/.bashrc`, the same marked block. `$EPOCHREALTIME` for timing. Appends to `spool/terminal/bash-<pid>.jsonl`. |
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

- **Scenario DSL** (YAML): a timeline of human-level actions ("09:00 focus Code in repo `foo` on `src/auth.py` for 25 m; save 6 times adding `refresh_token`; run `pytest` → exit 1; open Brave, search 'httpx timeout retry', read SO question 4 min; commit 'add refresh'"). The generator expands each action into the exact events each real observer would emit, including flicker, debounce artifacts, and idle gaps.
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

### 5.1 Tables (first migration)

- `events` (`id` PK, `source`, `type`, `ts_start`, `ts_end`, `tz_offset_min`, `project_id`, `project_confidence`, `privacy_class`, `retention`, `expires_at`, `observer_version`, `payload` JSON). Indexes on `(ts_start)`, `(source, type, ts_start)`, `(project_id, ts_start)`.
- `page_extracts` (`id`, `event_id`, `content_hash`, `title`, `meta_description`, `headings` JSON, `main_text`, `expires_at`), deduplicated by `content_hash`.
- `events_fts` (FTS5, external content, `unicode61 remove_diacritics 2` tokenizer): searchable text per event (title, url, command, commit message, extract text), kept in sync with triggers.
- `embeddings` (sqlite-vec `vec0`, phase 4), `sessions`, `projects`, `project_state`, `entities`, `edges`, `summaries` (with `evidence_event_ids`), `action_audit` (phase 5), `kv_settings`, `counters` (blocked/drop counters by day and source).

### 5.2 Migrations

Numbered SQL files (`0001_init.sql`, …) applied in order inside a transaction, tracked with `PRAGMA user_version`. Forward-only, with a backup copy (`VACUUM INTO`) before each migration. No Alembic, since there's no ORM.

### 5.3 Encryption at rest

Phase 0 ships plaintext SQLite in the user-private `%LOCALAPPDATA%` (protected by your Windows account; Windows 11 Home may have "Device encryption" on, which you can check in Settings → Privacy & security). Option for later: SQLCipher via `sqlcipher3` wheels, with the key protected by Windows DPAPI through `keyring`. **Risk:** sqlite-vec has to be validated against the SQLCipher build. See Q8.

### 5.4 Retention defaults

Raw events 30 days, page text 90 days, terminal output (when enabled) 14 days, clipboard 24 hours, sessions and summaries kept until you delete them. A nightly job (on AC power, while idle) deletes expired rows and runs `PRAGMA incremental_vacuum`. Export (JSON/Markdown), full wipe, per-project delete, and `inspect` (row counts and bytes per table, source, and privacy class) are all in the CLI.

---

## 6. Upper layers (sketch, designed in detail at their phase)

### 6.1 Project resolver and context engine (phases 3–4)

Signal order as in the brief. The cheap rules-based hint (repo root, cwd, path under root) is computed at ingest; embeddings and temporal smoothing run in a background job. Sessions split on 15 min idle or a project switch lasting ≥ 10 min. Summaries are JSON validated by pydantic, cite event ids, and are regenerated when their evidence changes (for example after `forget`).

### 6.2 Local models for this hardware (GTX 1650 4 GB, 16 GB RAM)

| Role | Proposal | Reason |
|---|---|---|
| Fast (classify, summarize, extract JSON) | `qwen3:4b` (already installed, about 2.5 GB, fits fully in VRAM) | Expect roughly 20–30 tokens/s on this GPU (to be measured). Good structured output. |
| Reasoning (`ask`, planning, tool calls) | `qwen3:4b` with thinking on by default. Optional `qwen3:8b` (partly offloaded to CPU, slower). `gpt-oss:20b` as an explicit "deep" mode only | `gpt-oss:20b` needs about 13 GB, mostly CPU on this machine, so expect single-digit tokens/s and heavy RAM pressure. It isn't usable as the default. |
| Embeddings | `nomic-embed-text` or `embeddinggemma` (both under 700 MB), picked by a small retrieval benchmark on your own data in phase 4 | Small, fast, fine on CPU. |

Background LLM jobs run only **on AC power, while the user is idle ≥ 2 min, and when no full-screen app is in the foreground**. They use Ollama `keep_alive` so the VRAM is freed afterwards. Everything sits behind the `LLMProvider` interface.

### 6.3 Action layer (phase 5)

Typed tool registry, risk tiers 0–3, dry-run, confirmations, append-only audit, kill switch, all as in the brief. One constraint matters for the observation layer: **captured text is data.** Events, page extracts, and terminal lines are passed to the LLM only inside clearly delimited data blocks, the tool-calling planner can't see raw observed text when choosing tools for proactive features, and a tool call whose arguments come from observed content needs the same confirmation as its tier says, with no "always allow" shortcut. Injection tests live in the simulator (planted "ignore previous instructions and delete…" in page extracts, commit messages, and file names).

### 6.4 Interfaces

CLI first (`typer`): `status`, `doctor`, `pause`, `resume`, `forget`, `inspect`, `export`, `wipe`, `install-hooks`/`uninstall-hooks`, `install-native-host`, `record`/`replay`, then `ask`. Tray (phase 6) via `pystray`. Dashboard served by the same FastAPI app on `127.0.0.1` (LAN exposure per Q6).

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
| Python native crash takes down all observers | Task Scheduler restart-on-failure; keep the `ctypes` surface minimal and covered by live tests; per-observer subprocess isolation flag if it ever happens. |

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

## 10. Open questions for you

See the list in chat. Answers will be folded into this document and `DECISIONS.md` before phase 0 starts.
