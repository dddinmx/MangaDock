-- Separate media ACLs: retaining legacy novel access must never grant comic access.
CREATE TABLE novel_group_permissions (
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  group_name TEXT NOT NULL,
  PRIMARY KEY(user_id,group_name)
);
-- Before novel ACLs the existing shelf was visible to every existing account.
-- Preserve that access once, only for existing novel groups/books and accounts.
INSERT INTO novel_group_permissions(user_id,group_name)
SELECT u.id,g.name FROM users u CROSS JOIN (
  SELECT name FROM library_groups WHERE kind='novel'
  UNION SELECT DISTINCT group_name FROM books
) g WHERE u.role='user';

-- Counts are maintained in O(1), including moving a ready chapter to another comic.
DROP TRIGGER chapters_ready_gain;
DROP TRIGGER chapters_ready_lose;
CREATE TRIGGER chapters_ready_gain AFTER UPDATE OF state,comic_id ON chapters
WHEN NEW.state='ready' AND (OLD.state!='ready' OR OLD.comic_id!=NEW.comic_id) BEGIN
  UPDATE comics SET chapter_count=chapter_count+1 WHERE id=NEW.comic_id;
END;
CREATE TRIGGER chapters_ready_lose AFTER UPDATE OF state,comic_id ON chapters
WHEN OLD.state='ready' AND (NEW.state!='ready' OR OLD.comic_id!=NEW.comic_id) BEGIN
  UPDATE comics SET chapter_count=chapter_count-1 WHERE id=OLD.comic_id;
END;
CREATE INDEX compute_parent_active ON compute_jobs(json_extract(payload,'$.parent_job'),state);
CREATE INDEX comic_source_wait ON comic_jobs(kind,state,updated_at,id);
CREATE INDEX compute_input_state ON compute_jobs(kind,input_key,created_at DESC);
UPDATE compute_jobs SET state='cancelled',claim_token=NULL,lease_until=0
WHERE kind='index-epub' AND state IN ('queued','running')
AND rowid NOT IN (SELECT MIN(rowid) FROM compute_jobs WHERE kind='index-epub' AND state IN ('queued','running') GROUP BY input_key);
CREATE UNIQUE INDEX compute_index_active ON compute_jobs(kind,input_key)
WHERE kind='index-epub' AND state IN ('queued','running');

-- Task polling must not recount every completed chapter of large books.
ALTER TABLE comic_jobs ADD COLUMN total_chapters INTEGER NOT NULL DEFAULT 0;
ALTER TABLE comic_jobs ADD COLUMN completed_chapters INTEGER NOT NULL DEFAULT 0;
UPDATE comic_jobs SET
 total_chapters=(SELECT COUNT(*) FROM comic_jobs x WHERE x.parent_id=comic_jobs.id AND x.kind='chapter'),
 completed_chapters=(SELECT COUNT(*) FROM comic_jobs x WHERE x.parent_id=comic_jobs.id AND x.kind='chapter' AND x.state='completed')
WHERE kind='source';
CREATE TRIGGER comic_child_insert AFTER INSERT ON comic_jobs
WHEN NEW.kind='chapter' BEGIN
 UPDATE comic_jobs SET total_chapters=total_chapters+1,completed_chapters=completed_chapters+(NEW.state='completed') WHERE id=NEW.parent_id;
END;
CREATE TRIGGER comic_child_delete AFTER DELETE ON comic_jobs
WHEN OLD.kind='chapter' BEGIN
 UPDATE comic_jobs SET total_chapters=total_chapters-1,completed_chapters=completed_chapters-(OLD.state='completed') WHERE id=OLD.parent_id;
END;
CREATE TRIGGER comic_child_gain AFTER UPDATE OF state,parent_id ON comic_jobs
WHEN NEW.kind='chapter' AND NEW.state='completed' AND (OLD.state!='completed' OR OLD.parent_id IS NOT NEW.parent_id) BEGIN
 UPDATE comic_jobs SET completed_chapters=completed_chapters+1 WHERE id=NEW.parent_id;
END;
CREATE TRIGGER comic_child_lose AFTER UPDATE OF state,parent_id ON comic_jobs
WHEN OLD.kind='chapter' AND OLD.state='completed' AND (NEW.state!='completed' OR OLD.parent_id IS NOT NEW.parent_id) BEGIN
 UPDATE comic_jobs SET completed_chapters=completed_chapters-1 WHERE id=OLD.parent_id;
END;
CREATE TRIGGER comic_child_move AFTER UPDATE OF parent_id ON comic_jobs
WHEN NEW.kind='chapter' AND OLD.parent_id IS NOT NEW.parent_id BEGIN
 UPDATE comic_jobs SET total_chapters=total_chapters-1 WHERE id=OLD.parent_id;
 UPDATE comic_jobs SET total_chapters=total_chapters+1 WHERE id=NEW.parent_id;
END;

CREATE TABLE update_checks (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  state TEXT NOT NULL DEFAULT 'checking',
  automatic INTEGER NOT NULL DEFAULT 0,
  cursor TEXT NOT NULL DEFAULT '',
  paging_done INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  completed INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  finished_at INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX update_checks_active ON update_checks(user_id) WHERE state='checking';
CREATE INDEX update_checks_history ON update_checks(user_id,created_at DESC);
CREATE INDEX update_checks_recovery ON update_checks(state,updated_at);
CREATE INDEX update_checks_ready ON update_checks(id) WHERE state='checking' AND paging_done=1 AND completed+failed=total;
CREATE INDEX update_checks_expiry ON update_checks(state,created_at);
CREATE INDEX update_checks_retention ON update_checks(finished_at,id) WHERE state!='checking';
CREATE TABLE update_check_items (
  check_id TEXT NOT NULL REFERENCES update_checks(id) ON DELETE CASCADE,
  comic_id TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued',
  attempts INTEGER NOT NULL DEFAULT 0,
  claim_token TEXT,
  lease_until INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL,
  result TEXT,
  PRIMARY KEY(check_id,comic_id)
);
CREATE INDEX update_items_recovery ON update_check_items(state,lease_until,updated_at);
CREATE INDEX update_items_results ON update_check_items(check_id,state,comic_id);
CREATE INDEX update_items_candidates ON update_check_items(check_id,comic_id) WHERE result IS NOT NULL;
CREATE TRIGGER update_items_insert AFTER INSERT ON update_check_items BEGIN
  UPDATE update_checks SET total=total+1 WHERE id=NEW.check_id;
END;
CREATE TRIGGER update_items_finish AFTER UPDATE OF state ON update_check_items
WHEN OLD.state NOT IN ('completed','error') AND NEW.state IN ('completed','error') BEGIN
  UPDATE update_checks SET completed=completed+(NEW.state='completed'),failed=failed+(NEW.state='error'),updated_at=NEW.updated_at WHERE id=NEW.check_id;
END;
