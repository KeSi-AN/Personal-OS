-- 0001: events, page extracts, full-text indexes, counters, settings.
-- Timestamps are Unix epoch milliseconds (UTC).

CREATE TABLE events (
    id                 TEXT PRIMARY KEY,          -- UUIDv7, sorts by time
    source             TEXT NOT NULL,
    type               TEXT NOT NULL,
    ts_start           INTEGER NOT NULL,
    ts_end             INTEGER,                   -- NULL = interval still open
    last_seen          INTEGER,                   -- last checkpoint of an open interval
    tz_offset_min      INTEGER NOT NULL DEFAULT 0,
    project_hint       TEXT,
    project_id         TEXT,                      -- set by the resolver (phase 3)
    project_confidence REAL,
    confidence         REAL NOT NULL DEFAULT 1.0,
    privacy_class      TEXT NOT NULL CHECK (privacy_class IN ('normal', 'sensitive')),
    retention          TEXT NOT NULL,
    expires_at         INTEGER,
    observer_version   TEXT NOT NULL,
    truncated          INTEGER NOT NULL DEFAULT 0,
    payload            TEXT NOT NULL,             -- JSON, page extract removed
    search_text        TEXT NOT NULL DEFAULT '',
    inserted_at        INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL
) STRICT;

CREATE INDEX events_ts ON events (ts_start);
CREATE INDEX events_source_ts ON events (source, type, ts_start);
CREATE INDEX events_project_ts ON events (project_id, ts_start);
CREATE INDEX events_expires ON events (expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX events_open ON events (source) WHERE ts_end IS NULL;

CREATE VIRTUAL TABLE events_fts USING fts5 (
    search_text,
    content = 'events',
    content_rowid = 'rowid',
    tokenize = 'unicode61 remove_diacritics 2'
);
INSERT INTO events_fts (events_fts, rank) VALUES ('secure-delete', 1);

CREATE TRIGGER events_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts (rowid, search_text) VALUES (new.rowid, new.search_text);
END;
CREATE TRIGGER events_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts (events_fts, rowid, search_text) VALUES ('delete', old.rowid, old.search_text);
END;
CREATE TRIGGER events_au AFTER UPDATE OF search_text ON events BEGIN
    INSERT INTO events_fts (events_fts, rowid, search_text) VALUES ('delete', old.rowid, old.search_text);
    INSERT INTO events_fts (rowid, search_text) VALUES (new.rowid, new.search_text);
END;

CREATE TABLE page_extracts (
    id               TEXT PRIMARY KEY,
    event_id         TEXT NOT NULL UNIQUE REFERENCES events (id) ON DELETE CASCADE,
    ts               INTEGER NOT NULL,
    content_hash     TEXT NOT NULL DEFAULT '',
    title            TEXT NOT NULL DEFAULT '',
    meta_description TEXT NOT NULL DEFAULT '',
    headings         TEXT NOT NULL DEFAULT '[]',   -- JSON array
    main_text        TEXT NOT NULL DEFAULT '',
    word_count       INTEGER NOT NULL DEFAULT 0,
    lang             TEXT,
    expires_at       INTEGER
) STRICT;

CREATE INDEX page_extracts_hash ON page_extracts (content_hash);
CREATE INDEX page_extracts_expires ON page_extracts (expires_at) WHERE expires_at IS NOT NULL;

CREATE VIRTUAL TABLE page_extracts_fts USING fts5 (
    title, meta_description, headings, main_text,
    content = 'page_extracts',
    content_rowid = 'rowid',
    tokenize = 'unicode61 remove_diacritics 2'
);
INSERT INTO page_extracts_fts (page_extracts_fts, rank) VALUES ('secure-delete', 1);

CREATE TRIGGER page_extracts_ai AFTER INSERT ON page_extracts BEGIN
    INSERT INTO page_extracts_fts (rowid, title, meta_description, headings, main_text)
    VALUES (new.rowid, new.title, new.meta_description, new.headings, new.main_text);
END;
CREATE TRIGGER page_extracts_ad AFTER DELETE ON page_extracts BEGIN
    INSERT INTO page_extracts_fts (page_extracts_fts, rowid, title, meta_description, headings, main_text)
    VALUES ('delete', old.rowid, old.title, old.meta_description, old.headings, old.main_text);
END;
CREATE TRIGGER page_extracts_au AFTER UPDATE ON page_extracts BEGIN
    INSERT INTO page_extracts_fts (page_extracts_fts, rowid, title, meta_description, headings, main_text)
    VALUES ('delete', old.rowid, old.title, old.meta_description, old.headings, old.main_text);
    INSERT INTO page_extracts_fts (rowid, title, meta_description, headings, main_text)
    VALUES (new.rowid, new.title, new.meta_description, new.headings, new.main_text);
END;

-- Daily counters (blocked/dropped/redacted per source or reason). Counts only, never content.
CREATE TABLE counters (
    day    TEXT NOT NULL,      -- local date YYYY-MM-DD
    scope  TEXT NOT NULL,      -- e.g. 'filter.blocked', 'bus.dropped'
    name   TEXT NOT NULL,      -- reason or source
    value  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, scope, name)
) STRICT;

CREATE TABLE kv_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,   -- JSON
    updated_at INTEGER NOT NULL
) STRICT;
