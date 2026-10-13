import { expect, it } from "vitest";
import { applyD1Migrations, env } from "cloudflare:test";
import worker, { now, type Env } from "../src/index";
import { QueueBudget, queueSend } from "../src/queue-safety";
import { runJob } from "../src/downloads";
import { fetchMock } from "cloudflare:test";

function storage() {
  const values = new Map<string, unknown>();
  let writes = 0;
  let tail = Promise.resolve();
  const tx = {
    get: async (key: string) => values.get(key),
    put: async (key: string, value: unknown) => {
      writes++;
      values.set(key, value);
    },
  };
  return {
    get writes() {
      return writes;
    },
    transaction: (fn: any) => {
      const result = tail.then(() => fn(tx));
      tail = result.then(
        () => {},
        () => {},
      );
      return result;
    },
  } as unknown as DurableObjectStorage & { writes: number };
}
it("queue admission is atomic, persisted, resets at UTC midnight, and rejected loops do not write", async () => {
  const state = storage();
  let time = 100000;
  const budget = new QueueBudget(state, () => time * 1000, 3);
  const results = await Promise.all(
    Array.from({ length: 8 }, () => budget.reserve(1)),
  );
  expect(results.filter((r) => r.allowed)).toHaveLength(3);
  const writes = state.writes;
  for (let i = 0; i < 100; i++)
    expect(
      (await new QueueBudget(state, () => time * 1000, 3).reserve(1)).allowed,
    ).toBe(false);
  expect(state.writes).toBe(writes);
  time = 172800;
  expect((await budget.reserve(1)).allowed).toBe(true);
});
it("emergency pause blocks the provider and persists across a redeployment until reset", async () => {
  const state = storage();
  let calls = 0;
  const bindings = { QUEUE_SEND_PAUSED_UNTIL: String(now() + 120) } as Env;
  const raw = {
    sendBatch: async () => {
      calls++;
    },
  } as any;
  const request = () =>
    new Request("https://queue.internal/send", {
      method: "POST",
      body: JSON.stringify([{ body: { id: "test", kind: "comic" } }]),
    });
  expect((await queueSend(request(), bindings, state, raw)).status).toBe(429);
  expect((await queueSend(request(), {} as Env, state, raw)).status).toBe(429);
  expect(calls).toBe(0);
  expect(state.writes).toBe(1);
});
it("oversized messages are rejected before a quota reservation or provider call", async () => {
  const state = storage();
  let calls = 0;
  await expect(
    queueSend(
      new Request("https://queue.internal/send", {
        method: "POST",
        body: JSON.stringify([{ body: { id: "x".repeat(17000) } }]),
      }),
      {} as Env,
      state,
      { sendBatch: async () => calls++ } as any,
    ),
  ).rejects.toThrow("Queue message exceeds");
  expect(state.writes).toBe(0);
  expect(calls).toBe(0);
});
it("database pauses are acknowledged rather than redelivered; transient failure retries once", async () => {
  for (const [status, attempts, expectedRetries] of [
    [503, 1, 0],
    [429, 1, 0],
    [500, 1, 1],
    [500, 2, 0],
  ]) {
    let retries = 0,
      acks = 0;
    const local = {
      APPLICATION: {
        idFromName: () => "jobs",
        get: () => ({ fetch: async () => new Response("{}", { status }) }),
      },
    } as unknown as Env;
    await worker.queue(
      {
        messages: [
          {
            body: { id: "test", kind: "comic" },
            attempts,
            ack: () => acks++,
            retry: () => retries++,
          },
        ],
      } as any,
      local,
    );
    expect(retries).toBe(expectedRetries);
    expect(acks).toBe(1 - expectedRetries);
  }
});
it("a chapter with 21 pages persists every job but uses three queue messages", async () => {
  const bindings = env as unknown as Env & { TEST_MIGRATIONS: any };
  await applyD1Migrations(bindings.DB, bindings.TEST_MIGRATIONS);
  await bindings.DB.prepare(
    "INSERT OR IGNORE INTO users(id,username,password_hash,role,can_download) VALUES ('batch-user','batch-user','x','admin',1)",
  ).run();
  await bindings.DB.prepare(
    "INSERT OR IGNORE INTO comics(id,name,provider,source_url,created_at,updated_at) VALUES ('batch-comic','batch-comic','baozimh_org','https://baozimh.org/manga/test',0,0)",
  ).run();
  await bindings.DB.prepare(
    "INSERT OR IGNORE INTO chapters(id,comic_id,ordinal,title,source_id) VALUES ('batch-chapter','batch-comic',0,'one','21')",
  ).run();
  await bindings.DB.prepare(
    "INSERT INTO comic_jobs(id,user_id,comic_id,kind,payload,created_at,updated_at) VALUES ('batch-job','batch-user','batch-comic','chapter',?,0,0)",
  )
    .bind(
      JSON.stringify({
        provider: "baozimh_org",
        chapter: { id: "21", mid: "1" },
        chapter_id: "batch-chapter",
        format: 2,
      }),
    )
    .run();
  fetchMock.activate();
  fetchMock.disableNetConnect();
  fetchMock
    .get("https://v2.apikk.top")
    .intercept({ path: "/api/v2/chapter/getinfo?m=1&c=21" })
    .reply(
      200,
      JSON.stringify({
        code: 200,
        data: {
          info: {
            images: {
              line: 1,
              images: Array.from({ length: 21 }, (_, i) => ({
                order: i,
                url: `/page-${i}.jpg`,
              })),
            },
          },
        },
      }),
    );
  const messages: any[] = [];
  const local = {
    ...bindings,
    DOWNLOADS: {
      send: async (body: any) => messages.push(body),
      sendBatch: async (items: any[]) =>
        messages.push(...items.map((i) => i.body)),
    },
  } as unknown as Env;
  try {
    await runJob(local, "batch-job");
    const jobs = await bindings.DB.prepare(
      "SELECT id FROM comic_jobs WHERE parent_id='batch-job' AND kind='page'",
    ).all();
    expect(jobs.results).toHaveLength(21);
    expect(messages.map((m) => m.ids?.length)).toEqual([10, 10, 1]);
    expect(new Set(messages.flatMap((m) => m.ids)).size).toBe(21);
    fetchMock.assertNoPendingInterceptors();
  } finally {
    fetchMock.deactivate();
  }
});
