-- Library index for objects stored in R2 (replaces filesystem scanning of COMIC_ROOT).
CREATE TABLE IF NOT EXISTS cf_comic (
  name TEXT PRIMARY KEY,
  title TEXT,
  provider TEXT,
  source_url TEXT,
  cover_key TEXT,
  description TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS cf_chapter (
  comic_name TEXT NOT NULL,
  chapter_order INTEGER NOT NULL,
  filename_base TEXT,
  title TEXT,
  page_count INTEGER NOT NULL DEFAULT 0,
  page_keys TEXT NOT NULL DEFAULT '[]',
  pages_done INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ready',
  task_id TEXT,
  PRIMARY KEY (comic_name, chapter_order)
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_reading_progress_user_comic ON reading_progress (user_id, comic_name);
