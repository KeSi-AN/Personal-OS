-- 0006: phase 7. Facts can also be remembered by voice (a CHECK can't be altered in SQLite,
-- so the table is rebuilt with its rows).

CREATE TABLE memory_facts_new (
    id         TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('cli', 'overlay', 'dashboard', 'phone', 'voice')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;
INSERT INTO memory_facts_new SELECT id, text, source, created_at, updated_at FROM memory_facts;
DROP TABLE memory_facts;
ALTER TABLE memory_facts_new RENAME TO memory_facts;
