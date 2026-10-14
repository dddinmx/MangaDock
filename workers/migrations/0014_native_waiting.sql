-- Bound recovery to active native chapter waiters; parent/page lookups use existing indexes.
CREATE INDEX comic_native_waiting ON comic_jobs(updated_at,id)
WHERE kind='chapter' AND state='waiting' AND remote_poll_at IS NULL;
