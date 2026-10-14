-- Lease expiry, not an unrelated thirty-minute age, controls crashed pages.
CREATE INDEX comic_page_recovery ON comic_jobs(state,lease_until,updated_at,id)
WHERE kind='page' AND remote_poll_at IS NULL;
