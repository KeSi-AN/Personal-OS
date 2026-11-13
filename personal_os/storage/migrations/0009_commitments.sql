-- 0009: commitments (docs/AGENT.md, phase 11). A to-do with a due time is something you said
-- you'd do; once that time passes, Donna says so once. nudged_at records when she did, so a
-- restart never repeats it.

ALTER TABLE project_items ADD COLUMN nudged_at INTEGER;
CREATE INDEX project_items_due ON project_items (due_at)
    WHERE kind = 'todo' AND done = 0 AND due_at IS NOT NULL AND nudged_at IS NULL;
