-- Abandoned streaming uploads must not block an image until the daily GC.
-- Zero denotes legacy reservations, which retain a ten-minute grace period.
ALTER TABLE storage_ledger ADD COLUMN lease_until INTEGER NOT NULL DEFAULT 0;
