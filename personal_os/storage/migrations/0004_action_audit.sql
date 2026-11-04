-- 0004: the append-only action audit log (phase 5, DESIGN.md §6.3).
-- One row per stage of an action (proposed, auto/confirmed/declined/blocked, started,
-- succeeded/failed/cancelled, undone). Rows are never updated or deleted, except that
-- `forget` replaces the arguments and details in its window with a tombstone (ADR-011).

CREATE TABLE action_audit (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id TEXT NOT NULL,
    ts        INTEGER NOT NULL,
    stage     TEXT NOT NULL CHECK (stage IN (
        'proposed', 'auto', 'confirmed', 'declined', 'blocked',
        'started', 'succeeded', 'failed', 'cancelled', 'undone')),
    tool      TEXT NOT NULL,
    tier      INTEGER NOT NULL,
    origin    TEXT NOT NULL CHECK (origin IN ('user', 'planned', 'proactive')),
    tainted   INTEGER NOT NULL DEFAULT 0,
    request   TEXT,                       -- what you typed, for planned actions
    args      TEXT NOT NULL,              -- JSON
    detail    TEXT NOT NULL DEFAULT '',   -- reason, preview or result (truncated)
    undo      TEXT,                       -- JSON {tool, args} that reverses it
    undo_of   TEXT                        -- the action this one undoes
) STRICT;
CREATE INDEX action_audit_action ON action_audit (action_id, seq);
CREATE INDEX action_audit_ts ON action_audit (ts);

CREATE TRIGGER action_audit_no_delete BEFORE DELETE ON action_audit BEGIN
    SELECT RAISE(ABORT, 'action_audit is append-only');
END;

CREATE TRIGGER action_audit_no_update BEFORE UPDATE ON action_audit
WHEN NOT (
    new.args = '{"forgotten":true}' AND new.detail = '[forgotten]' AND new.request IS NULL
    AND new.undo IS NULL AND new.seq = old.seq AND new.action_id = old.action_id
    AND new.ts = old.ts AND new.stage = old.stage AND new.tool = old.tool
    AND new.tier = old.tier AND new.origin = old.origin AND new.tainted = old.tainted
    AND new.undo_of IS old.undo_of
) BEGIN
    SELECT RAISE(ABORT, 'action_audit is append-only');
END;
