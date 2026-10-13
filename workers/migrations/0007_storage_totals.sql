-- Maintain the exact original reservation formula without rescanning the
-- growing library for each downloaded image. Backfill once, then use triggers.
CREATE INDEX books_object_key ON books(object_key);
CREATE TABLE storage_totals (
  id INTEGER PRIMARY KEY CHECK(id=1),
  book_bytes INTEGER NOT NULL,
  ledger_bytes INTEGER NOT NULL,
  overlap_bytes INTEGER NOT NULL,
  reservation_bytes INTEGER NOT NULL
);
INSERT INTO storage_totals SELECT 1,
  (SELECT COALESCE(SUM(size),0) FROM books WHERE object_key NOT LIKE 'webdav://%'),
  (SELECT COALESCE(SUM(size),0) FROM storage_ledger),
  (SELECT COALESCE(SUM(size),0) FROM storage_ledger s WHERE EXISTS(SELECT 1 FROM books b WHERE b.object_key=s.key)),
  (SELECT COALESCE(SUM(reserved_bytes),0) FROM jobs);

CREATE TRIGGER storage_book_insert AFTER INSERT ON books BEGIN
  UPDATE storage_totals SET book_bytes=book_bytes+CASE WHEN NEW.object_key NOT LIKE 'webdav://%' THEN NEW.size ELSE 0 END,
    overlap_bytes=overlap_bytes+CASE WHEN NOT EXISTS(SELECT 1 FROM books WHERE object_key=NEW.object_key AND id!=NEW.id) THEN COALESCE((SELECT size FROM storage_ledger WHERE key=NEW.object_key),0) ELSE 0 END WHERE id=1;
END;
CREATE TRIGGER storage_book_delete AFTER DELETE ON books BEGIN
  UPDATE storage_totals SET book_bytes=book_bytes-CASE WHEN OLD.object_key NOT LIKE 'webdav://%' THEN OLD.size ELSE 0 END,
    overlap_bytes=overlap_bytes-CASE WHEN NOT EXISTS(SELECT 1 FROM books WHERE object_key=OLD.object_key) THEN COALESCE((SELECT size FROM storage_ledger WHERE key=OLD.object_key),0) ELSE 0 END WHERE id=1;
END;
CREATE TRIGGER storage_book_update AFTER UPDATE OF object_key,size ON books BEGIN
  UPDATE storage_totals SET book_bytes=book_bytes+CASE WHEN NEW.object_key NOT LIKE 'webdav://%' THEN NEW.size ELSE 0 END-CASE WHEN OLD.object_key NOT LIKE 'webdav://%' THEN OLD.size ELSE 0 END,
    overlap_bytes=overlap_bytes
    -CASE WHEN OLD.object_key!=NEW.object_key AND NOT EXISTS(SELECT 1 FROM books WHERE object_key=OLD.object_key) THEN COALESCE((SELECT size FROM storage_ledger WHERE key=OLD.object_key),0) ELSE 0 END
    +CASE WHEN OLD.object_key!=NEW.object_key AND NOT EXISTS(SELECT 1 FROM books WHERE object_key=NEW.object_key AND id!=NEW.id) THEN COALESCE((SELECT size FROM storage_ledger WHERE key=NEW.object_key),0) ELSE 0 END WHERE id=1;
END;
CREATE TRIGGER storage_ledger_insert AFTER INSERT ON storage_ledger BEGIN
  UPDATE storage_totals SET ledger_bytes=ledger_bytes+NEW.size,
    overlap_bytes=overlap_bytes+CASE WHEN EXISTS(SELECT 1 FROM books WHERE object_key=NEW.key) THEN NEW.size ELSE 0 END WHERE id=1;
END;
CREATE TRIGGER storage_ledger_delete AFTER DELETE ON storage_ledger BEGIN
  UPDATE storage_totals SET ledger_bytes=ledger_bytes-OLD.size,
    overlap_bytes=overlap_bytes-CASE WHEN EXISTS(SELECT 1 FROM books WHERE object_key=OLD.key) THEN OLD.size ELSE 0 END WHERE id=1;
END;
CREATE TRIGGER storage_ledger_update AFTER UPDATE OF key,size ON storage_ledger BEGIN
  UPDATE storage_totals SET ledger_bytes=ledger_bytes+NEW.size-OLD.size,
    overlap_bytes=overlap_bytes+CASE WHEN EXISTS(SELECT 1 FROM books WHERE object_key=NEW.key) THEN NEW.size ELSE 0 END-CASE WHEN EXISTS(SELECT 1 FROM books WHERE object_key=OLD.key) THEN OLD.size ELSE 0 END WHERE id=1;
END;
CREATE TRIGGER storage_reservation_insert AFTER INSERT ON jobs BEGIN
  UPDATE storage_totals SET reservation_bytes=reservation_bytes+NEW.reserved_bytes WHERE id=1;
END;
CREATE TRIGGER storage_reservation_delete AFTER DELETE ON jobs BEGIN
  UPDATE storage_totals SET reservation_bytes=reservation_bytes-OLD.reserved_bytes WHERE id=1;
END;
CREATE TRIGGER storage_reservation_update AFTER UPDATE OF reserved_bytes ON jobs BEGIN
  UPDATE storage_totals SET reservation_bytes=reservation_bytes+NEW.reserved_bytes-OLD.reserved_bytes WHERE id=1;
END;
