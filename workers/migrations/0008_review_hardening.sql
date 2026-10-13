ALTER TABLE comic_jobs ADD COLUMN claim_token TEXT;
ALTER TABLE compute_jobs ADD COLUMN claim_token TEXT;
ALTER TABLE uploads ADD COLUMN compute_job_id TEXT;
ALTER TABLE uploads ADD COLUMN claim_token TEXT;
CREATE INDEX compute_jobs_parent ON compute_jobs(json_extract(payload,'$.parent_job'));
CREATE INDEX compute_jobs_comic ON compute_jobs(json_extract(payload,'$.comic_id'));
CREATE INDEX compute_jobs_media ON compute_jobs(json_extract(payload,'$.media_id'));
CREATE INDEX compute_jobs_chapter ON compute_jobs(json_extract(payload,'$.chapter_id'));
CREATE INDEX chapters_object ON chapters(object_key);
CREATE INDEX chapters_manifest ON chapters(manifest_key);
CREATE INDEX uploads_key_state ON uploads(object_key,state);
CREATE INDEX uploads_state_created ON uploads(state,created_at);
CREATE INDEX storage_reserved_created ON storage_ledger(state,created_at);
CREATE TABLE storage_gc (key TEXT PRIMARY KEY,prefix INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL);
CREATE INDEX storage_gc_created ON storage_gc(created_at);
-- In-flight older clients have no lease token; reclaim rather than accept their outputs.
UPDATE compute_jobs SET state='queued',lease_until=0 WHERE state='running';
UPDATE comic_jobs SET state='queued',lease_until=0 WHERE state='running';
ALTER TABLE storage_ledger ADD COLUMN owner TEXT;
UPDATE uploads SET state='aborted' WHERE state='uploading' AND id NOT IN (SELECT MIN(id) FROM uploads WHERE state='uploading' GROUP BY object_key);
CREATE UNIQUE INDEX uploads_one_active_key ON uploads(object_key) WHERE state='uploading';
UPDATE storage_ledger SET owner=(SELECT id FROM uploads WHERE object_key=storage_ledger.key AND state='uploading') WHERE state='reserved';
CREATE INDEX comics_cover ON comics(cover_key);
CREATE INDEX comics_hero ON comics(hero_key);
CREATE INDEX comics_mobile ON comics(mobile_hero_key);
CREATE INDEX comics_banner ON comics(banner_key);
CREATE INDEX books_cover ON books(cover_key);
CREATE INDEX books_hero ON books(hero_key);
CREATE INDEX books_mobile ON books(mobile_hero_key);
CREATE INDEX books_index ON books(index_key);
CREATE INDEX compute_jobs_input_key ON compute_jobs(input_key,state);
UPDATE uploads SET state='aborted' WHERE state='uploading' AND compute_job_id IS NULL AND object_key LIKE 'compute/%';
CREATE TRIGGER compute_cancel_gc AFTER UPDATE OF state ON compute_jobs
WHEN NEW.state IN ('cancelled','error') AND OLD.state NOT IN ('cancelled','error') BEGIN
  INSERT INTO storage_gc(key,prefix,created_at) VALUES ('compute/'||NEW.id||'/',1,unixepoch()) ON CONFLICT(key) DO NOTHING;
END;
CREATE INDEX comic_jobs_source_key ON comic_jobs(json_extract(payload,'$.source_key'));
-- One-time reconciliation for unreferenced conversion output from earlier deletions.
INSERT INTO storage_gc(key,prefix,created_at)
SELECT 'compute/'||id||'/',1,unixepoch() FROM compute_jobs WHERE state IN ('completed','cancelled','error')
ON CONFLICT(key) DO NOTHING;
