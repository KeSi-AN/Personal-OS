# Architecture Decision Log

Each entry has a status (Proposed, Accepted, Superseded) and a one-paragraph rationale. Everything below is **Proposed** until you approve `DESIGN.md`.

| # | Decision | Status | Why |
|---|---|---|---|
| ADR-001 | Single daemon process, asyncio, observers as supervised in-process tasks | Proposed | Fits the 300 MB budget and is simple to debug. Python exceptions are isolated per observer by the supervisor. Per-observer subprocess isolation stays as a config flag if a native crash ever happens. |
| ADR-002 | Daemon runs at logon in the user session through Task Scheduler, not as a Windows service | Proposed | Services live in session 0 and can't see foreground windows, idle time, or lock events. Task Scheduler gives restart-on-failure without admin rights. |
| ADR-003 | Browser → daemon over native messaging; everything else over token-authenticated HTTP on 127.0.0.1 | Proposed | Native messaging pins the extension ID and opens no port to web pages. The CLI, editor extension, and dashboard need an API anyway. Host and Origin checks block DNS rebinding and web-page access. |
| ADR-004 | Shell hooks always append to a per-shell spool file, which the daemon tails | Proposed | Prompt latency can't depend on the daemon. Appending to a file is around a millisecond and can't hang. One delivery path covers both "daemon up" and "daemon down". |
| ADR-005 | git CLI (porcelain v2, `--no-optional-locks`) instead of GitPython | Proposed | Stable output contract, one less dependency, and it never competes with the user's git for `index.lock`. |
| ADR-006 | `ctypes` instead of `pywin32` for Win32 calls | Proposed | About 10 API functions are needed. Avoids a large dependency with a post-install step. |
| ADR-007 | Interval events are upserted by id, checkpointed every 60 s | Proposed | A crash loses at most 60 s of an open interval. One table holds both open and closed intervals. |
| ADR-008 | Privacy filter is a single choke point that fails closed | Proposed | A filter bug may lose data but can never leak it. |
| ADR-009 | Plain SQL migrations tracked with `PRAGMA user_version`; no ORM | Proposed | SQLite-specific features (FTS5, vec0, upserts, triggers) are easier in SQL. Fewer dependencies. |
| ADR-010 | CPython 3.12 managed by `uv` | Proposed | Widest wheel availability for later phases (voice/ML). 3.13 also works for phases 0–6. |
| ADR-011 | `forget` tombstones action-audit arguments instead of deleting the rows | Proposed | Keeps the audit log append-only (who did what, when) while still removing captured content from the forgotten window. |
| ADR-012 | Own UUIDv7 implementation for event ids | Proposed | Time-sortable ids with no dependency. The stdlib only gets `uuid7` in 3.14. |
| ADR-013 | No file shadow copies; diff summaries come from git numstat, or line/size deltas outside git | Proposed | Storing copies of your files would contradict "context, not logs" and enlarge the privacy surface. |
| ADR-014 | LLM background jobs gated on AC power + user idle + no full-screen app | Proposed | Laptop with a 4 GB GPU. Summaries mustn't drain the battery or slow down active work or games. |
| ADR-015 | Default data dir `%LOCALAPPDATA%\PersonalOS`, outside Documents and OneDrive | Proposed | User-private ACL, not synced to the cloud, and never inside a watched root. |
