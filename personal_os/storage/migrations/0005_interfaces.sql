-- 0005: phase 6 (DESIGN.md §6.4): remembered facts, reminders, rollups, paired phones.

-- Things you told the assistant to remember. Yours, editable, never inferred.
CREATE TABLE memory_facts (
    id         TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('cli', 'overlay', 'dashboard', 'phone')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;

CREATE TABLE reminders (
    id         TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    due_at     INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    fired_at   INTEGER,
    cancelled  INTEGER NOT NULL DEFAULT 0,
    action_id  TEXT                     -- the action that created it
) STRICT;
CREATE INDEX reminders_due ON reminders (due_at) WHERE fired_at IS NULL AND cancelled = 0;

-- Daily summaries and per-project current-state notes, written from session summaries.
CREATE TABLE rollups (
    id                 TEXT PRIMARY KEY,   -- 'day:2026-11-03' or 'project:<id>'
    kind               TEXT NOT NULL CHECK (kind IN ('day', 'project_state')),
    project_id         TEXT,
    ts_start           INTEGER NOT NULL,
    ts_end             INTEGER NOT NULL,
    input_hash         TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    attempts           INTEGER NOT NULL DEFAULT 1,
    model              TEXT NOT NULL,
    prompt_version     INTEGER NOT NULL,
    body               TEXT NOT NULL DEFAULT '{}',
    evidence_event_ids TEXT NOT NULL DEFAULT '[]',
    created_at         INTEGER NOT NULL
) STRICT;
CREATE INDEX rollups_kind ON rollups (kind, ts_start);

-- Phones paired for LAN access. Only a hash of each token is kept.
CREATE TABLE devices (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    last_seen  INTEGER,
    revoked    INTEGER NOT NULL DEFAULT 0
) STRICT;
