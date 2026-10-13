-- A crashed cloud conversion can be reclaimed at most three times.
ALTER TABLE compute_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
CREATE INDEX compute_jobs_attempts ON compute_jobs(state,attempts,lease_until,created_at);
