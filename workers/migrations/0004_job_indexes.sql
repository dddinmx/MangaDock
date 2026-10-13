-- Indexes for the comic download pipeline and hot listing queries.
-- Without comic_jobs(parent_id,...) every page/chapter job did several full scans of comic_jobs.
CREATE INDEX IF NOT EXISTS comic_jobs_parent ON comic_jobs(parent_id, state);
CREATE INDEX IF NOT EXISTS comic_jobs_kind_created ON comic_jobs(kind, created_at);
CREATE INDEX IF NOT EXISTS comic_jobs_lease ON comic_jobs(state, lease_until);
CREATE INDEX IF NOT EXISTS jobs_lease ON jobs(state, lease_until);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS comics_source_url ON comics(source_url);
CREATE INDEX IF NOT EXISTS comics_updated ON comics(updated_at);
CREATE INDEX IF NOT EXISTS compute_jobs_pending ON compute_jobs(state, created_at);
