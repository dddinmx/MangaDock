-- Complement 0004's task indexes. Cleanup and file/user lookups must not scan
-- the complete tables, including when the library or login history grows.
CREATE INDEX IF NOT EXISTS login_attempts_window ON login_attempts(window);
CREATE INDEX IF NOT EXISTS login_logs_user_created ON login_logs(username,created_at DESC);
CREATE INDEX IF NOT EXISTS comic_jobs_creator_created ON comic_jobs(user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS comic_jobs_comic ON comic_jobs(comic_id);
CREATE INDEX IF NOT EXISTS comic_jobs_mount_state ON comic_jobs(json_extract(payload,'$.mount_id'),state) WHERE kind='webdav';
CREATE INDEX IF NOT EXISTS jobs_creator_created ON jobs(user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS chapters_legacy_filename ON chapters(comic_id,legacy_filename) WHERE legacy_filename IS NOT NULL;
CREATE INDEX IF NOT EXISTS reading_time_user_day ON reading_time(user_id,day);
CREATE INDEX IF NOT EXISTS comics_group_updated ON comics(group_name,updated_at DESC);
CREATE INDEX IF NOT EXISTS compute_jobs_lease ON compute_jobs(state,lease_until,created_at);
CREATE INDEX IF NOT EXISTS compute_jobs_kind_input_history ON compute_jobs(kind,input_key,state);
CREATE INDEX IF NOT EXISTS books_created ON books(created_at DESC);
