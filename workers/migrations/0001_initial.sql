PRAGMA foreign_keys = ON;
CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','user')) DEFAULT 'user');
CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), csrf TEXT NOT NULL, expires_at INTEGER NOT NULL);
CREATE INDEX sessions_expiry ON sessions(expires_at);
CREATE TABLE login_attempts (key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, window INTEGER NOT NULL);
CREATE TABLE books (id TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL, author TEXT NOT NULL, object_key TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE jobs (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), source_id TEXT NOT NULL, chapter_ids TEXT NOT NULL DEFAULT '[]', remote_id TEXT, state TEXT NOT NULL DEFAULT 'queued', message TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0, lease_until INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE INDEX jobs_pending ON jobs(state, updated_at);
CREATE TABLE progress (user_id TEXT NOT NULL REFERENCES users(id), book_id TEXT NOT NULL REFERENCES books(id), chapter INTEGER NOT NULL DEFAULT 0, page INTEGER NOT NULL DEFAULT 0, scroll INTEGER NOT NULL DEFAULT 0, client_ms INTEGER NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY (user_id, book_id));
