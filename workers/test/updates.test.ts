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
  updateStatus,
  recoverUpdates,
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
  await sql("DELETE FROM update_checks WHERE user_id LIKE 'upd-%'");
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

describe("continuous compute wake leases", () => {
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
    expect((await probe(storage)).data.pending).toBe(true);
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

  it("admits the next batch after 24 successes without erasing history or the old idle cooldown", async () => {
    const storage = memoryStorage();
    const day = new Date().toISOString().slice(0, 10);
    await storage.put("compute-wake", {
      sequence: 4,
      pending: true,
      day,
      runs: 24,
      next: now() + 900,
      leasedSequence: 0,
    });
    expect((await probe(storage)).data.pending).toBe(true);
    expect((await storage.get("compute-wake")).runs).toBe(24);
    await finish(storage, { sequence: 4, pending: true });
    expect((await storage.get("compute-wake")).runs).toBe(25);
    expect((await probe(storage)).data.pending).toBe(true);
  });

  it("keeps an active old launcher lease even after 24 successful batches", async () => {
    const storage = memoryStorage();
    await storage.put("compute-wake", {
      sequence: 4,
      pending: true,
      day: new Date().toISOString().slice(0, 10),
      runs: 24,
      next: now() + 900,
      leasedSequence: 4,
    });
    expect((await probe(storage)).data.pending).toBe(false);
    expect((await storage.get("compute-wake")).runs).toBe(24);
  });

  it("does not let a stale completion release or acknowledge a newer wake", async () => {
    const storage = memoryStorage();
    await wake(storage);
    await probe(storage);
    await wake(storage);
    await finish(storage, { sequence: 1, pending: false });
    expect((await probe(storage)).data).toMatchObject({
      pending: true,
      sequence: 2,
    });
    await finish(storage, { sequence: 1, pending: false });
    expect((await storage.get("compute-wake")).leasedSequence).toBe(2);
    expect((await probe(storage)).data.pending).toBe(false);
    await finish(storage, { sequence: 2, pending: false });
    expect((await probe(storage)).data.pending).toBe(false);
    expect((await storage.get("compute-wake")).runs).toBe(2);
  });

  it("recovers an expired launcher lease without spending or resetting its runs", async () => {
    const storage = memoryStorage();
    await storage.put("compute-wake", {
      sequence: 4,
      pending: true,
      day: new Date().toISOString().slice(0, 10),
      runs: 26,
      next: now() - 1,
      leasedSequence: 4,
    });
    expect((await probe(storage)).data.pending).toBe(true);
    expect((await storage.get("compute-wake")).runs).toBe(26);
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

  it("enqueues one active check per user and reports queued rather than old results", async () => {
    await seedUser();
    await addComic("upd-a", 1, "https://example.com/a");
    expect(await checkUpdates(testEnv, user)).toEqual([]);
    expect(await checkUpdates(testEnv, user)).toEqual([]);
    expect(sent).toHaveLength(1);
    expect(await updateStatus(testEnv, user)).toMatchObject({
      checking: true,
      total: 0,
      candidates: [],
      finished_at: 0,
    });
  });
  it("pages stable IDs, deduplicates redelivery and finishes only after every comic", async () => {
    await seedUser();
    for (let i = 0; i < 5; i++)
      await addComic(`upd-${i}`, 50 - i, "https://example.com/a");
    await addComic("upd-hidden", 99, "https://example.com/h", "默认分组");
    await checkUpdates(testEnv, user);
    let page = { ...sent.shift()!, budget: 2 };
    const seen: string[] = [];
    for (let steps = 0; steps < 3; steps++) {
      sent.length = 0;
      await dispatchUpdate(testEnv, page);
      for (const item of sent.filter((body) => body.kind === "update-comic")) {
        seen.push(item.comicId!);
        await dispatchUpdate(testEnv, item);
        await dispatchUpdate(testEnv, item);
      }
      const next = sent.find((body) => body.kind === "update-page");
      if (!next) break;
      page = next;
    }
    expect(seen).toEqual(["upd-0", "upd-1", "upd-2", "upd-3", "upd-4"]);
    const status = await updateStatus(testEnv, user);
    expect(status).toMatchObject({
      checking: false,
      total: 5,
      failed: 5,
      completed: 0,
    });
    expect(status.finished_at).toBeGreaterThan(0);
    expect(
      (await updateStatus(testEnv, { ...user, user_id: "other" })).candidates,
    ).toEqual([]);
  });
  it("keeps the single comic result and rejects duplicate/expired messages", async () => {
    await seedUser();
    await addComic("upd-ok", 1, "https://www.manhuagui.com/comic/12345/");
    await sql(
      "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,state) VALUES ('upd-ch','upd-ok',1,'第一话',?,'ready')",
      "https://www.manhuagui.com/comic/12345/1.html",
    );
    fetchMock
      .get("https://www.manhuagui.com")
      .intercept({ path: "/comic/12345/" })
      .reply(
        200,
        '<html><body><h1>更新测试</h1><a href="/comic/12345/1.html">第一话</a><a href="/comic/12345/2.html">第二话</a></body></html>',
      );
    await checkUpdates(testEnv, user);
    const page = sent.shift()!;
    await dispatchUpdate(testEnv, page);
    expect(await updateStatus(testEnv, user)).toMatchObject({
      checking: true,
      total: 1,
      finished_at: 0,
      candidates: [],
    });
    const item = sent.find((body) => body.kind === "update-comic")!;
    await dispatchUpdate(testEnv, item);
    await dispatchUpdate(testEnv, item);
    expect(await updateStatus(testEnv, user)).toMatchObject({
      checking: false,
      total: 1,
      completed: 1,
      failed: 0,
      candidates: [
        {
          comic_name: "upd-ok",
          pending_chapters: 1,
          remote_total_chapters: 2,
          local_total_chapters: 1,
          latest_chapter_title: "第二话",
        },
      ],
    });
    await dispatchUpdate(testEnv, { ...item, generation: "expired" });
  });
  it("automatic checks enqueue a download and retain pending gaps", async () => {
    await seedUser();
    await addComic("upd-auto", 1, "https://www.manhuagui.com/comic/22222/");
    fetchMock
      .get("https://www.manhuagui.com")
      .intercept({ path: "/comic/22222/" })
      .reply(
        200,
        '<html><body><h1>自动</h1><a href="/comic/22222/1.html">第一话</a></body></html>',
      );
    const autoEnv = {
      ...testEnv,
      APPLICATION: {
        idFromName: () => "scheduler",
        get: () => ({ fetch: async () => new Response("{}") }),
      },
    } as unknown as Env;
    await checkUpdates(autoEnv, user, true);
    await dispatchUpdate(autoEnv, sent.shift()!);
    await dispatchUpdate(
      autoEnv,
      sent.find((body) => body.kind === "update-comic")!,
    );
    const job = await bindings.DB.prepare(
      "SELECT payload FROM comic_jobs WHERE user_id='upd-user'",
    ).first<any>();
    expect(JSON.parse(job.payload)).toMatchObject({
      is_update: true,
      max_chapters: 0,
    });
  });
  it("maintenance recovers lost messages and dead-letters exhausted items", async () => {
    await seedUser();
    await addComic("upd-lost", 1, "https://example.com/a");
    await checkUpdates(testEnv, user);
    await dispatchUpdate(testEnv, sent.shift()!);
    await sql(
      "UPDATE update_check_items SET updated_at=0 WHERE comic_id='upd-lost'",
    );
    sent.length = 0;
    await recoverUpdates(testEnv);
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      kind: "update-comic",
      comicId: "upd-lost",
    });
    await sql(
      "UPDATE update_check_items SET attempts=3,lease_until=0 WHERE comic_id='upd-lost'",
    );
    await recoverUpdates(testEnv);
    expect(await updateStatus(testEnv, user)).toMatchObject({
      checking: false,
      failed: 1,
      total: 1,
    });
  });
  it("finishes a check after the last item committed but aggregation was lost", async () => {
    await seedUser();
    await addComic("upd-last", 1, "https://example.com/a");
    await checkUpdates(testEnv, user);
    await dispatchUpdate(testEnv, sent.shift()!);
    await sql(
      "UPDATE update_check_items SET state='completed',updated_at=? WHERE comic_id='upd-last'",
      now(),
    );
    expect(await updateStatus(testEnv, user)).toMatchObject({
      checking: true,
      completed: 1,
      total: 1,
    });
    await recoverUpdates(testEnv);
    expect(await updateStatus(testEnv, user)).toMatchObject({
      checking: false,
      completed: 1,
      total: 1,
    });
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
    const listed = (
      (await response!.json()) as { data: { id: string }[] }
    ).data.map((row) => row.id);
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
