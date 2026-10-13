// Included from worker.test.ts (one vitest file per queue consumer).
import { env, fetchMock } from "cloudflare:test";
import { beforeEach, describe, it, expect } from "vitest";
import { runJob, MAX_JOB_ATTEMPTS } from "../src/downloads";
import { maintenance, type Env } from "../src/index";
const db = (env as unknown as Env).DB;
const t = () => Math.floor(Date.now() / 1000);
describe("download job loop / D1 row usage", () => {
  beforeEach(async () => {
    fetchMock.activate();
    fetchMock.disableNetConnect();
    await db.batch(
      ["comic_jobs", "chapters", "comics"].map((x) =>
        db.prepare(`DELETE FROM ${x}`),
      ),
    );
    await db
      .prepare(
        "INSERT OR IGNORE INTO users(id,username,password_hash,role,can_download) VALUES ('admin','admin','x','admin',1)",
      )
      .run();
    await db
      .prepare(
        "INSERT INTO comics(id,name,provider,source_url,created_at,updated_at) VALUES ('c1','C','manhuagui','https://example.com/c',0,0)",
      )
      .run();
    // A realistically large comic_jobs table: 2,000 page jobs of other chapters.
    const rows = [];
    for (let i = 0; i < 2000; i++)
      rows.push(
        db
          .prepare(
            "INSERT INTO comic_jobs(id,user_id,comic_id,parent_id,kind,payload,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
          )
          .bind(
            `old${i}`,
            "admin",
            "c1",
            `p${i % 40}`,
            "page",
            JSON.stringify({ page: i }),
            "completed",
            0,
            0,
          ),
      );
    await db.batch(rows);
  });
  it("parent/child lookups use indexes instead of scanning comic_jobs", async () => {
    const queries: [string, unknown[]][] = [
      [
        "SELECT COALESCE(SUM(state NOT IN ('completed','cancelled','error')),0) AS n,COALESCE(SUM(state='error'),0) AS failed FROM comic_jobs WHERE parent_id=?",
        ["p1"],
      ],
      [
        "SELECT json_extract(payload,'$.page') AS page FROM comic_jobs WHERE parent_id=?",
        ["p1"],
      ],
      [
        "SELECT 1 FROM comic_jobs WHERE parent_id=? AND state!='completed' LIMIT 1",
        ["p1"],
      ],
      ["SELECT SUM(size) AS bytes FROM comic_jobs WHERE parent_id=?", ["p1"]],
      [
        "SELECT id FROM comic_jobs WHERE state IN ('queued','running') AND lease_until<? AND updated_at<? LIMIT 20",
        [t(), t()],
      ],
      ["SELECT * FROM comics WHERE source_url=?", ["https://example.com/c"]],
    ];
    for (const [sql, args] of queries) {
      const result = await db
        .prepare(sql)
        .bind(...args)
        .all();
      // 2,000-row table: a full scan would read >= 2,000 rows.
      expect(result.meta.rows_read, sql).toBeLessThan(200);
    }
  });
  it("a failing job backs off, is not re-claimed in a tight loop, and dies after MAX attempts", async () => {
    await db
      .prepare(
        "INSERT INTO comic_jobs(id,user_id,comic_id,parent_id,kind,payload,state,created_at,updated_at) VALUES ('pj','admin','c1',NULL,'chapter','{}','waiting',0,0)",
      )
      .run();
    await db
      .prepare(
        "INSERT INTO comic_jobs(id,user_id,comic_id,parent_id,kind,payload,created_at,updated_at) VALUES ('j','admin','c1','pj','page',?,?,?)",
      )
      .bind(
        JSON.stringify({
          key: "comics/c1/x",
          url: "https://img.example.com/1.jpg",
          referer: "https://example.com/c",
          page: 0,
        }),
        t(),
        t(),
      )
      .run();
    await runJob(env as unknown as Env, "j");
    let row = await db
      .prepare("SELECT * FROM comic_jobs WHERE id='j'")
      .first<any>();
    expect(row.state).toBe("queued");
    expect(row.attempts).toBe(1);
    expect(row.lease_until).toBeGreaterThanOrEqual(t() + 59);
    // Redelivery during the backoff window must be a no-op (no claim, no work).
    for (let i = 0; i < 10; i++) await runJob(env as unknown as Env, "j");
    row = await db
      .prepare("SELECT * FROM comic_jobs WHERE id='j'")
      .first<any>();
    expect(row.attempts).toBe(1);
    // Exhausted job: never claimed again, cron dead-letters it.
    await db
      .prepare(
        "UPDATE comic_jobs SET attempts=?,lease_until=0,updated_at=0 WHERE id='j'",
      )
      .bind(MAX_JOB_ATTEMPTS)
      .run();
    await runJob(env as unknown as Env, "j");
    await maintenance(env as unknown as Env);
    row = await db
      .prepare("SELECT * FROM comic_jobs WHERE id='j'")
      .first<any>();
    expect(row.attempts).toBe(MAX_JOB_ATTEMPTS);
    expect(row.state).toBe("error");
  });
  it("storage reservation reads stay bounded with 2,000 existing files", async () => {
    await db.batch(
      Array.from({ length: 2000 }, (_, i) =>
        db
          .prepare(
            "INSERT INTO storage_ledger(key,size,created_at) VALUES (?,1,0)",
          )
          .bind("quota-fixture-" + i),
      ),
    );
    const result = await db
      .prepare(
        "INSERT INTO storage_ledger(key,size,created_at) SELECT 'new-file',100,0 FROM storage_totals WHERE id=1 AND book_bytes+ledger_bytes-overlap_bytes+reservation_bytes+100<=256000000 RETURNING key",
      )
      .all();
    expect(result.results).toHaveLength(1);
    expect(result.meta.rows_read).toBeLessThan(20);
  });
  it("idle cron reads only a handful of rows", async () => {
    // Snapshot via D1 meta is per statement; measure by running the cron's queries.
    const before = Date.now();
    await maintenance(env as unknown as Env);
    expect(Date.now() - before).toBeLessThan(5000);
    const idle = await db
      .prepare(
        "UPDATE comic_jobs SET updated_at=? WHERE id IN (SELECT id FROM comic_jobs WHERE state IN ('queued','running') AND lease_until<? AND updated_at<? LIMIT 20) RETURNING id",
      )
      .bind(t(), t(), t() - 1800)
      .all();
    expect(idle.results.length).toBe(0);
    expect(idle.meta.rows_read).toBeLessThan(50);
  });
  it("completion checks seek unfinished states without recounting every finished sibling", async () => {
    await db
      .prepare(
        "INSERT INTO comic_jobs(id,user_id,comic_id,parent_id,kind,payload,state,created_at,updated_at) VALUES ('pending','admin','c1','p1','page','{}','waiting',0,0)",
      )
      .run();
    for (const states of [
      "'queued','submitting','running','waiting'",
      "'queued','submitting','running','waiting','cancelled','error'",
    ]) {
      const result = await db
        .prepare(
          `SELECT 1 FROM comic_jobs WHERE parent_id=? AND state IN (${states}) LIMIT 1`,
        )
        .bind("p1")
        .all();
      expect(result.results).toHaveLength(1);
      expect(result.meta.rows_read).toBeLessThan(10);
    }
  });
  it("cleanup, login history and legacy chapter filenames use their dedicated indexes", async () => {
    for (const [sql, index] of [
      [
        "SELECT key FROM login_attempts WHERE window<0",
        "login_attempts_window",
      ],
      [
        "SELECT * FROM login_logs WHERE username='admin' ORDER BY created_at DESC LIMIT 5",
        "login_logs_user_created",
      ],
      [
        "SELECT * FROM chapters WHERE comic_id='c1' AND legacy_filename='1.cbz' AND state='ready'",
        "chapters_legacy_filename",
      ],
    ]) {
      const result = await db.prepare("EXPLAIN QUERY PLAN " + sql).all<any>();
      expect(result.results.map((row) => row.detail).join(" ")).toContain(
        index,
      );
    }
  });
});
