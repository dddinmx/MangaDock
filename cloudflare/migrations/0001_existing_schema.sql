-- Existing MangaDock schema, dumped from the production SQLite DB (SQLAlchemy-generated).
-- Kept column-for-column so SQLite->D1 data export can be INSERTed unchanged.
CREATE TABLE IF NOT EXISTS download_task (
	id VARCHAR(36) NOT NULL, 
	comic_name VARCHAR(255), 
	url VARCHAR(512), 
	status VARCHAR(20), 
	progress_percent INTEGER, 
	total_chapters INTEGER, 
	completed_chapters INTEGER, 
	comic_format INTEGER, 
	log TEXT, 
	start_time DATETIME, 
	end_time DATETIME, 
	created_at DATETIME, 
	is_update BOOLEAN, 
	"group" VARCHAR(255), allow_adult BOOLEAN DEFAULT 0, created_by_user_id INTEGER, worker_pid INTEGER, 
	PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS reading_progress (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	comic_name VARCHAR(255) NOT NULL, 
	last_chapter INTEGER, 
	last_page INTEGER, 
	scroll_position INTEGER, 
	anchor_paragraph INTEGER, 
	anchor_offset INTEGER, 
	total_chapters INTEGER, 
	total_pages INTEGER, 
	last_read_at DATETIME, 
	created_at DATETIME, client_progress_ms BIGINT, 
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_reading_progress_user_id ON reading_progress (user_id);
CREATE TABLE IF NOT EXISTS reading_time (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	comic_name VARCHAR(255) NOT NULL, 
	duration INTEGER NOT NULL, 
	duration_seconds INTEGER NOT NULL, 
	read_at DATETIME NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_reading_time_user_id ON reading_time (user_id);
CREATE TABLE IF NOT EXISTS reading_session_state (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	comic_name VARCHAR(255) NOT NULL, 
	session_key VARCHAR(128) NOT NULL, 
	last_reported_seconds INTEGER NOT NULL, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_reading_session_state UNIQUE (user_id, comic_name, session_key)
);
CREATE INDEX IF NOT EXISTS ix_reading_session_state_user_id ON reading_session_state (user_id);
CREATE TABLE IF NOT EXISTS comic_group (
	id INTEGER NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);
CREATE TABLE IF NOT EXISTS comic_group_membership (
	id INTEGER NOT NULL, 
	comic_name VARCHAR(255) NOT NULL, 
	group_name VARCHAR(255) NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (comic_name)
);
CREATE TABLE IF NOT EXISTS user_group_permission (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	group_name VARCHAR(255) NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_user_group_permission_user_id ON user_group_permission (user_id);
CREATE TABLE IF NOT EXISTS admin_hidden_library_item (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	target_type VARCHAR(20) NOT NULL, 
	target_value VARCHAR(255) NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_admin_hidden_library_item UNIQUE (user_id, target_type, target_value)
);
CREATE INDEX IF NOT EXISTS ix_admin_hidden_library_item_user_id ON admin_hidden_library_item (user_id);
CREATE TABLE IF NOT EXISTS comic_scan_path (
	id INTEGER NOT NULL, 
	path VARCHAR(1024) NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (path)
);
CREATE TABLE IF NOT EXISTS comic_identity (
	id INTEGER NOT NULL, 
	comic_id VARCHAR(32) NOT NULL, 
	comic_name VARCHAR(255) NOT NULL, 
	description TEXT, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (comic_name)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_comic_identity_comic_id ON comic_identity (comic_id);
CREATE TABLE IF NOT EXISTS comic_update_check (
	id INTEGER NOT NULL, 
	comic_name VARCHAR(255) NOT NULL, 
	source_url VARCHAR(1024) NOT NULL, 
	has_updates BOOLEAN NOT NULL, 
	pending_chapters INTEGER NOT NULL, 
	remote_total_chapters INTEGER NOT NULL, 
	local_total_chapters INTEGER NOT NULL, 
	latest_chapter_title VARCHAR(255), 
	status VARCHAR(20) NOT NULL, 
	error_message VARCHAR(500), 
	last_checked_at DATETIME, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (comic_name)
);
CREATE TABLE IF NOT EXISTS background_command (
	id INTEGER NOT NULL, 
	command_type VARCHAR(64) NOT NULL, 
	payload TEXT, 
	status VARCHAR(20) NOT NULL, 
	requested_at DATETIME, 
	started_at DATETIME, 
	finished_at DATETIME, 
	message VARCHAR(500), 
	updated_at DATETIME, 
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_background_command_command_type ON background_command (command_type);
CREATE INDEX IF NOT EXISTS ix_background_command_status ON background_command (status);
CREATE TABLE IF NOT EXISTS app_setting (
	"key" VARCHAR(128) NOT NULL, 
	value VARCHAR(500) NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY ("key")
);
CREATE TABLE IF NOT EXISTS user (
	id INTEGER NOT NULL, 
	username VARCHAR(80) NOT NULL, 
	password_hash VARCHAR(200) NOT NULL, 
	role VARCHAR(20) NOT NULL, can_download BOOLEAN DEFAULT 0, can_view_adult BOOLEAN DEFAULT 0, session_version INTEGER NOT NULL DEFAULT 1, 
	PRIMARY KEY (id), 
	UNIQUE (username)
);
CREATE TABLE IF NOT EXISTS login_log (
	id INTEGER NOT NULL, 
	username VARCHAR(80) NOT NULL, 
	ip_address VARCHAR(45) NOT NULL, 
	user_agent VARCHAR(500), 
	login_time DATETIME, 
	success BOOLEAN NOT NULL, 
	message VARCHAR(200), 
	PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS novel_group (
	id INTEGER NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (name)
);
CREATE TABLE IF NOT EXISTS novel_group_membership (
	id INTEGER NOT NULL, 
	novel_id VARCHAR(255) NOT NULL, 
	group_name VARCHAR(255) NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (novel_id)
);
CREATE TABLE IF NOT EXISTS ani_list_account (
	user_id INTEGER NOT NULL, 
	anilist_user_id INTEGER NOT NULL, 
	username VARCHAR(80) NOT NULL, 
	encrypted_token TEXT NOT NULL, 
	PRIMARY KEY (user_id)
);
CREATE TABLE IF NOT EXISTS ani_list_comic_link (
	id INTEGER NOT NULL, 
	user_id INTEGER NOT NULL, 
	comic_name VARCHAR(255) NOT NULL, 
	media_id INTEGER NOT NULL, 
	media_title VARCHAR(255) NOT NULL, 
	first_chapter INTEGER NOT NULL, 
	synced_progress INTEGER NOT NULL, 
	completed_progress INTEGER NOT NULL, 
	last_sync_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_anilist_comic_link UNIQUE (user_id, comic_name)
);
CREATE INDEX IF NOT EXISTS ix_ani_list_comic_link_user_id ON ani_list_comic_link (user_id);
CREATE TABLE IF NOT EXISTS login_failure (
	failure_key VARCHAR(255) NOT NULL, 
	fail_count INTEGER NOT NULL, 
	last_failure_at DATETIME NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (failure_key)
);
