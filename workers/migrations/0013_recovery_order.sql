-- Fair oldest-first recovery avoids queued image rows starving interrupted
-- running directory jobs, without scanning unrelated historical job rows.
CREATE INDEX comic_jobs_recovery_age ON comic_jobs(updated_at,id)
WHERE state IN ('queued','running') AND remote_poll_at IS NULL;
CREATE INDEX jobs_recovery_age ON jobs(updated_at,id)
WHERE state IN ('queued','running');
