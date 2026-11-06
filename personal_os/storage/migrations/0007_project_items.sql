-- 0007: phase 9k. Things you attach to a project yourself: links, browser tabs (with the tab
-- group they came from), files, notes and to-dos. project_id NULL is the inbox (not filed yet).

CREATE TABLE project_items (
    id         TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects (id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('link', 'tab', 'file', 'note', 'todo')),
    title      TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',        -- http(s) address or file path
    body       TEXT NOT NULL DEFAULT '',        -- a note's text, or a source's note
    grp        TEXT NOT NULL DEFAULT '',        -- the browser tab group it came from
    done       INTEGER NOT NULL DEFAULT 0,      -- to-dos
    due_at     INTEGER,                         -- to-dos, optional
    source     TEXT NOT NULL CHECK (source IN ('voice', 'chat', 'overlay', 'dashboard', 'extension', 'group', 'cli')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;

CREATE INDEX project_items_by_project ON project_items (project_id, kind, created_at);
-- One link per project: adding it again updates it instead of duplicating it.
CREATE UNIQUE INDEX project_items_one_link ON project_items (project_id, url)
    WHERE url != '' AND kind IN ('link', 'tab', 'file');
