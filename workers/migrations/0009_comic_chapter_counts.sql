-- Ready-chapter totals for library listings. Backfill once, then keep the
-- counter inside the same transaction as every chapters INSERT, DELETE, and
-- ready-state change (downloads, imports, scans, WebDAV, compute, deletion).
-- chapters_library (comic_id, state, ordinal) from 0003 already covers
-- COUNT(*) WHERE comic_id=? AND state='ready'; keep that lookup indexed.
CREATE INDEX IF NOT EXISTS chapters_library ON chapters(comic_id,state,ordinal);
ALTER TABLE comics ADD COLUMN chapter_count INTEGER NOT NULL DEFAULT 0;
UPDATE comics SET chapter_count=(
  SELECT COUNT(*) FROM chapters WHERE comic_id=comics.id AND state='ready'
);
CREATE TRIGGER chapters_ready_insert AFTER INSERT ON chapters
WHEN NEW.state='ready' BEGIN
  UPDATE comics SET chapter_count=chapter_count+1 WHERE id=NEW.comic_id;
END;
CREATE TRIGGER chapters_ready_delete AFTER DELETE ON chapters
WHEN OLD.state='ready' BEGIN
  UPDATE comics SET chapter_count=chapter_count-1 WHERE id=OLD.comic_id;
END;
CREATE TRIGGER chapters_ready_gain AFTER UPDATE OF state ON chapters
WHEN OLD.state!='ready' AND NEW.state='ready' BEGIN
  UPDATE comics SET chapter_count=chapter_count+1 WHERE id=NEW.comic_id;
END;
CREATE TRIGGER chapters_ready_lose AFTER UPDATE OF state ON chapters
WHEN OLD.state='ready' AND NEW.state!='ready' BEGIN
  UPDATE comics SET chapter_count=chapter_count-1 WHERE id=NEW.comic_id;
END;
