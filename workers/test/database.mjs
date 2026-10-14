import test from "node:test";
import assert from "node:assert/strict";
import { createClient } from "@libsql/client/node";
import { readFile, readdir } from "node:fs/promises";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { migrateTurso } from "../scripts/turso-migrate.mjs";
import { TursoDatabase, databaseEnv, tursoUrl } from "../src/database.ts";

async function fixture() {
  const client = createClient({ url: "file::memory:", intMode: "number" });
  for (const file of (await readdir(new URL("../migrations/", import.meta.url)))
    .filter((name) => name.endsWith(".sql"))
    .sort())
    await client.executeMultiple(
      await readFile(new URL("../migrations/" + file, import.meta.url), "utf8"),
    );
  return { client, db: new TursoDatabase(client) };
}
test("remote polling migration adopts only active Fanqie comics and due queries use the partial index on libSQL", async () => {
  const client = createClient({ url: "file::memory:", intMode: "number" });
  try {
    const directory = new URL("../migrations/", import.meta.url);
    for (const name of (await readdir(directory))
      .filter((name) => name.endsWith(".sql") && name < "0012")
      .sort())
      await client.executeMultiple(
        await readFile(new URL(name, directory), "utf8"),
      );
    await client.execute(
      "INSERT INTO users(id,username,password_hash) VALUES ('poll-user','poll-user','x')",
    );
    const future = Math.floor(Date.now() / 1000) + 3600;
    for (const [id, state, payload] of [
      ["active", "queued", { provider: "fanqie", remote_id: "remote" }],
      ["cancelled", "cancelled", { provider: "fanqie", remote_id: "remote" }],
      ["importing", "waiting", { provider: "fanqie", remote_id: "remote" }],
      ["native", "queued", { provider: "baozimh_org", remote_id: "remote" }],
      ["unsubmitted", "queued", { provider: "fanqie" }],
    ])
      await client.execute({
        sql: "INSERT INTO comic_jobs(id,user_id,kind,payload,state,lease_until,created_at,updated_at) VALUES (?,'poll-user','chapter',?,?,?,0,0)",
        args: [id, JSON.stringify(payload), state, future],
      });
    await client.executeMultiple(
      await readFile(new URL("0012_remote_polling.sql", directory), "utf8"),
    );
    const rows = (
      await client.execute(
        "SELECT id,remote_poll_at FROM comic_jobs ORDER BY id",
      )
    ).rows;
    assert.equal(rows.find((r) => r.id === "active").remote_poll_at, future);
    assert.ok(
      rows
        .filter((r) => r.id !== "active")
        .every((r) => r.remote_poll_at === null),
    );
    for (const query of [
      "SELECT id FROM comic_jobs WHERE remote_poll_at IS NOT NULL AND remote_poll_at<=0 ORDER BY remote_poll_at,id LIMIT 5",
      "SELECT remote_poll_at FROM comic_jobs WHERE remote_poll_at IS NOT NULL ORDER BY remote_poll_at,id LIMIT 1",
    ]) {
      const plan = (await client.execute("EXPLAIN QUERY PLAN " + query)).rows
        .map((r) => r.detail)
        .join(" ");
      assert.match(plan, /comic_jobs_remote_poll/);
      assert.doesNotMatch(plan, /TEMP B-TREE/);
    }
    await client.execute(
      "UPDATE comic_jobs SET state='cancelled' WHERE id='active'",
    );
    assert.equal(
      (
        await client.execute(
          "SELECT remote_poll_at FROM comic_jobs WHERE id='active'",
        )
      ).rows[0].remote_poll_at,
      null,
    );
  } finally {
    client.close();
  }
});
test("task timestamp migration preserves unknown history and tracks execution, finish and retry on libSQL", async () => {
  const client = createClient({ url: "file::memory:", intMode: "number" });
  try {
    const directory = new URL("../migrations/", import.meta.url);
    for (const name of (await readdir(directory))
      .filter((name) => name.endsWith(".sql") && name < "0011")
      .sort())
      await client.executeMultiple(
        await readFile(new URL(name, directory), "utf8"),
      );
    await client.execute(
      "INSERT INTO users(id,username,password_hash) VALUES ('timing','timing','hash')",
    );
    await client.execute(
      "INSERT INTO comic_jobs(id,user_id,kind,payload,state,created_at,updated_at) VALUES ('old','timing','source','{}','completed',100,200)",
    );
    await client.executeMultiple(
      await readFile(new URL("0011_task_progress.sql", directory), "utf8"),
    );
    const get = async (table, id) =>
      (
        await client.execute({
          sql: `SELECT started_at,finished_at FROM ${table} WHERE id=?`,
          args: [id],
        })
      ).rows[0];
    const old = await get("comic_jobs", "old");
    assert.equal(old.started_at, null);
    assert.equal(old.finished_at, 200);
    for (const table of ["comic_jobs", "jobs"]) {
      await client.execute(
        table === "comic_jobs"
          ? "INSERT INTO comic_jobs(id,user_id,kind,payload,created_at,updated_at) VALUES ('new','timing','source','{}',100,100)"
          : "INSERT INTO jobs(id,user_id,source_id,created_at,updated_at) VALUES ('new','timing','123456789',100,100)",
      );
      assert.equal((await get(table, "new")).started_at, null);
      await client.execute(
        `UPDATE ${table} SET state='running' WHERE id='new'`,
      );
      const start = (await get(table, "new")).started_at;
      assert.ok(start >= Math.floor(Date.now() / 1000) - 2);
      await client.execute(
        `UPDATE ${table} SET state='waiting' WHERE id='new'`,
      );
      assert.equal((await get(table, "new")).started_at, start);
      await client.execute(
        `UPDATE ${table} SET state='completed' WHERE id='new'`,
      );
      const finish = (await get(table, "new")).finished_at;
      assert.ok(finish >= start);
      await client.execute(
        `UPDATE ${table} SET state='completed',updated_at=999999 WHERE id='new'`,
      );
      assert.equal((await get(table, "new")).finished_at, finish);
      await client.execute(`UPDATE ${table} SET state='queued' WHERE id='new'`);
      assert.equal((await get(table, "new")).started_at, null);
      assert.equal((await get(table, "new")).finished_at, null);
    }
  } finally {
    client.close();
  }
});
test("all migrations and partial/json indexes work with actual libSQL", async () => {
  const { client, db } = await fixture();
  try {
    const result = await db
      .prepare("SELECT name FROM sqlite_master WHERE type='index'")
      .all();
    assert.ok(result.results.some((row) => row.name === "comic_jobs_parent"));
    assert.ok(
      result.results.some((row) => row.name === "chapters_legacy_filename"),
    );
    assert.equal(result.meta.rows_read, undefined);
    assert.equal(result.meta.rows_written, undefined);
  } finally {
    client.close();
  }
});
test("storage totals backfill and triggers match the original formula through overlaps, updates, deletes and rollback", async () => {
  const client = createClient({ url: "file::memory:" }),
    db = new TursoDatabase(client);
  try {
    const directory = new URL("../migrations/", import.meta.url);
    for (const file of (await readdir(directory))
      .filter((name) => name.endsWith(".sql") && !name.startsWith("0007"))
      .sort())
      await client.executeMultiple(
        await readFile(new URL(file, directory), "utf8"),
      );
    await client.execute(
      "INSERT INTO users(id,username,password_hash) VALUES ('u','user','hash')",
    );
    await client.execute(
      "INSERT INTO storage_ledger(key,size,created_at) VALUES ('a',100,0)",
    );
    await client.executeMultiple(
      await readFile(new URL("0007_storage_totals.sql", directory), "utf8"),
    );
    async function check() {
      const original = await db
        .prepare(
          "SELECT (SELECT COALESCE(SUM(size),0) FROM books WHERE object_key NOT LIKE 'webdav://%')+(SELECT COALESCE(SUM(size),0) FROM storage_ledger WHERE key NOT IN (SELECT object_key FROM books))+(SELECT COALESCE(SUM(reserved_bytes),0) FROM jobs) AS used",
        )
        .first("used");
      assert.equal(
        await db
          .prepare(
            "SELECT book_bytes+ledger_bytes-overlap_bytes+reservation_bytes AS used FROM storage_totals WHERE id=1",
          )
          .first("used"),
        original,
      );
    }
    await check();
    for (const sql of [
      "INSERT INTO books(id,source_id,title,author,object_key,size,sha256,created_at) VALUES ('b1','s1','book','author','a',100,'hash',0)",
      "INSERT INTO books(id,source_id,title,author,object_key,size,sha256,created_at) VALUES ('b2','s2','book','author','a',100,'hash',0)",
      "UPDATE storage_ledger SET size=200 WHERE key='a'",
      "UPDATE books SET object_key='b',size=80 WHERE id='b1'",
      "INSERT INTO storage_ledger(key,size,created_at) VALUES ('b',80,0)",
      "DELETE FROM books WHERE id='b2'",
      "UPDATE books SET object_key='webdav://mount/book',size=999 WHERE id='b1'",
      "DELETE FROM storage_ledger WHERE key='b'",
      "UPDATE storage_ledger SET key='webdav://mount/book' WHERE key='a'",
      "INSERT INTO jobs(id,user_id,source_id,reserved_bytes,created_at,updated_at) VALUES ('j','u','source',50,0,0)",
      "UPDATE jobs SET reserved_bytes=150 WHERE id='j'",
      "DELETE FROM jobs WHERE id='j'",
    ]) {
      await db.prepare(sql).run();
      await check();
    }
    await assert.rejects(
      db.batch([
        db.prepare(
          "INSERT INTO storage_ledger(key,size,created_at) VALUES ('rollback',777,0)",
        ),
        db.prepare("INSERT INTO missing VALUES (1)"),
      ]),
    );
    await check();
    assert.equal(
      await db
        .prepare("SELECT key FROM storage_ledger WHERE key='rollback'")
        .first(),
      null,
    );
  } finally {
    client.close();
  }
});
test("bound SQL, RETURNING and first(column) retain the current API contract", async () => {
  const { client, db } = await fixture();
  try {
    const base = db.prepare(
      "INSERT INTO settings(key,value) VALUES (?,?) RETURNING key,value",
    );
    const row = await base.bind("quoted", "O'Reilly").first();
    assert.deepEqual(row, { key: "quoted", value: "O'Reilly" });
    assert.equal(
      await db
        .prepare("SELECT value FROM settings WHERE key=?")
        .bind("quoted")
        .first("value"),
      "O'Reilly",
    );
    assert.equal(
      await db
        .prepare("SELECT value FROM settings WHERE key=?")
        .bind("missing")
        .first(),
      null,
    );
    await assert.rejects(
      db.prepare("SELECT value FROM settings").first("missing"),
    );
    assert.equal(
      (await base.bind("second", "value").all()).results[0].key,
      "second",
    );
  } finally {
    client.close();
  }
});
test("batch is atomic and rolls back earlier writes if a later statement fails", async () => {
  const { client, db } = await fixture();
  try {
    await assert.rejects(
      db.batch([
        db.prepare("INSERT INTO settings VALUES ('key','first')"),
        db.prepare("INSERT INTO settings VALUES ('key','duplicate')"),
      ]),
    );
    assert.equal(
      await db.prepare("SELECT value FROM settings WHERE key='key'").first(),
      null,
    );
    const rows = await db.batch([
      db.prepare("INSERT INTO settings VALUES ('key','new')"),
      db.prepare("SELECT value FROM settings WHERE key='key'"),
    ]);
    assert.equal(rows[1].results[0].value, "new");
  } finally {
    client.close();
  }
});
test("monotonic progress and cumulative reading-time deduplication survive the adapter", async () => {
  const { client, db } = await fixture();
  try {
    await db
      .prepare("INSERT INTO users VALUES ('u','user','hash','user',0,0)")
      .run();
    await db
      .prepare(
        "INSERT INTO comics(id,name,provider,source_url,created_at,updated_at) VALUES ('c','comic','import','',0,0)",
      )
      .run();
    const progress = db.prepare(
      "INSERT INTO comic_progress(user_id,comic_id,chapter,page,client_ms,updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(user_id,comic_id) DO UPDATE SET chapter=excluded.chapter,page=excluded.page,client_ms=excluded.client_ms WHERE excluded.client_ms>comic_progress.client_ms",
    );
    await progress.bind("u", "c", 2, 3, 200, 1).run();
    await progress.bind("u", "c", 0, 0, 100, 1).run();
    assert.equal(
      (
        await db
          .prepare("SELECT * FROM comic_progress WHERE user_id='u'")
          .first()
      ).page,
      3,
    );
    async function report(seconds) {
      await db.batch([
        db
          .prepare(
            "INSERT INTO reading_time(user_id,media_id,day,seconds) SELECT 'u','c','2026-01-01',MAX(0,?-COALESCE((SELECT seconds FROM reading_reports WHERE user_id='u' AND media_id='c' AND session_id='s'),0)) ON CONFLICT(user_id,media_id,day) DO UPDATE SET seconds=seconds+excluded.seconds",
          )
          .bind(seconds),
        db
          .prepare(
            "INSERT INTO reading_reports VALUES ('u','c','s',?) ON CONFLICT(user_id,media_id,session_id) DO UPDATE SET seconds=MAX(seconds,excluded.seconds)",
          )
          .bind(seconds),
      ]);
    }
    await report(60);
    await report(60);
    await report(120);
    assert.equal(
      await db.prepare("SELECT seconds FROM reading_time").first("seconds"),
      120,
    );
  } finally {
    client.close();
  }
});
test("binary and integers are normalized without libSQL numeric column aliases", async () => {
  const { client, db } = await fixture();
  try {
    const result = await db
      .prepare("SELECT ? AS bytes,? AS n,? AS allowed")
      .bind(new Uint8Array([0, 255]), 1700000000000, true)
      .all();
    assert.deepEqual(result.results, [
      { bytes: [0, 255], n: 1700000000000, allowed: 1 },
    ]);
    assert.throws(() => db.prepare("SELECT ?").bind(undefined));
    assert.throws(() =>
      db.prepare("SELECT ?").bind(Number.MAX_SAFE_INTEGER + 1),
    );
  } finally {
    client.close();
  }
});
test("provider selection fails closed and never falls back to D1 if Turso credentials are absent", () => {
  const d1 = { prepare() {}, batch() {} };
  assert.equal(databaseEnv({ DB: d1 }).DB, d1);
  assert.throws(
    () => databaseEnv({ DB: d1, DATABASE_PROVIDER: "turso" }),
    /尚未配置/,
  );
  assert.throws(
    () => databaseEnv({ DB: d1, DATABASE_PROVIDER: "typo" }),
    /无效/,
  );
  assert.equal(
    tursoUrl("libsql://mangadock-user.turso.io"),
    "https://mangadock-user.turso.io/",
  );
  for (const url of [
    "http://mangadock-user.turso.io",
    "https://example.com",
    "https://secret@name.turso.io",
    "https://name.turso.io?token=secret",
  ])
    assert.throws(() => tursoUrl(url));
});
test("the Workers HTTP driver uses the token, rejects redirects and decodes actual libSQL wire values", async () => {
  const original = globalThis.fetch;
  let calls = 0;
  try {
    globalThis.fetch = async (input, init) => {
      calls++;
      assert.equal(init.redirect, "manual");
      const request = new Request(input, init);
      assert.equal(
        request.headers.get("authorization"),
        "Bearer unit-test-token",
      );
      assert.equal(new URL(request.url).hostname, "fixture.turso.io");
      const body = await request.json();
      return Response.json({
        baton: null,
        base_url: null,
        results: body.requests.map((item) => ({
          type: "ok",
          response:
            item.type === "close"
              ? { type: "close" }
              : {
                  type: "execute",
                  result: {
                    cols: [{ name: "answer", decltype: "INTEGER" }],
                    rows: [[{ type: "integer", value: "42" }]],
                    affected_row_count: 0,
                    last_insert_rowid: null,
                  },
                },
        })),
      });
    };
    const { DB } = databaseEnv({
      DATABASE_PROVIDER: "turso",
      TURSO_DATABASE_URL: "libsql://fixture.turso.io",
      TURSO_AUTH_TOKEN: "unit-test-token",
    });
    assert.deepEqual(await DB.prepare("SELECT 42 AS answer").first(), {
      answer: 42,
    });
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = original;
  }
});
test("comic chapter_count is backfilled and updated with ready chapters", async () => {
  const client = createClient({ url: "file::memory:", intMode: "number" }),
    db = new TursoDatabase(client),
    directory = new URL("../migrations/", import.meta.url);
  try {
    for (const file of (await readdir(directory))
      .filter((name) => name.endsWith(".sql") && name < "0009")
      .sort())
      await client.executeMultiple(
        await readFile(new URL(file, directory), "utf8"),
      );
    await client.execute(
      "INSERT INTO comics(id,name,provider,source_url,created_at,updated_at) VALUES ('c','n','p','',0,0)",
    );
    await client.execute(
      "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,state) VALUES ('a','c',0,'a','a','ready'),('b','c',1,'b','b','pending')",
    );
    await client.executeMultiple(
      await readFile(
        new URL("0009_comic_chapter_counts.sql", directory),
        "utf8",
      ),
    );
    const count = () =>
      db
        .prepare("SELECT chapter_count AS n FROM comics WHERE id='c'")
        .first("n");
    assert.equal(await count(), 1);
    await db
      .prepare(
        "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,state) VALUES ('d','c',2,'d','d','ready')",
      )
      .run();
    assert.equal(await count(), 2);
    await db
      .prepare(
        "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,state) VALUES ('d','c',2,'renamed','d','ready') ON CONFLICT(id) DO UPDATE SET title=excluded.title",
      )
      .run();
    assert.equal(await count(), 2);
    await db.prepare("UPDATE chapters SET state='ready' WHERE id='b'").run();
    assert.equal(await count(), 3);
    await db
      .prepare("UPDATE chapters SET state='converting' WHERE id='a'")
      .run();
    assert.equal(await count(), 2);
    await db.prepare("DELETE FROM chapters WHERE id='d'").run();
    assert.equal(await count(), 1);
    await assert.rejects(
      db.batch([
        db.prepare(
          "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,state) VALUES ('e','c',3,'e','e','ready')",
        ),
        db.prepare("INSERT INTO missing VALUES (1)"),
      ]),
    );
    assert.equal(await count(), 1);
    await db.prepare("DELETE FROM chapters WHERE comic_id='c'").run();
    assert.equal(await count(), 0);
  } finally {
    client.close();
  }
});
test("migration initialization is repeatable and preserves the administrator password", async () => {
  const client = createClient({ url: "file::memory:" });
  try {
    const first = await migrateTurso(client, { password: "fixture-password" });
    assert.equal(first.applied.length, 14);
    assert.ok(first.applied.includes("0009_comic_chapter_counts.sql"));
    assert.ok(
      (await client.execute("PRAGMA table_info(comics)")).rows.some(
        (row) => row.name === "chapter_count",
      ),
    );
    const hash = (
      await client.execute("SELECT password_hash FROM users WHERE id='admin'")
    ).rows[0].password_hash;
    assert.match(hash, /^pbkdf2:sha256:600000\$/);
    const repeat = await migrateTurso(client, {
      password: "different-password",
    });
    assert.deepEqual(repeat.applied, []);
    assert.equal(
      (await client.execute("SELECT password_hash FROM users WHERE id='admin'"))
        .rows[0].password_hash,
      hash,
    );
    await assert.rejects(
      client.execute(
        "INSERT INTO sessions VALUES ('token','missing-user','csrf',123)",
      ),
    );
  } finally {
    client.close();
  }
});
test("a failed migration rolls back its schema changes and is not marked applied", async () => {
  const directory = await mkdtemp(join(tmpdir(), "md-turso-migration-")),
    client = createClient({ url: "file::memory:" });
  try {
    await writeFile(
      join(directory, "0001_bad.sql"),
      "CREATE TABLE should_rollback(id INTEGER); INSERT INTO missing VALUES (1);",
    );
    await assert.rejects(
      migrateTurso(client, {
        directory: pathToFileURL(directory + "/"),
        password: "fixture-password",
      }),
      /migration failed/,
    );
    assert.equal(
      (
        await client.execute(
          "SELECT name FROM sqlite_master WHERE name='should_rollback'",
        )
      ).rows.length,
      0,
    );
    assert.equal(
      (await client.execute("SELECT * FROM mangadock_migrations")).rows.length,
      0,
    );
  } finally {
    client.close();
    await rm(directory, { recursive: true, force: true });
  }
});

test("new ACL migration preserves old novel access without granting same-name comic access", async () => {
  const client = createClient({ url: "file::memory:" });
  try {
    const directory = new URL("../migrations/", import.meta.url);
    for (const name of (await readdir(directory))
      .filter((n) => n.endsWith(".sql") && n < "0010")
      .sort())
      await client.executeMultiple(
        await readFile(new URL(name, directory), "utf8"),
      );
    await client.executeMultiple(
      "INSERT INTO users(id,username,password_hash) VALUES ('reader','reader','hash'); INSERT INTO library_groups VALUES ('novel','private',1),('comic','private',1); INSERT INTO books(id,source_id,title,author,object_key,size,sha256,created_at,group_name) VALUES ('b','b','title','author','imports/b.epub',1,'hash',1,'private');",
    );
    await client.executeMultiple(
      await readFile(new URL("0010_review_regressions.sql", directory), "utf8"),
    );
    const novel = await client.execute(
      "SELECT group_name FROM novel_group_permissions WHERE user_id='reader' AND group_name='private'",
    );
    const comic = await client.execute(
      "SELECT group_name FROM group_permissions WHERE user_id='reader' AND group_name='private'",
    );
    assert.equal(novel.rows.length, 1);
    assert.equal(comic.rows.length, 0);
  } finally {
    client.close();
  }
});

test("audited cancellation repair restores only known ready children, remains cancelled and is idempotent", async () => {
  const { repairCancelled } = await import("../scripts/repair-cancelled.mjs");
  const { client } = await fixture();
  const id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
  try {
    await client.execute(
      "INSERT INTO users(id,username,password_hash) VALUES ('repair','repair','x')",
    );
    await client.execute(
      "INSERT INTO comics(id,name,provider,source_url,created_at,updated_at) VALUES ('repair','repair','import','',0,0)",
    );
    await client.execute(
      "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,state) VALUES ('repair-chapter','repair',0,'one','one','ready')",
    );
    await client.execute({
      sql: "INSERT INTO comic_jobs(id,user_id,comic_id,kind,payload,state,finished_at,created_at,updated_at) VALUES (?,'repair','repair','source','{}','cancelled',200,0,200)",
      args: [id],
    });
    await client.execute({
      sql: "INSERT INTO comic_jobs(id,user_id,parent_id,kind,payload,state,finished_at,created_at,updated_at) VALUES ('repair-child','repair',?,'chapter','{\"chapter_id\":\"repair-chapter\"}','cancelled',100,0,100)",
      args: [id],
    });
    await assert.rejects(
      repairCancelled(client, { id, expected: 2 }, true),
      /do not match/,
    );
    assert.equal(
      (await repairCancelled(client, { id, expected: 1 })).dry_run,
      true,
    );
    assert.equal(
      (await repairCancelled(client, { id, expected: 1 }, true)).restored,
      1,
    );
    assert.equal(
      (await repairCancelled(client, { id, expected: 1 }, true))
        .already_correct,
      true,
    );
    const root = (
      await client.execute({
        sql: "SELECT state,completed_chapters FROM comic_jobs WHERE id=?",
        args: [id],
      })
    ).rows[0];
    assert.equal(root.state, "cancelled");
    assert.equal(Number(root.completed_chapters), 1);
  } finally {
    client.close();
  }
});

test("stale native recovery prioritizes old directory jobs over queued pages using the age index", async () => {
  const { client } = await fixture();
  try {
    await client.execute(
      "INSERT INTO users(id,username,password_hash) VALUES ('recovery-user','recovery-user','x')",
    );
    await client.execute(
      "INSERT INTO comic_jobs(id,user_id,kind,payload,state,created_at,updated_at,lease_until) VALUES ('old-root','recovery-user','source','{}','running',0,1,0)",
    );
    for (let i = 0; i < 120; i++)
      await client.execute({
        sql: "INSERT INTO comic_jobs(id,user_id,kind,payload,state,created_at,updated_at,lease_until) VALUES (?,'recovery-user','page','{}','queued',0,2,0)",
        args: [`page-${i}`],
      });
    await client.execute(
      "INSERT INTO comic_jobs(id,user_id,kind,payload,state,created_at,updated_at,lease_until,remote_poll_at) VALUES ('remote','recovery-user','chapter','{}','queued',0,0,0,100)",
    );
    await client.execute(
      "INSERT INTO comic_jobs(id,user_id,kind,payload,state,created_at,updated_at,lease_until) VALUES ('live','recovery-user','page','{}','running',0,0,10000)",
    );
    const query =
      "SELECT id FROM comic_jobs INDEXED BY comic_jobs_recovery_age WHERE state IN ('queued','running') AND remote_poll_at IS NULL AND lease_until<100 AND updated_at<100 ORDER BY updated_at,id LIMIT 100";
    const rows = (await client.execute(query)).rows;
    assert.equal(rows.length, 100);
    assert.equal(rows[0].id, "old-root");
    assert.ok(rows.every((r) => r.id !== "remote" && r.id !== "live"));
    const plan = (await client.execute("EXPLAIN QUERY PLAN " + query)).rows
      .map((r) => r.detail)
      .join(" ");
    assert.match(plan, /comic_jobs_recovery_age/);
    assert.doesNotMatch(plan, /TEMP B-TREE/);
  } finally {
    client.close();
  }
});

test("native waiting recovery uses its partial age index without sorting historical jobs", async () => {
  const { client } = await fixture();
  try {
    const plan = (
      await client.execute(
        "EXPLAIN QUERY PLAN SELECT j.id FROM comic_jobs j INDEXED BY comic_native_waiting WHERE j.kind='chapter' AND j.state='waiting' AND j.remote_poll_at IS NULL AND j.updated_at<100 ORDER BY j.updated_at,j.id LIMIT 4",
      )
    ).rows
      .map((r) => r.detail)
      .join(" ");
    assert.match(plan, /comic_native_waiting/);
    assert.doesNotMatch(plan, /TEMP B-TREE/);
  } finally {
    client.close();
  }
});
