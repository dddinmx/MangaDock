-- Separate waiting for upstream archives from the Queue transport outbox.
ALTER TABLE comic_jobs ADD COLUMN remote_poll_at INTEGER;
CREATE INDEX comic_jobs_remote_poll ON comic_jobs(remote_poll_at,id)
WHERE remote_poll_at IS NOT NULL;
-- Adopt existing in-flight Fanqie comic polls without resubmitting upstream work.
UPDATE comic_jobs SET remote_poll_at=MAX(lease_until,unixepoch())
WHERE kind='chapter' AND state IN ('queued','running')
AND json_valid(payload) AND json_extract(payload,'$.provider')='fanqie'
AND json_extract(payload,'$.remote_id') IS NOT NULL;
CREATE TRIGGER comic_remote_poll_stop AFTER UPDATE OF state ON comic_jobs
WHEN NEW.state NOT IN ('queued','running') AND NEW.remote_poll_at IS NOT NULL
BEGIN
  UPDATE comic_jobs SET remote_poll_at=NULL WHERE id=NEW.id;
END;
