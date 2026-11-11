-- 0008: items Donna attaches herself (research she noticed) are marked 'auto'. A CHECK can't be
-- altered in SQLite, so the table is rebuilt with its rows and indexes.

CREATE TABLE project_items_new (
    id         TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects (id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('link', 'tab', 'file', 'note', 'todo')),
    title      TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    grp        TEXT NOT NULL DEFAULT '',
    done       INTEGER NOT NULL DEFAULT 0,
    due_at     INTEGER,
    source     TEXT NOT NULL CHECK (source IN ('voice', 'chat', 'overlay', 'dashboard', 'extension', 'group', 'cli', 'auto')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;
INSERT INTO project_items_new SELECT * FROM project_items;
DROP TABLE project_items;
ALTER TABLE project_items_new RENAME TO project_items;
CREATE INDEX project_items_by_project ON project_items (project_id, kind, created_at);
CREATE UNIQUE INDEX project_items_one_link ON project_items (project_id, url)
    WHERE url != '' AND kind IN ('link', 'tab', 'file');
