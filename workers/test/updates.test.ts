import { applyD1Migrations, env, fetchMock } from "cloudflare:test";
import { afterEach, beforeAll, describe, expect, it } from "vitest";
import { schedulerRequest } from "../src/compute-scheduler";
import { cancelOrphanedCompute, computeApi } from "../src/compute";
import worker, {
  now,
  type Env,
  type Identity,
  type QueueBody,
} from "../src/index";
import { setting, setSetting } from "../src/library";
import {
  checkUpdates,
  dispatchUpdate,
  UPDATE_PAGE_BUDGET,
  type UpdateMessage,
} from "../src/updates";

const bindings = env as unknown as Env & {
  TEST_MIGRATIONS: Parameters<typeof applyD1Migrations>[1];
};
const sent: QueueBody[] = [];
const testEnv = {
  ...bindings,
  DOWNLOADS: {
    async send(body: QueueBody) {
      sent.push(body);
    },
    async sendBatch(messages: { body: QueueBody }[]) {
      for (const message of messages) sent.push(message.body);
    },
  },
} as Env;
const user: Identity = {
  user_id: "upd-user",
  username: "upd-user",
  role: "user",
  can_download: 1,
  can_view_adult: 0,
  csrf: "",
  token_hash: "",
};

beforeAll(async () => {
  await applyD1Migrations(bindings.DB, bindings.TEST_MIGRATIONS);
});

async function sql(statement: string, ...args: unknown[]) {
  await bindings.DB.prepare(statement)
    .bind(...args)
    .run();
}

async function cleanup() {
  await sql("DELETE FROM chapters WHERE comic_id LIKE 'upd-%'");
  await sql(
    "DELETE FROM comic_jobs WHERE user_id LIKE 'upd-%' OR id LIKE 'upd-%'",
  );
  await sql("DELETE FROM compute_jobs WHERE id LIKE 'upd-%'");
  await sql("DELETE FROM comics WHERE id LIKE 'upd-%'");
  await sql("DELETE FROM group_permissions WHERE user_id LIKE 'upd-%'");
  await sql("DELETE FROM library_groups WHERE name='upd-group'");
  await sql("DELETE FROM users WHERE id LIKE 'upd-%'");
  await sql(
    "DELETE FROM settings WHERE key IN ('update_candidates','update_check_generation','updates_checked_at')",
  );
}

function memoryStorage() {
  const values = new Map<string, any>();
  const api = {
    get: async (key: string) => values.get(key),
    put: async (key: string, value: unknown) => {
      values.set(key, value);
    },
    getAlarm: async () => null,
    setAlarm: async () => {},
    deleteAll: async () => values.clear(),
    transaction: async (fn: (tx: typeof api) => Promise<unknown>) => fn(api),
  };
  return api;
}

const schedulerEnv = {
  COMPUTE_WAKE_TOKEN: "wake-token",
  COMPUTE_REF: "b".repeat(40),
} as Env;

async function wake(storage: ReturnType<typeof memoryStorage>) {
  const response = await schedulerRequest(
    new Request("https://scheduler.internal/wake", {
      method: "POST",
      body: JSON.stringify({ at: now() }),
    }),
    schedulerEnv,
    storage as any,
  );
  expect(response?.status).toBe(200);
}

async function probe(storage: ReturnType<typeof memoryStorage>) {
  const response = await schedulerRequest(
    new Request("https://example.com/api/compute-ready", {
      headers: { Authorization: "Bearer wake-token" },
    }),
    schedulerEnv,
    storage as any,
  );
  expect(response).not.toBeNull();
  return (await response!.json()) as {
    ok: boolean;
    data: { pending: boolean; sequence?: number; limit?: number; ref?: string };
  };
}

async function finish(
  storage: ReturnType<typeof memoryStorage>,
  body: { sequence: number; pending: boolean; ok?: boolean },
) {
  const response = await schedulerRequest(
    new Request("https://example.com/api/compute-ready/finished", {
      method: "POST",
      headers: { Authorization: "Bearer wake-token" },
      body: JSON.stringify(body),
    }),
    schedulerEnv,
    storage as any,
  );
  expect(response?.status).toBe(200);
}

describe("compute wake quota", () => {
  it("holds a probe without spending a daily run until dispatch succeeds", async () => {
    const storage = memoryStorage();
    await wake(storage);
    const first = await probe(storage);
    expect(first.data).toMatchObject({ pending: true, sequence: 1, limit: 5 });
    expect((await storage.get("compute-wake")).runs).toBe(0);
    expect((await probe(storage)).data.pending).toBe(false);
    expect((await storage.get("compute-wake")).runs).toBe(0);
    await finish(storage, { sequence: first.data.sequence!, pending: true });
    const state = await storage.get("compute-wake");
    expect(state.runs).toBe(1);
    expect(state.pending).toBe(true);
    expect((await probe(storage)).data.pending).toBe(false);
  });

  it("refunds the hold when dispatch fails and does not count the run", async () => {
    const storage = memoryStorage();
    await wake(storage);
    const first = await probe(storage);
    await finish(storage, {
      sequence: first.data.sequence!,
      pending: true,
      ok: false,
    });
    const state = await storage.get("compute-wake");
    expect(state.runs).toBe(0);
    expect(state.next).toBe(0);
    expect(state.pending).toBe(true);
    const again = await probe(storage);
    expect(again.data.pending).toBe(true);
    expect((await storage.get("compute-wake")).runs).toBe(0);
  });

  it("still refuses a probe after 24 confirmed runs", async () => {
    const storage = memoryStorage();
    const day = new Date().toISOString().slice(0, 10);
    await storage.put("compute-wake", {
      sequence: 4,
      pending: true,
      day,
      runs: 24,
      next: 0,
    });
    expect((await probe(storage)).data.pending).toBe(false);
    expect((await storage.get("compute-wake")).runs).toBe(24);
  });
});

describe("paged update checks", () => {
  afterEach(async () => {
    fetchMock.assertNoPendingInterceptors();
    fetchMock.deactivate();
    await cleanup();
  });

  async function seedUser() {
    await cleanup();
    fetchMock.activate();
    fetchMock.disableNetConnect();
    sent.length = 0;
    await sql(
      "INSERT INTO users(id,username,password_hash,role,can_download) VALUES ('upd-user','upd-user','x','user',1)",
    );
    await sql(
      "INSERT INTO library_groups(kind,name,created_at) VALUES ('comic','upd-group',1)",
    );
    await sql(
      "INSERT INTO group_permissions(user_id,group_name) VALUES ('upd-user','upd-group')",
    );
  }

  async function addComic(
    id: string,
    updatedAt: number,
    sourceUrl: string,
    group = "upd-group",
  ) {
    await sql(
      "INSERT INTO comics(id,name,provider,source_url,group_name,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
      id,
      id,
      "test",
      sourceUrl,
      group,
      updatedAt,
      updatedAt,
    );
  }

  it("enqueues one cursor page and returns the previous candidate array", async () => {
    await seedUser();
    await addComic(
      "upd-live",
      5,
      "https://www.manhuagui.com/comic/12345/",
    );
    await setSetting(
      testEnv,
      "update_candidates",
      JSON.stringify([{ id: "upd-live", comic_name: "upd-live", pending_chapters: 3 }]),
    );
    const returned = await checkUpdates(testEnv, user);
    expect(returned).toEqual([
      { id: "upd-live", comic_name: "upd-live", pending_chapters: 3 },
    ]);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      kind: "update-page",
      cursor: "",
      budget: UPDATE_PAGE_BUDGET,
      userId: "upd-user",
      automatic: false,
    });
    expect(await setting(testEnv, "update_candidates", "")).toBe("[]");
    expect(sent[0].generation).toBe(
      await setting(testEnv, "update_check_generation", ""),
    );
  });

  it("pages a budget of comics, then continues from the cursor", async () => {
    await seedUser();
    await addComic("upd-z", 50, "https://example.com/z");
    await addComic("upd-m", 50, "https://example.com/m");
    await addComic("upd-c", 30, "https://example.com/c");
    await addComic("upd-b", 20, "https://example.com/b");
    await addComic("upd-a", 10, "https://example.com/a");
    await addComic("upd-hidden", 99, "https://example.com/hidden", "默认分组");
    const returned = await checkUpdates(testEnv, user);
    expect(Array.isArray(returned)).toBe(true);
    const generation = sent[0].generation!;
    const comicIds: string[] = [];
    let pages = [
      {
        kind: "update-page",
        cursor: "",
        budget: 2,
        generation,
        userId: "upd-user",
        automatic: false,
      } as UpdateMessage,
    ];
    let steps = 0;
    while (pages.length) {
      const next: UpdateMessage[] = [];
      for (const page of pages) {
        let acked = 0;
        let retried = 0;
        sent.length = 0;
        await worker.queue(
          {
            messages: [
              {
                body: page,
                attempts: 1,
                ack() {
                  acked++;
                },
                retry() {
                  retried++;
                },
              },
            ],
          } as any,
          testEnv,
        );
        expect(acked).toBe(1);
        expect(retried).toBe(0);
        for (const body of sent) {
          if (body.kind === "update-comic") comicIds.push(String(body.comicId));
          if (body.kind === "update-page") next.push(body);
        }
        if (next.length)
          expect(sent.at(-1)?.kind).toBe("update-page");
      }
      pages = next;
      steps++;
      expect(steps).toBeLessThan(6);
    }
    expect(comicIds).toEqual(["upd-z", "upd-m", "upd-c", "upd-b", "upd-a"]);
    expect(Number(await setting(testEnv, "updates_checked_at", "0"))).toBeGreaterThan(
      0,
    );
  });

  it("checks a single comic and keeps the candidate fields", async () => {
    await seedUser();
    const source = "https://www.manhuagui.com/comic/12345/";
    await addComic("upd-ok", 3, source);
    await addComic("upd-bad", 2, "https://example.com/not-a-comic");
    await sql(
      "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,state) VALUES ('upd-ch','upd-ok',1,'第一话',?,'ready')",
      "https://www.manhuagui.com/comic/12345/1.html",
    );
    fetchMock
      .get("https://www.manhuagui.com")
      .intercept({ path: "/comic/12345/" })
      .reply(
        200,
        "<html><head><title>更新测试</title></head><body><h1>更新测试</h1><a href=\"/comic/12345/1.html\">第一话</a><a href=\"/comic/12345/2.html\">第二话</a></body></html>",
      );
    const generation = "upd-gen";
    await setSetting(testEnv, "update_check_generation", generation);
    await setSetting(testEnv, "update_candidates", "[]");
    await dispatchUpdate(testEnv, {
      kind: "update-comic",
      comicId: "upd-ok",
      generation,
      userId: "upd-user",
    });
    await dispatchUpdate(testEnv, {
      kind: "update-comic",
      comicId: "upd-bad",
      generation,
      userId: "upd-user",
    });
    const candidates = JSON.parse(await setting(testEnv, "update_candidates", "[]"));
    expect(candidates).toEqual([
      expect.objectContaining({
        id: "upd-ok",
        comic_name: "upd-ok",
        status: "completed",
        pending_chapters: 1,
        remote_total_chapters: 2,
        local_total_chapters: 1,
        latest_chapter_title: "第二话",
      }),
      expect.objectContaining({
        id: "upd-bad",
        comic_name: "upd-bad",
        status: "error",
        error_message: "来源暂时不可用",
      }),
    ]);
    expect(
      await bindings.DB.prepare(
        "SELECT COUNT(*) AS n FROM comic_jobs WHERE user_id='upd-user'",
      ).first<{ n: number }>(),
    ).toEqual({ n: 0 });
  });

  it("queues an automatic download only after that comic has new chapters", async () => {
    await seedUser();
    const source = "https://www.manhuagui.com/comic/22222/";
    await addComic("upd-auto", 4, source);
    fetchMock
      .get("https://www.manhuagui.com")
      .intercept({ path: "/comic/22222/" })
      .reply(
        200,
        "<html><body><h1>自动</h1><a href=\"/comic/22222/1.html\">第一话</a></body></html>",
      );
    const generation = "upd-auto-gen";
    await setSetting(testEnv, "update_check_generation", generation);
    await setSetting(testEnv, "update_candidates", "[]");
    sent.length = 0;
    // Wake the scheduler without the shared Durable Object. A previous test
    // file can invalidate it when the worker module reloads.
    const autoEnv = {
      ...testEnv,
      APPLICATION: {
        idFromName: () => "scheduler",
        get: () => ({
          fetch: async () => new Response("{}", { status: 200 }),
        }),
      },
    } as Env;
    await dispatchUpdate(autoEnv, {
      kind: "update-comic",
      comicId: "upd-auto",
      generation,
      userId: "upd-user",
      automatic: true,
    });
    const job = await bindings.DB.prepare(
      "SELECT kind,payload,state FROM comic_jobs WHERE user_id='upd-user'",
    ).first<{ kind: string; payload: string; state: string }>();
    expect(job?.kind).toBe("source");
    expect(job?.state).toBe("queued");
    expect(JSON.parse(job!.payload)).toMatchObject({
      url: source,
      is_update: true,
      group: "upd-group",
    });
    expect(sent.some((body) => body.kind === "comic" && body.id)).toBe(true);
  });
});

describe("orphaned compute jobs", () => {
  afterEach(cleanup);

  it("leaves cancellation to maintenance and hides orphans from the jobs poll", async () => {
    await cleanup();
    await sql(
      "INSERT INTO users(id,username,password_hash,role,can_download) VALUES ('upd-maint','upd-maint','x','admin',1)",
    );
    await sql(
      "INSERT INTO comic_jobs(id,user_id,kind,payload,state,created_at,updated_at) VALUES ('upd-live-parent','upd-maint','source','{}','running',1,1),('upd-dead-parent','upd-maint','source','{}','cancelled',1,1)",
    );
    await sql(
      "INSERT INTO compute_jobs(id,kind,input_key,payload,state,created_at,updated_at) VALUES ('upd-live','cover','upd-live-key','{\"parent_job\":\"upd-live-parent\"}','queued',1,1),('upd-orphan','cover','upd-orphan-key','{\"parent_job\":\"upd-dead-parent\"}','queued',2,2),('upd-free','cover','upd-free-key','{}','queued',3,3)",
    );
    const admin: Identity = {
      user_id: "upd-maint",
      username: "upd-maint",
      role: "admin",
      can_download: 1,
      can_view_adult: 1,
      csrf: "",
      token_hash: "",
    };
    const response = await computeApi(
      new Request("https://mangadock.test/api/admin/compute/jobs"),
      bindings,
      admin,
    );
    const listed = ((await response!.json()) as { data: { id: string }[] }).data.map(
      (row) => row.id,
    );
    expect(listed).toContain("upd-live");
    expect(listed).toContain("upd-free");
    expect(listed).not.toContain("upd-orphan");
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM compute_jobs WHERE id='upd-orphan'",
        ).first<{ state: string }>()
      )?.state,
    ).toBe("queued");
    await cancelOrphanedCompute(bindings);
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM compute_jobs WHERE id='upd-orphan'",
        ).first<{ state: string }>()
      )?.state,
    ).toBe("cancelled");
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM compute_jobs WHERE id='upd-live'",
        ).first<{ state: string }>()
      )?.state,
    ).toBe("queued");
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM compute_jobs WHERE id='upd-free'",
        ).first<{ state: string }>()
      )?.state,
    ).toBe("queued");
  });
});
