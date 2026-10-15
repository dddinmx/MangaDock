import test from "node:test";
import assert from "node:assert/strict";
import { createClient } from "@libsql/client";
import { clearDatabase, dropOrder } from "../scripts/clear-turso-database.mjs";
test("cleanup drops child tables first and keeps unrelated schema protected", () => {
  assert.deepEqual(
    dropOrder(
      ["users", "progress", "books"],
      new Map([["progress", ["users", "books"]]]),
    ),
    ["progress", "users", "books"],
  );
  assert.throws(
    () =>
      dropOrder(
        ["a", "b"],
        new Map([
          ["a", ["b"]],
          ["b", ["a"]],
        ]),
      ),
    /cycle/,
  );
});
test("full MangaDock cleanup removes real rows, triggers, views and schema with foreign keys enabled", async () => {
  const c = createClient({ url: "file::memory:" });
  try {
    await c.executeMultiple(
      "PRAGMA foreign_keys=ON; CREATE TABLE users(id PRIMARY KEY); CREATE TABLE books(id PRIMARY KEY); CREATE TABLE comics(id PRIMARY KEY); CREATE TABLE comic_jobs(id PRIMARY KEY,user_id REFERENCES users(id)); CREATE TABLE mangadock_migrations(name PRIMARY KEY); CREATE TABLE progress(user_id REFERENCES users(id),book_id REFERENCES books(id)); INSERT INTO users VALUES(1); INSERT INTO books VALUES(1); INSERT INTO progress VALUES(1,1); INSERT INTO comic_jobs VALUES(1,1); CREATE VIEW titles AS SELECT id FROM books; CREATE TRIGGER removed AFTER DELETE ON books BEGIN DELETE FROM progress WHERE book_id=old.id; END;",
    );
    const result = await clearDatabase(c);
    assert.equal(result.user_objects_remaining, 0);
    assert.equal(result.data_cleared, true);
    assert.deepEqual(
      (
        await c.execute(
          "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'",
        )
      ).rows,
      [],
    );
  } finally {
    c.close();
  }
});
test("cleanup refuses to change another application database", async () => {
  const c = createClient({ url: "file::memory:" });
  try {
    await c.execute("CREATE TABLE other_project(id)");
    await assert.rejects(clearDatabase(c), /fingerprint/);
    assert.equal(
      (
        await c.execute(
          "SELECT name FROM sqlite_schema WHERE name='other_project'",
        )
      ).rows.length,
      1,
    );
  } finally {
    c.close();
  }
});
