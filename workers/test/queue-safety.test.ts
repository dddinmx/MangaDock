import { expect, it, vi } from "vitest";
import { applyD1Migrations, env } from "cloudflare:test";
import worker, { now, type Env } from "../src/index";
import { QueueBudget, queueSend } from "../src/queue-safety";
import { runJob, recoverNativeChapters } from "../src/downloads";
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
it("a 260-page chapter checkpoints creation and packs fifty IDs per queue message", async () => {
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
              images: Array.from({ length: 260 }, (_, i) => ({
                order: i,
                url: `/page-${i}.jpg`,
              })),
            },
          },
        },
      }),
    )
    .times(2);
  const messages: any[] = [];
  let sqlRequests = 0;
  const wrap = (raw: any): any => ({
    raw,
    bind: (...args: any[]) => wrap(raw.bind(...args)),
    first: (...args: any[]) => {
      sqlRequests++;
      return raw.first(...args);
    },
    all: (...args: any[]) => {
      sqlRequests++;
      return raw.all(...args);
    },
    run: (...args: any[]) => {
      sqlRequests++;
      return raw.run(...args);
    },
  });
  const countedDB = {
    prepare: (sql: string) => wrap(bindings.DB.prepare(sql)),
    batch: (items: any[]) => {
      sqlRequests++;
      return bindings.DB.batch(items.map((item) => item.raw));
    },
  };
  const local = {
    ...bindings,
    DB: countedDB,
    DOWNLOADS: {
      send: async (body: any) => messages.push(body),
      sendBatch: async (items: any[]) =>
        messages.push(...items.map((i) => i.body)),
    },
  } as unknown as Env;
  try {
    await runJob(local, "batch-job");
    expect(sqlRequests).toBeLessThan(30);
    expect(
      await bindings.DB.prepare(
        "SELECT state FROM comic_jobs WHERE id='batch-job'",
      ).first("state"),
    ).toBe("queued");
    expect(messages).toEqual([{ id: "batch-job", kind: "comic" }]);
    expect(
      (
        await bindings.DB.prepare(
          "SELECT COUNT(*) AS n FROM comic_jobs WHERE parent_id='batch-job'",
        ).first<any>()
      )?.n,
    ).toBe(200);
    messages.length = 0;
    sqlRequests = 0;
    await runJob(local, "batch-job");
    expect(sqlRequests).toBeLessThan(30);
    const jobs = await bindings.DB.prepare(
      "SELECT id FROM comic_jobs WHERE parent_id='batch-job' AND kind='page'",
    ).all();
    expect(jobs.results).toHaveLength(260);
    expect(messages.map((m) => m.ids?.length)).toEqual([
      50, 50, 50, 50, 50, 10,
    ]);
    expect(new Set(messages.flatMap((m) => m.ids)).size).toBe(260);
    fetchMock.assertNoPendingInterceptors();
  } finally {
    fetchMock.deactivate();
  }
});

it("page batches use fresh invocations, finish later IDs after failure, and retry only once", async () => {
  for (const attempts of [1, 2]) {
    const calls: { url: string; id: string }[] = [];
    let acks = 0,
      retries = 0;
    const ids = Array.from({ length: 10 }, (_, i) => `page-${i}`);
    const local = {
      APPLICATION: {
        idFromName: () => "jobs",
        get: () => ({
          fetch: async (url: string, init: RequestInit) => {
            const { id } = JSON.parse(String(init.body));
            calls.push({ url, id });
            return new Response("{}", { status: id === "page-0" ? 500 : 200 });
          },
        }),
      },
    } as unknown as Env;
    await worker.queue(
      {
        messages: [
          {
            body: { kind: "comic-pages", ids },
            attempts,
            ack: () => acks++,
            retry: () => retries++,
          },
        ],
      } as any,
      local,
    );
    expect(calls).toEqual(
      ids.map((id) => ({ url: "https://jobs.internal/run", id })),
    );
    expect(retries).toBe(attempts === 1 ? 1 : 0);
    expect(acks).toBe(attempts === 1 ? 0 : 1);
  }
});
it("a provider pause records the entire remaining batch after one probe", async () => {
  const ids = Array.from({ length: 50 }, (_, i) => `hold-page-${i}`);
  const calls: { url: string; data: any }[] = [];
  let acks = 0,
    retries = 0;
  const local = {
    APPLICATION: {
      idFromName: (name: string) => name,
      get: () => ({
        fetch: async (url: string, init: RequestInit) => {
          calls.push({ url, data: JSON.parse(String(init.body)) });
          return new Response(
            "{}",
            url.includes("jobs.internal")
              ? { status: 503, headers: { "Retry-After": "28800" } }
              : { status: 200 },
          );
        },
      }),
    },
  } as unknown as Env;
  await worker.queue(
    {
      messages: [
        {
          body: { kind: "comic-pages", ids },
          attempts: 1,
          ack: () => acks++,
          retry: () => retries++,
        },
      ],
    } as any,
    local,
  );
  expect(calls).toEqual([
    { url: "https://jobs.internal/run", data: { id: ids[0] } },
    {
      url: "https://recovery.internal/defer",
      data: { ids, retry_after: 28800 },
    },
  ]);
  expect(acks).toBe(1);
  expect(retries).toBe(0);
});
it("a slow queue batch defers untouched IDs before the fifteen-minute consumer wall limit", async () => {
  const ids = ["deadline-0", "deadline-1", "deadline-2"];
  let clock = Date.now();
  const mocked = vi.spyOn(Date, "now").mockImplementation(() => clock);
  const calls: { url: string; data: any }[] = [];
  let acks = 0;
  try {
    const local = {
      APPLICATION: {
        idFromName: (name: string) => name,
        get: () => ({
          fetch: async (url: string, init: RequestInit) => {
            calls.push({ url, data: JSON.parse(String(init.body)) });
            if (url.includes("jobs.internal")) clock += 540001;
            return new Response("{}");
          },
        }),
      },
    } as unknown as Env;
    await worker.queue(
      {
        messages: [
          {
            body: { kind: "comic-pages", ids },
            attempts: 1,
            ack: () => acks++,
            retry: () => {},
          },
        ],
      } as any,
      local,
    );
    expect(calls).toEqual([
      { url: "https://jobs.internal/run", data: { id: ids[0] } },
      {
        url: "https://recovery.internal/defer",
        data: { ids: ids.slice(1), retry_after: 1 },
      },
    ]);
    expect(acks).toBe(1);
  } finally {
    mocked.mockRestore();
  }
});

it("repairs a partial native plan and a lost completion callback without replacing completed pages or restarting cancelled roots", async () => {
  const bindings = env as unknown as Env & { TEST_MIGRATIONS: any };
  await applyD1Migrations(bindings.DB, bindings.TEST_MIGRATIONS);
  await bindings.DB.prepare(
    "INSERT INTO users(id,username,password_hash,role,can_download) VALUES ('repair-user','repair-user','x','admin',1)",
  ).run();
  await bindings.DB.prepare(
    "INSERT INTO comics(id,name,provider,source_url,created_at,updated_at) VALUES ('repair-comic','repair-comic','baozimh_org','https://baozimh.org/manga/test',0,0)",
  ).run();
  await bindings.DB.prepare(
    "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,state,page_count) VALUES ('repair-chapter','repair-comic',0,'repair','4','downloading',4)",
  ).run();
  await bindings.DB.prepare(
    "INSERT INTO comic_jobs(id,user_id,comic_id,kind,payload,state,created_at,updated_at) VALUES ('repair-root','repair-user','repair-comic','source','{}','waiting',0,0)",
  ).run();
  const data = {
    provider: "baozimh_org",
    chapter: { id: "4", mid: "1" },
    chapter_id: "repair-chapter",
    format: 2,
  };
  await bindings.DB.prepare(
    "INSERT INTO comic_jobs(id,user_id,comic_id,parent_id,kind,payload,state,attempts,created_at,updated_at) VALUES ('repair-job','repair-user','repair-comic','repair-root','chapter',?,'waiting',1,0,0)",
  )
    .bind(JSON.stringify(data))
    .run();
  for (let page = 0; page < 3; page++)
    await bindings.DB.prepare(
      "INSERT INTO comic_jobs(id,user_id,comic_id,parent_id,kind,payload,state,created_at,updated_at) VALUES (?,'repair-user','repair-comic','repair-job','page',?,'completed',0,0)",
    )
      .bind(`repair-page-${page}`, JSON.stringify({ page }))
      .run();
  expect(
    (await recoverNativeChapters(bindings)).results.map((j) => j.id),
  ).toContain("repair-job");
  fetchMock.activate();
  fetchMock.disableNetConnect();
  fetchMock
    .get("https://v2.apikk.top")
    .intercept({ path: "/api/v2/chapter/getinfo?m=1&c=4" })
    .reply(
      200,
      JSON.stringify({
        code: 200,
        data: {
          info: {
            images: {
              line: 1,
              images: Array.from({ length: 4 }, (_, i) => ({
                order: i,
                url: `/repair-${i}.jpg`,
              })),
            },
          },
        },
      }),
    )
    .times(2);
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
    await runJob(local, "repair-job");
    expect(
      (
        await bindings.DB.prepare(
          "SELECT id FROM comic_jobs WHERE parent_id='repair-job'",
        ).all()
      ).results,
    ).toHaveLength(4);
    expect(
      (
        await bindings.DB.prepare(
          "SELECT id FROM comic_jobs WHERE parent_id='repair-job' AND state='completed'",
        ).all()
      ).results
        .map((r) => r.id)
        .sort(),
    ).toEqual(["repair-page-0", "repair-page-1", "repair-page-2"]);
    expect(messages).toHaveLength(1);
    expect(messages[0].ids).toHaveLength(1);
    await bindings.DB.prepare(
      "UPDATE comic_jobs SET state='completed' WHERE parent_id='repair-job'",
    ).run();
    await bindings.DB.prepare(
      "UPDATE comic_jobs SET updated_at=0 WHERE id='repair-job'",
    ).run();
    await bindings.DB.prepare(
      "UPDATE comic_jobs SET state='cancelled' WHERE id='repair-root'",
    ).run();
    expect(
      (await recoverNativeChapters(bindings)).results.map((j) => j.id),
    ).not.toContain("repair-job");
    await bindings.DB.prepare(
      "UPDATE comic_jobs SET state='waiting' WHERE id='repair-root'",
    ).run();
    expect(
      (await recoverNativeChapters(bindings)).results.map((j) => j.id),
    ).toContain("repair-job");
    await runJob(local, "repair-job");
    expect(
      await bindings.DB.prepare(
        "SELECT state FROM chapters WHERE id='repair-chapter'",
      ).first("state"),
    ).toBe("ready");
    expect(
      await bindings.DB.prepare(
        "SELECT state FROM comic_jobs WHERE id='repair-job'",
      ).first("state"),
    ).toBe("completed");
    // Old runtimes incorrectly completed a subset. A currently active root
    // may repair that child; fully complete and cancelled roots stay stopped.
    await bindings.DB.prepare(
      "UPDATE comic_jobs SET state='waiting' WHERE id='repair-root'",
    ).run();
    await bindings.DB.prepare(
      "UPDATE chapters SET page_count=5 WHERE id='repair-chapter'",
    ).run();
    await bindings.DB.prepare(
      "UPDATE comic_jobs SET updated_at=0 WHERE id='repair-job'",
    ).run();
    await bindings.DB.prepare(
      "INSERT INTO compute_jobs(id,kind,input_key,payload,state,created_at,updated_at) VALUES ('repair-cloud','legacy-chapter','repair-cloud',?,'completed',0,0)",
    )
      .bind(JSON.stringify({ parent_job: "repair-job" }))
      .run();
    expect(
      (await recoverNativeChapters(bindings)).results.map((j) => j.id),
    ).not.toContain("repair-job");
    await bindings.DB.prepare(
      "DELETE FROM compute_jobs WHERE id='repair-cloud'",
    ).run();
    expect(
      (await recoverNativeChapters(bindings)).results.map((j) => j.id),
    ).toContain("repair-job");
    expect(
      await bindings.DB.prepare(
        "SELECT completed_chapters FROM comic_jobs WHERE id='repair-root'",
      ).first("completed_chapters"),
    ).toBe(0);
    expect(
      await bindings.DB.prepare(
        "SELECT state FROM chapters WHERE id='repair-chapter'",
      ).first("state"),
    ).toBe("downloading");
    expect(
      (
        await bindings.DB.prepare(
          "SELECT id FROM comic_jobs WHERE parent_id='repair-job' AND state='completed'",
        ).all()
      ).results,
    ).toHaveLength(4);
    fetchMock.assertNoPendingInterceptors();
  } finally {
    fetchMock.deactivate();
  }
});
