-- Look up incomplete native chapter plans only under active source roots.
CREATE INDEX comic_native_plan ON comic_jobs(parent_id,updated_at,id)
WHERE kind='chapter' AND state IN ('waiting','completed') AND remote_poll_at IS NULL;
