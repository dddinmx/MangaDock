-- Reservations survive crashes and prevent concurrent transfers from exceeding this library's budget.
ALTER TABLE jobs ADD COLUMN reserved_bytes INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX jobs_one_active_source ON jobs(source_id) WHERE state IN ('queued','submitting','running');
