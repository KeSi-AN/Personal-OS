-- 0010: rewrite a row's full-text entry only when its text changed (audit, 20 Nov).
-- The writer's upsert sets search_text on every checkpoint (an open window, a page visit),
-- so "AFTER UPDATE OF search_text" fired every 60 s per open interval and deleted and
-- re-inserted the same text: about 20x the work of the update itself. page_extracts' trigger
-- fired on any update at all.

DROP TRIGGER events_au;
CREATE TRIGGER events_au AFTER UPDATE OF search_text ON events
WHEN old.search_text IS NOT new.search_text BEGIN
    INSERT INTO events_fts (events_fts, rowid, search_text) VALUES ('delete', old.rowid, old.search_text);
    INSERT INTO events_fts (rowid, search_text) VALUES (new.rowid, new.search_text);
END;

DROP TRIGGER page_extracts_au;
CREATE TRIGGER page_extracts_au AFTER UPDATE ON page_extracts
WHEN old.title IS NOT new.title OR old.meta_description IS NOT new.meta_description
  OR old.headings IS NOT new.headings OR old.main_text IS NOT new.main_text BEGIN
    INSERT INTO page_extracts_fts (page_extracts_fts, rowid, title, meta_description, headings, main_text)
    VALUES ('delete', old.rowid, old.title, old.meta_description, old.headings, old.main_text);
    INSERT INTO page_extracts_fts (rowid, title, meta_description, headings, main_text)
    VALUES (new.rowid, new.title, new.meta_description, new.headings, new.main_text);
END;
