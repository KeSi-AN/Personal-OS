-- 0003: LLM session summaries and embedding vectors (phase 4, DESIGN.md §6.2).

CREATE TABLE summaries (
    id                 TEXT PRIMARY KEY,         -- 'session:<session id>'
    kind               TEXT NOT NULL CHECK (kind IN ('session')),
    session_id         TEXT,
    project_id         TEXT,
    ts_start           INTEGER NOT NULL,
    ts_end             INTEGER NOT NULL,
    input_hash         TEXT NOT NULL,            -- hash of the evidence pack it was made from
    status             TEXT NOT NULL CHECK (status IN ('ok', 'failed')),
    attempts           INTEGER NOT NULL DEFAULT 1,
    model              TEXT NOT NULL,
    prompt_version     INTEGER NOT NULL,
    body               TEXT NOT NULL DEFAULT '{}', -- validated JSON (goal, actions, ...)
    search_text        TEXT NOT NULL DEFAULT '',
    evidence_event_ids TEXT NOT NULL DEFAULT '[]', -- JSON array of event ids the body cites
    created_at         INTEGER NOT NULL
) STRICT;
CREATE INDEX summaries_time ON summaries (ts_start);
CREATE INDEX summaries_project ON summaries (project_id, ts_start);

CREATE VIRTUAL TABLE summaries_fts USING fts5 (
    search_text,
    content = 'summaries',
    content_rowid = 'rowid',
    tokenize = 'unicode61 remove_diacritics 2'
);
INSERT INTO summaries_fts (summaries_fts, rank) VALUES ('secure-delete', 1);

CREATE TRIGGER summaries_ai AFTER INSERT ON summaries BEGIN
    INSERT INTO summaries_fts (rowid, search_text) VALUES (new.rowid, new.search_text);
END;
CREATE TRIGGER summaries_ad AFTER DELETE ON summaries BEGIN
    INSERT INTO summaries_fts (summaries_fts, rowid, search_text) VALUES ('delete', old.rowid, old.search_text);
    DELETE FROM embeddings WHERE item_kind = 'summary' AND item_id = old.id;
END;
CREATE TRIGGER summaries_au AFTER UPDATE OF search_text ON summaries BEGIN
    INSERT INTO summaries_fts (summaries_fts, rowid, search_text) VALUES ('delete', old.rowid, old.search_text);
    INSERT INTO summaries_fts (rowid, search_text) VALUES (new.rowid, new.search_text);
    DELETE FROM embeddings WHERE item_kind = 'summary' AND item_id = old.id;
END;

-- Unit-length float32 vectors, searched by brute force (ADR-044). No sqlite-vec: deleting a row
-- here is zeroed on disk by secure_delete, so forget and retention keep their guarantees.
CREATE TABLE embeddings (
    item_kind    TEXT NOT NULL CHECK (item_kind IN ('event', 'summary')),
    item_id      TEXT NOT NULL,
    model        TEXT NOT NULL,
    dims         INTEGER NOT NULL,
    content_hash TEXT NOT NULL,                   -- of the text that was embedded
    vector       BLOB NOT NULL,                   -- float32 little-endian
    ts           INTEGER NOT NULL,                -- the item's time, for range filters
    created_at   INTEGER NOT NULL,
    PRIMARY KEY (item_kind, item_id)
) STRICT;
CREATE INDEX embeddings_ts ON embeddings (ts);

-- A vector never outlives what it was made from.
CREATE TRIGGER embeddings_event_ad AFTER DELETE ON events BEGIN
    DELETE FROM embeddings WHERE item_kind = 'event' AND item_id = old.id;
END;
CREATE TRIGGER embeddings_event_private AFTER UPDATE OF privacy_class ON events
WHEN new.privacy_class != 'normal' BEGIN
    DELETE FROM embeddings WHERE item_kind = 'event' AND item_id = old.id;
END;
CREATE TRIGGER embeddings_extract_ad AFTER DELETE ON page_extracts BEGIN
    DELETE FROM embeddings WHERE item_kind = 'event' AND item_id = old.event_id;
END;
