import { applyD1Migrations, env, fetchMock } from "cloudflare:test";
import { beforeAll, beforeEach, afterEach, describe, it, expect } from "vitest";
import worker, {
  now,
  maintenance,
  type Env,
  type Identity,
} from "../src/index";
import { databaseEnv } from "../src/database";
import { DatabaseBudget, databasePaused } from "../src/database-budget";
import { dispatchUpdate } from "../src/updates";
import { runJob, completeParent, tasks } from "../src/downloads";
import { saveGroups, saveNovelGroups } from "../src/accounts";
import { bookById, novels, groups, canAccess } from "../src/library";
const bindings = env as unknown as Env & {
  TEST_MIGRATIONS: Parameters<typeof applyD1Migrations>[1];
};
const sent: any[] = [];
const local = {
  ...bindings,
  APPLICATION: {
    idFromName: () => "scheduler",
    get: () => ({ fetch: async () => new Response("{}") }),
  },
  DOWNLOADS: {
    send: async (body: any) => {
      sent.push(body);
    },
    sendBatch: async (messages: any[]) => {
      sent.push(...messages.map((m) => m.body));
    },
  },
} as unknown as Env;
const user = {
  user_id: "reg-user",
  username: "reg-user",
  role: "user",
  can_download: 1,
  can_view_adult: 0,
  csrf: "",
  token_hash: "",
} as Identity;
beforeAll(async () => applyD1Migrations(bindings.DB, bindings.TEST_MIGRATIONS));
beforeEach(async () => {
  sent.length = 0;
  fetchMock.activate();
  fetchMock.disableNetConnect();
  for (const table of [
    "comic_jobs",
    "chapters",
    "comics",
    "books",
    "group_permissions",
    "novel_group_permissions",
    "users",
  ]) {
    const field =
      table === "chapters"
        ? "comic_id"
        : table.endsWith("permissions")
          ? "user_id"
          : table === "comic_jobs"
            ? "user_id"
            : "id";
    await bindings.DB.prepare(
      `DELETE FROM ${table} WHERE ${field} LIKE 'reg-%'`,
    ).run();
  }
  await bindings.DB.prepare(
    "DELETE FROM chapters WHERE comic_id IN (SELECT id FROM comics WHERE name LIKE 'reg-%')",
  ).run();
  await bindings.DB.prepare("DELETE FROM comics WHERE name LIKE 'reg-%'").run();
  await bindings.DB.prepare(
    "DELETE FROM library_groups WHERE name='reg-shared'",
  ).run();
  await bindings.DB.prepare(
    "INSERT INTO users(id,username,password_hash,role,can_download) VALUES ('reg-user','reg-user','x','user',1)",
  ).run();
});
afterEach(() => {
  fetchMock.assertNoPendingInterceptors();
  fetchMock.deactivate();
});
describe("review regression fixes", () => {
  it("routes update messages through the persisted Turso budget; exhausted requests make zero provider calls", async () => {
    const values = new Map<string, any>();
    const tx = {
      get: async (key: string) => values.get(key),
      put: async (key: string, value: any) => {
        values.set(key, value);
      },
    };
    const budget = new DatabaseBudget(
      { ...tx, transaction: async (fn: any) => fn(tx) },
      Date.now,
      { requests: 1, statements: 1 },
    );
    let outgoing = 0,
      acked = 0,
      retried = 0;
    const routes: string[] = [];
    fetchMock
      .get("https://reg-budget.turso.io")
      .intercept({ path: "/v2/pipeline", method: "POST" })
      .reply(
        200,
        () => {
          outgoing++;
          return JSON.stringify({
            baton: null,
            base_url: null,
            results: [
              {
                type: "ok",
                response: {
                  type: "execute",
                  result: {
                    cols: [],
                    rows: [],
                    affected_row_count: 0,
                    last_insert_rowid: null,
                  },
                },
              },
              { type: "ok", response: { type: "close" } },
            ],
          });
        },
        { headers: { "content-type": "application/json" } },
      );
    const metered = databaseEnv(
      {
        ...bindings,
        DATABASE_PROVIDER: "turso",
        TURSO_DATABASE_URL: "libsql://reg-budget.turso.io",
        TURSO_AUTH_TOKEN: "synthetic",
      },
      budget,
    ) as Env;
    const outer = {
      ...bindings,
      APPLICATION: {
        idFromName: (name: string) => {
          routes.push(name);
          return name;
        },
        get: () => ({
          fetch: async (request: string, init: any) => {
            expect(request).toBe("https://jobs.internal/update");
            try {
              await dispatchUpdate(metered, JSON.parse(init.body));
              return new Response("{}");
            } catch (error) {
              expect(databasePaused(error)).toBe(true);
              return new Response("{}", { status: 503 });
            }
          },
        }),
      },
    } as unknown as Env;
    for (let i = 0; i < 3; i++)
      await worker.queue(
        {
          messages: [
            {
              body: {
                kind: "update-page",
                generation: "nonexistent",
                userId: "reg-user",
              },
              attempts: 1,
              ack: () => acked++,
              retry: () => retried++,
            },
          ],
        } as any,
        outer,
      );
    expect({ outgoing, acked, retried, routes }).toEqual({
      outgoing: 1,
      acked: 3,
      retried: 0,
      routes: ["updates", "updates", "updates"],
    });
  });
  it("grants and revokes novel-only groups without changing same-name comic access", async () => {
    await bindings.DB.batch([
      bindings.DB.prepare(
        "INSERT INTO library_groups VALUES ('comic','reg-shared',1)",
      ),
      bindings.DB.prepare(
        "INSERT INTO library_groups VALUES ('novel','reg-shared',1)",
      ),
      bindings.DB.prepare(
        "INSERT INTO books(id,source_id,title,author,object_key,size,sha256,created_at,group_name) VALUES ('reg-book','reg-book','b','a','imports/reg.epub',1,'hash',1,'reg-shared')",
      ),
    ]);
    expect(await saveNovelGroups(local, user.user_id, ["reg-shared"])).toEqual([
      "reg-shared",
    ]);
    expect((await bookById(local, user, "reg-book")).id).toBe("reg-book");
    expect((await novels(local, user)).some((b) => b.id === "reg-book")).toBe(
      true,
    );
    expect(await groups(local, user, "novel")).toContain("reg-shared");
    expect(await canAccess(local, user, { group_name: "reg-shared" })).toBe(
      false,
    );
    await saveGroups(local, user.user_id, ["reg-shared"]);
    await saveNovelGroups(local, user.user_id, []);
    await expect(bookById(local, user, "reg-book")).rejects.toMatchObject({
      status: 403,
    });
    expect(await canAccess(local, user, { group_name: "reg-shared" })).toBe(
      true,
    );
  });
  it("uses constant-cost triggers for ready chapters, moves and deletion", async () => {
    await bindings.DB.batch(
      ["reg-big", "reg-other"].map((id) =>
        bindings.DB.prepare(
          "INSERT INTO comics(id,name,provider,source_url,created_at,updated_at) VALUES (?,?,'import','',1,1)",
        ).bind(id, id),
      ),
    );
    await bindings.DB.batch(
      Array.from({ length: 500 }, (_, i) =>
        bindings.DB.prepare(
          "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,state) VALUES (?,'reg-big',?,'t',?,'ready')",
        ).bind("reg-ch-" + i, i, "reg-src-" + i),
      ),
    );
    const changed = await bindings.DB.prepare(
      "UPDATE chapters SET state='converting' WHERE id='reg-ch-0'",
    ).run();
    expect(Number(changed.meta.rows_read)).toBeLessThan(20);
    await bindings.DB.prepare(
      "UPDATE chapters SET state='ready',comic_id='reg-other' WHERE id='reg-ch-0'",
    ).run();
    expect(
      await bindings.DB.prepare(
        "SELECT chapter_count FROM comics WHERE id='reg-big'",
      ).first("chapter_count"),
    ).toBe(499);
    expect(
      await bindings.DB.prepare(
        "SELECT chapter_count FROM comics WHERE id='reg-other'",
      ).first("chapter_count"),
    ).toBe(1);
    await bindings.DB.prepare("DELETE FROM chapters WHERE id='reg-ch-0'").run();
    expect(
      await bindings.DB.prepare(
        "SELECT chapter_count FROM comics WHERE id='reg-other'",
      ).first("chapter_count"),
    ).toBe(0);
  });
  async function startBook() {
    await bindings.DB.prepare(
      "UPDATE users SET role='admin' WHERE id='reg-user'",
    ).run();
    const source = {
      title: "reg-book",
      provider: "manhuagui",
      description: "",
      cover: null,
      chapters: Array.from({ length: 45 }, (_, i) => ({
        id: String(i + 1),
        order: i,
        title: String(i + 1),
      })),
    };
    await bindings.LIBRARY.put("fixture/reg-source", JSON.stringify(source));
    await bindings.DB.prepare(
      "INSERT INTO comic_jobs(id,user_id,kind,payload,created_at,updated_at) VALUES ('reg-root','reg-user','source',?,?,?)",
    )
      .bind(
        JSON.stringify({
          url: "https://www.manhuagui.com/comic/12345/",
          provider: "manhuagui",
          source_key: "fixture/reg-source",
          group: "默认分组",
          format: 2,
          max_chapters: 0,
        }),
        now(),
        now(),
      )
      .run();
  }
  async function finishBatch() {
    await bindings.DB.prepare(
      "UPDATE chapters SET state='ready' WHERE id IN (SELECT json_extract(payload,'$.chapter_id') FROM comic_jobs WHERE parent_id='reg-root')",
    ).run();
    await bindings.DB.prepare(
      "UPDATE comic_jobs SET state='completed' WHERE parent_id='reg-root'",
    ).run();
    await completeParent(local, "reg-root");
  }
  it("downloads the complete 45-chapter directory in 20/20/5 batches with idempotent continuation", async () => {
    await startBook();
    for (const total of [20, 40, 45]) {
      await runJob(local, "reg-root");
      const jobs = await bindings.DB.prepare(
        "SELECT id FROM comic_jobs WHERE parent_id='reg-root'",
      ).all();
      expect(jobs.results).toHaveLength(total);
      const progress = (
        await tasks(local, { ...user, role: "admin" }, "reg-root")
      )[0];
      expect(progress.total_chapters).toBe(45);
      expect(progress.progress_percent).toBeLessThan(100);
      await finishBatch();
    }
    expect(
      await bindings.DB.prepare(
        "SELECT state FROM comic_jobs WHERE id='reg-root'",
      ).first("state"),
    ).toBe("completed");
    expect(
      await bindings.DB.prepare(
        "SELECT chapter_count FROM comics WHERE name='reg-book'",
      ).first("chapter_count"),
    ).toBe(45);
    await completeParent(local, "reg-root");
    await runJob(local, "reg-root");
    expect(
      (
        await bindings.DB.prepare(
          "SELECT id FROM comic_jobs WHERE parent_id='reg-root'",
        ).all()
      ).results,
    ).toHaveLength(45);
  });
  it("does not continue the next batch after cancellation", async () => {
    await startBook();
    await runJob(local, "reg-root");
    await bindings.DB.prepare(
      "UPDATE comic_jobs SET state='cancelled' WHERE id='reg-root'",
    ).run();
    sent.length = 0;
    await finishBatch();
    await runJob(local, "reg-root");
    expect(sent).toHaveLength(0);
    expect(
      (
        await bindings.DB.prepare(
          "SELECT id FROM comic_jobs WHERE parent_id='reg-root'",
        ).all()
      ).results,
    ).toHaveLength(20);
  });
  it("recovers a committed last chapter when the parent completion callback is lost", async () => {
    await startBook();
    await runJob(local, "reg-root");
    await bindings.DB.prepare(
      "UPDATE comic_jobs SET state='completed' WHERE parent_id='reg-root'",
    ).run();
    await bindings.DB.prepare(
      "UPDATE comic_jobs SET updated_at=0 WHERE id='reg-root'",
    ).run();
    sent.length = 0;
    await maintenance(local);
    expect(
      await bindings.DB.prepare(
        "SELECT state FROM comic_jobs WHERE id='reg-root'",
      ).first("state"),
    ).toBe("queued");
    expect(
      sent.some((body) => body.kind === "comic" && body.id === "reg-root"),
    ).toBe(true);
    await runJob(local, "reg-root");
    expect(
      (
        await bindings.DB.prepare(
          "SELECT id FROM comic_jobs WHERE parent_id='reg-root'",
        ).all()
      ).results,
    ).toHaveLength(40);
  });
});
