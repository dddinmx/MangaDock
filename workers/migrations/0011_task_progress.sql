-- Record actual execution times; queued time is not execution time.
ALTER TABLE comic_jobs ADD COLUMN started_at INTEGER;
ALTER TABLE comic_jobs ADD COLUMN finished_at INTEGER;
-- Earlier rows have no reliable start time. Retain only known terminal times.
UPDATE comic_jobs SET finished_at=updated_at WHERE state IN ('completed','error','cancelled');
CREATE TRIGGER comic_jobs_execution_times AFTER UPDATE OF state ON comic_jobs
BEGIN
  UPDATE comic_jobs SET
    started_at=CASE
      WHEN OLD.state IN ('completed','error','cancelled') AND NEW.state NOT IN ('completed','error','cancelled')
        THEN CASE WHEN NEW.state IN ('running','submitting') THEN unixepoch() ELSE NULL END
      WHEN NEW.state IN ('running','submitting') THEN COALESCE(OLD.started_at,unixepoch())
      ELSE OLD.started_at END,
    finished_at=CASE WHEN NEW.state IN ('completed','error','cancelled') THEN COALESCE(OLD.finished_at,unixepoch()) ELSE NULL END
  WHERE id=NEW.id;
END;
ALTER TABLE jobs ADD COLUMN started_at INTEGER;
ALTER TABLE jobs ADD COLUMN finished_at INTEGER;
-- Earlier rows have no reliable start time. Retain only known terminal times.
UPDATE jobs SET finished_at=updated_at WHERE state IN ('completed','error','cancelled');
CREATE TRIGGER jobs_execution_times AFTER UPDATE OF state ON jobs
BEGIN
  UPDATE jobs SET
    started_at=CASE
      WHEN OLD.state IN ('completed','error','cancelled') AND NEW.state NOT IN ('completed','error','cancelled')
        THEN CASE WHEN NEW.state IN ('running','submitting') THEN unixepoch() ELSE NULL END
      WHEN NEW.state IN ('running','submitting') THEN COALESCE(OLD.started_at,unixepoch())
      ELSE OLD.started_at END,
    finished_at=CASE WHEN NEW.state IN ('completed','error','cancelled') THEN COALESCE(OLD.finished_at,unixepoch()) ELSE NULL END
  WHERE id=NEW.id;
END;
