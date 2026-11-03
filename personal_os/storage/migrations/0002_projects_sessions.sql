-- 0002: projects, learned assignment rules, sessions; per-event assignment bookkeeping.

CREATE TABLE projects (
    id         TEXT PRIMARY KEY,                -- slug, e.g. 'auth-service'
    name       TEXT NOT NULL,
    roots      TEXT NOT NULL DEFAULT '[]',      -- JSON array of folder paths
    remotes    TEXT NOT NULL DEFAULT '[]',      -- JSON array of 'owner/repo'
    keywords   TEXT NOT NULL DEFAULT '[]',      -- JSON array, typed by you
    source     TEXT NOT NULL CHECK (source IN ('auto', 'manual', 'system')),
    archived   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
) STRICT;

INSERT INTO projects (id, name, source, created_at) VALUES ('none', 'Not project work', 'system', 0);

-- How each event got its project_id / project_confidence (columns from 0001).
ALTER TABLE events ADD COLUMN assign_source TEXT;    -- user|path|title|remote|rule|keyword|temporal|unassigned|n/a
ALTER TABLE events ADD COLUMN assigned_at INTEGER;   -- NULL = not looked at yet
CREATE INDEX events_pending_assign ON events (ts_start) WHERE assigned_at IS NULL;

-- Learned from your corrections: (kind, key) -> project, with a weight per agreeing correction.
CREATE TABLE assignment_rules (
    kind       TEXT NOT NULL,                   -- domain | github | path | app_title | app
    key        TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    weight     REAL NOT NULL DEFAULT 1,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (kind, key, project_id)
) STRICT;

CREATE TABLE sessions (
    id         TEXT PRIMARY KEY,
    ts_start   INTEGER NOT NULL,
    ts_end     INTEGER NOT NULL,
    project_id TEXT,                            -- NULL = unassigned
    confidence REAL NOT NULL,
    active_ms  INTEGER NOT NULL,
    stats      TEXT NOT NULL DEFAULT '{}',      -- JSON: apps, files, pages, commands, commits
    built_at   INTEGER NOT NULL
) STRICT;
CREATE INDEX sessions_ts ON sessions (ts_start);
