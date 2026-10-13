import { applyD1Migrations, env } from "cloudflare:test";
import { beforeAll, describe, expect, it } from "vitest";
import { fencedEnv } from "../src/job-state";
import type { Env } from "../src/index";

const bindings = env as unknown as Env & {
  TEST_MIGRATIONS: Parameters<typeof applyD1Migrations>[1];
};

beforeAll(async () => {
  await applyD1Migrations(bindings.DB, bindings.TEST_MIGRATIONS);
});

const indexedCount =
  "UPDATE comics SET chapter_count=(SELECT COUNT(*) FROM chapters WHERE comic_id=? AND state='ready') WHERE id=?";
const indexedCountByChapter =
  "UPDATE comics SET chapter_count=(SELECT COUNT(*) FROM chapters WHERE comic_id=comics.id AND state='ready') WHERE id=(SELECT comic_id FROM chapters WHERE id=?)";

function capturePrepare() {
  const captured: string[] = [];
  const db = bindings.DB;
  const restricted = fencedEnv(
    {
      ...bindings,
      DB: {
        batch: db.batch.bind(db),
        prepare(sql: string) {
          captured.push(sql);
          return db.prepare(sql);
        },
      },
    } as Env,
    "comic_jobs",
    { id: "count-sql-job", claim_token: "tok", payload: "{}" },
  );
  return { captured, restricted };
}

describe("fenced chapter_count SQL", () => {
  it("injects the execution fence at the outer UPDATE WHERE", () => {
    const { captured, restricted } = capturePrepare();
    restricted.DB.prepare(indexedCount).bind("count-sql-c", "count-sql-c");
    expect(captured).toHaveLength(1);
    const guarded = captured[0];
    expect(guarded).toContain(
      "FROM chapters WHERE comic_id=? AND state='ready') WHERE (id=?)",
    );
    expect(guarded).toContain(
      "EXISTS(SELECT 1 FROM comic_jobs execution WHERE execution.id=?",
    );
    expect(guarded).not.toMatch(/FROM chapters WHERE \(/);
  });

  it("keeps subquery WHERE clauses intact when only chapter_id is bound", () => {
    const { captured, restricted } = capturePrepare();
    restricted.DB.prepare(indexedCountByChapter).bind("count-sql-ready");
    expect(captured).toHaveLength(1);
    const guarded = captured[0];
    expect(guarded).toContain(
      "FROM chapters WHERE comic_id=comics.id AND state='ready') WHERE (id=(SELECT comic_id FROM chapters WHERE id=?))",
    );
    expect(guarded).not.toMatch(/FROM chapters WHERE \(/);
  });

  it("does not treat WHERE inside quoted SET values as the UPDATE predicate", () => {
    const { captured, restricted } = capturePrepare();
    restricted.DB.prepare(
      "UPDATE comics SET name='has WHERE inside' WHERE id=?",
    ).bind("count-sql-c");
    expect(captured[0]).toContain("SET name='has WHERE inside' WHERE (id=?)");
  });

  it("recounts ready chapters under the fence using the indexed lookup", async () => {
    await bindings.DB.batch([
      bindings.DB.prepare(
        "INSERT OR IGNORE INTO users(id,username,password_hash,role) VALUES ('count-sql-user','count-sql-user','x','user')",
      ),
      bindings.DB.prepare(
        "INSERT OR IGNORE INTO comics(id,name,provider,source_url,created_at,updated_at) VALUES ('count-sql-c','count-sql-n','p','',1,1)",
      ),
      bindings.DB.prepare(
        "INSERT OR IGNORE INTO chapters(id,comic_id,ordinal,title,source_id,state) VALUES ('count-sql-ready','count-sql-c',0,'t','s-ready','ready')",
      ),
      bindings.DB.prepare(
        "INSERT OR IGNORE INTO chapters(id,comic_id,ordinal,title,source_id,state) VALUES ('count-sql-pending','count-sql-c',1,'t','s-pending','pending')",
      ),
      bindings.DB.prepare(
        "INSERT OR IGNORE INTO comic_jobs(id,user_id,kind,payload,state,claim_token,created_at,updated_at) VALUES ('count-sql-job','count-sql-user','source','{}','running','tok',1,1)",
      ),
    ]);
    await bindings.DB.prepare(
      "UPDATE comics SET chapter_count=99 WHERE id='count-sql-c'",
    ).run();
    const restricted = fencedEnv(bindings, "comic_jobs", {
      id: "count-sql-job",
      claim_token: "tok",
      payload: "{}",
    });
    await restricted.DB.prepare(indexedCount)
      .bind("count-sql-c", "count-sql-c")
      .run();
    expect(
      (
        await bindings.DB.prepare(
          "SELECT chapter_count AS n FROM comics WHERE id='count-sql-c'",
        ).first<{ n: number }>()
      )?.n,
    ).toBe(1);
    await bindings.DB.prepare(
      "UPDATE comics SET chapter_count=99 WHERE id='count-sql-c'",
    ).run();
    await restricted.DB.prepare(indexedCountByChapter)
      .bind("count-sql-ready")
      .run();
    expect(
      (
        await bindings.DB.prepare(
          "SELECT chapter_count AS n FROM comics WHERE id='count-sql-c'",
        ).first<{ n: number }>()
      )?.n,
    ).toBe(1);
    await bindings.DB.prepare(
      "UPDATE comic_jobs SET state='cancelled' WHERE id='count-sql-job'",
    ).run();
    await bindings.DB.prepare(
      "UPDATE comics SET chapter_count=99 WHERE id='count-sql-c'",
    ).run();
    await restricted.DB.prepare(indexedCount)
      .bind("count-sql-c", "count-sql-c")
      .run();
    expect(
      (
        await bindings.DB.prepare(
          "SELECT chapter_count AS n FROM comics WHERE id='count-sql-c'",
        ).first<{ n: number }>()
      )?.n,
    ).toBe(99);
  });

  it("has an index covering chapters(comic_id, state)", async () => {
    const indexes = await bindings.DB.prepare(
      "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='chapters' AND sql IS NOT NULL",
    ).all<{ sql: string }>();
    expect(
      indexes.results.some((row) =>
        /\(\s*comic_id\s*,\s*state\b/i.test(row.sql),
      ),
    ).toBe(true);
  });
});
