import { applyD1Migrations, env, runInDurableObject } from "cloudflare:test";
import { describe, it, expect, vi, afterEach, beforeAll } from "vitest";
import worker, { now, type Env } from "../src/index";
import {
  recoveryAlarm,
  recoveryRequest,
  reclaimDeferred,
  recoverExpiredNativePages,
} from "../src/download-recovery";
import { QueuePausedError } from "../src/queue-safety";
const bindings = env as unknown as Env & {
  TEST_MIGRATIONS: Parameters<typeof applyD1Migrations>[1];
};
beforeAll(async () => applyD1Migrations(bindings.DB, bindings.TEST_MIGRATIONS));
afterEach(() => vi.restoreAllMocks());
function storage() {
  const data = new Map<string, any>();
  let alarm: number | null = null;
  const result: any = {
    get: async (key: string | string[]) =>
      Array.isArray(key)
        ? new Map(
            key
              .filter((k) => data.has(k))
              .map((k) => [k, structuredClone(data.get(k))]),
          )
        : structuredClone(data.get(key)),
    put: async (key: string | Record<string, any>, value: any) => {
      if (typeof key === "string") data.set(key, structuredClone(value));
      else
        for (const [k, v] of Object.entries(key))
          data.set(k, structuredClone(v));
    },
    delete: async (key: string) => data.delete(key),
    list: async ({ prefix, limit }: any) =>
      new Map(
        [...data].filter(([key]) => key.startsWith(prefix)).slice(0, limit),
      ),
    getAlarm: async () => alarm,
    setAlarm: async (time: number) => {
      alarm = time;
    },
    deleteAlarm: async () => {
      alarm = null;
    },
    transaction: async (fn: any) => fn(result),
  };
  return result as DurableObjectStorage;
}
async function hold(
  store: DurableObjectStorage,
  ids = ["held-a", "held-b"],
  retry = 1,
) {
  return recoveryRequest(
    new Request("https://recovery.internal/defer", {
      method: "POST",
      body: JSON.stringify({ ids, retry_after: retry }),
    }),
    store,
  );
}
function environment({
  paused = false,
  retry = 120,
  send = async (_messages: any[]) => {},
  fail = false,
} = {}) {
  let calls = 0;
  return {
    calls: () => calls,
    env: {
      APPLICATION: {
        idFromName: (name: string) => name,
        get: () => ({
          fetch: async (url: string, init: any) => {
            calls++;
            if (fail) throw new Error("synthetic unavailable");
            if (url.endsWith("resource-status"))
              return Response.json({
                database: { paused, retry_after: retry },
              });
            const { ids } = JSON.parse(init.body);
            return Response.json({
              recovered: ids.map((id: string) => ({ id, kind: "page" })),
              pending: [],
            });
          },
        }),
      },
      DOWNLOADS: { sendBatch: send },
    } as unknown as Env,
  };
}
describe("durable database-pause recovery", () => {
  it("deduplicates held IDs and does not spend SQL or Queue calls until protection expires", async () => {
    let time = Date.now();
    vi.spyOn(Date, "now").mockImplementation(() => time);
    const store = storage();
    await hold(store);
    await hold(store);
    expect(await store.get("download-hold-count")).toBe(2);
    time += 1001;
    let sends = 0;
    const blocked = environment({
      paused: true,
      send: async () => {
        sends++;
      },
    });
    await recoveryAlarm(blocked.env, store);
    expect(blocked.calls()).toBe(1);
    expect(sends).toBe(0);
    expect(await store.getAlarm()).toBe(time + 120000);
    time += 120001;
    const ready = environment({
      send: async (messages) => {
        sends++;
        expect(messages).toEqual([
          { body: { kind: "comic-pages", ids: ["held-a", "held-b"] } },
        ]);
      },
    });
    await recoveryAlarm(ready.env, store);
    expect(sends).toBe(1);
    expect(await store.get("download-hold-count")).toBe(0);
    expect(await store.getAlarm()).toBeNull();
  });
  it("retains the recovery plan when Queue admission pauses", async () => {
    let time = Date.now();
    vi.spyOn(Date, "now").mockImplementation(() => time);
    const store = storage();
    await hold(store);
    time += 1001;
    await recoveryAlarm(
      environment({
        send: async () => {
          throw new QueuePausedError(3600);
        },
      }).env,
      store,
    );
    expect(await store.get("download-hold-count")).toBe(2);
    expect(await store.getAlarm()).toBe(time + 3600000);
    time += 3600001;
    await recoveryAlarm(environment().env, store);
    expect(await store.get("download-hold-count")).toBe(0);
  });
  it("bounds infrastructure retries and leaves persisted jobs for ordinary maintenance", async () => {
    let time = Date.now();
    vi.spyOn(Date, "now").mockImplementation(() => time);
    const store = storage();
    await hold(store);
    time += 1001;
    for (let i = 0; i < 5; i++) {
      await recoveryAlarm(environment({ fail: true }).env, store);
      if (i < 4) time = (await store.getAlarm())! + 1;
    }
    expect(await store.get("download-hold-count")).toBe(0);
    expect(await store.getAlarm()).toBeNull();
  });
  it("records the pause before acknowledging a grouped message; persistence failures retry the transport", async () => {
    for (const fail of [false, true]) {
      let saved = false,
        ack = 0,
        retry = 0;
      const outer = {
        ...bindings,
        APPLICATION: {
          idFromName: (name: string) => name,
          get: (name: string) => ({
            fetch: async (_url: string, init: any) => {
              if (name === "jobs")
                return new Response("{}", {
                  status: 503,
                  headers: { "Retry-After": "120" },
                });
              expect(JSON.parse(init.body).ids).toEqual(["held-a", "held-b"]);
              saved = !fail;
              return new Response("{}", { status: fail ? 503 : 200 });
            },
          }),
        },
      } as unknown as Env;
      await worker.queue(
        {
          messages: [
            {
              body: { kind: "comic-pages", ids: ["held-a", "held-b"] },
              attempts: 1,
              ack: () => {
                expect(saved).toBe(true);
                ack++;
              },
              retry: () => {
                retry++;
              },
            },
          ],
        } as any,
        outer,
      );
      expect(ack).toBe(fail ? 0 : 1);
      expect(retry).toBe(fail ? 1 : 0);
    }
  });
  it("reclaims expired native rows while preserving live leases and stopped ancestors", async () => {
    await bindings.DB.prepare(
      "INSERT OR IGNORE INTO users(id,username,password_hash) VALUES ('resume-user','resume-user','unused')",
    ).run();
    await bindings.DB.prepare(
      "DELETE FROM comic_jobs WHERE user_id='resume-user'",
    ).run();
    await bindings.DB.batch([
      bindings.DB.prepare(
        "INSERT INTO comic_jobs(id,user_id,kind,payload,state,created_at,updated_at) VALUES ('resume-root','resume-user','source','{}','waiting',1,1)",
      ),
      bindings.DB.prepare(
        "INSERT INTO comic_jobs(id,user_id,parent_id,kind,payload,state,created_at,updated_at) VALUES ('resume-chapter','resume-user','resume-root','chapter','{}','waiting',1,1)",
      ),
      ...["ready", "live", "completed", "cancelled", "expired"].map((id) =>
        bindings.DB.prepare(
          "INSERT INTO comic_jobs(id,user_id,parent_id,kind,payload,state,lease_until,attempts,claim_token,created_at,updated_at) VALUES (?,'resume-user','resume-chapter','page','{}',?,?,?,?,1,1)",
        ).bind(
          "resume-" + id,
          id === "completed" || id === "cancelled"
            ? id
            : id === "ready"
              ? "queued"
              : "running",
          id === "live" ? now() + 60 : 0,
          id === "expired" ? 2 : 0,
          id === "expired" ? "old-claim" : null,
        ),
      ),
    ]);
    const result = await reclaimDeferred(bindings, [
      "resume-ready",
      "resume-live",
      "resume-completed",
      "resume-cancelled",
      "resume-expired",
    ]);
    expect(result.recovered.map((row) => row.id).sort()).toEqual([
      "resume-expired",
      "resume-ready",
    ]);
    expect(result.pending.map((row) => row.id)).toEqual(["resume-live"]);
    expect(
      await bindings.DB.prepare(
        "SELECT attempts,claim_token FROM comic_jobs WHERE id='resume-expired'",
      ).first(),
    ).toEqual({ attempts: 2, claim_token: null });
    await bindings.DB.prepare(
      "UPDATE comic_jobs SET state='cancelled' WHERE id='resume-root'",
    ).run();
    expect(
      (await reclaimDeferred(bindings, ["resume-ready"])).recovered,
    ).toHaveLength(0);
  });
  it("routes the actual recovery alarm without invoking the compute wake handler", async () => {
    const stub = bindings.APPLICATION.get(
      bindings.APPLICATION.idFromName("download-recovery"),
    );
    await runInDurableObject(stub, async (instance, state) => {
      await state.storage.deleteAll();
      await instance.alarm();
      expect(await state.storage.getAlarm()).toBeNull();
      expect(await state.storage.get("compute-wake")).toBeUndefined();
    });
  });
});

it("expired native executions recover before thirty minutes, preserving attempts and excluding live leases, backoff and stopped parents", async () => {
  await bindings.DB.prepare(
    "INSERT OR IGNORE INTO users(id,username,password_hash,role) VALUES ('lease-user','lease-user','unused','admin')",
  ).run();
  await bindings.DB.prepare(
    "INSERT INTO comic_jobs(id,user_id,kind,payload,state,created_at,updated_at) VALUES ('lease-root','lease-user','source','{}','waiting',1,1)",
  ).run();
  await bindings.DB.prepare(
    "INSERT INTO comic_jobs(id,user_id,parent_id,kind,payload,state,created_at,updated_at) VALUES ('lease-chapter','lease-user','lease-root','chapter','{}','waiting',1,1),('lease-stopped-chapter','lease-user','lease-root','chapter','{}','cancelled',1,1)",
  ).run();
  for (const [id, state, lease, updated, attempts, parent] of [
    ["expired", "running", now() - 1, now(), 2, "lease-chapter"],
    ["lost", "queued", 0, now() - 301, 0, "lease-chapter"],
    ["recent", "queued", 0, now(), 0, "lease-chapter"],
    ["live", "running", now() + 120, now(), 1, "lease-chapter"],
    ["backoff", "queued", now() + 120, now() - 900, 1, "lease-chapter"],
    ["finished", "completed", 0, 1, 1, "lease-chapter"],
    ["cancelled", "cancelled", 0, 1, 1, "lease-chapter"],
    ["exhausted", "running", now() - 1, 1, 5, "lease-chapter"],
    ["stopped", "running", now() - 1, 1, 1, "lease-stopped-chapter"],
  ] as const)
    await bindings.DB.prepare(
      "INSERT INTO comic_jobs(id,user_id,parent_id,kind,payload,state,lease_until,attempts,claim_token,created_at,updated_at) VALUES (?,'lease-user',?,'page','{}',?,?,?,'old-execution',1,?)",
    )
      .bind("lease-" + id, parent, state, lease, attempts, updated)
      .run();
  const result = await recoverExpiredNativePages(bindings);
  expect(
    result.results
      .filter((r) => r.id.startsWith("lease-"))
      .map((r) => r.id)
      .sort(),
  ).toEqual(["lease-expired", "lease-lost"]);
  expect(
    await bindings.DB.prepare(
      "SELECT state,attempts,lease_until,claim_token FROM comic_jobs WHERE id='lease-expired'",
    ).first(),
  ).toMatchObject({
    state: "queued",
    attempts: 2,
    lease_until: 0,
    claim_token: null,
  });
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='cancelled' WHERE id='lease-root'",
  ).run();
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET lease_until=0 WHERE id='lease-live'",
  ).run();
  expect(
    (await recoverExpiredNativePages(bindings)).results.some((r) =>
      r.id.startsWith("lease-"),
    ),
  ).toBe(false);
});

it("redelivery during a live claim records recovery before acknowledging instead of silently dropping the job", async () => {
  await bindings.DB.prepare(
    "INSERT OR IGNORE INTO users(id,username,password_hash,role) VALUES ('held-user','held-user','unused','admin')",
  ).run();
  await bindings.DB.prepare(
    "INSERT INTO comic_jobs(id,user_id,kind,payload,state,created_at,updated_at) VALUES ('held-root','held-user','source','{}','waiting',1,1)",
  ).run();
  await bindings.DB.prepare(
    "INSERT INTO comic_jobs(id,user_id,parent_id,kind,payload,state,created_at,updated_at) VALUES ('held-chapter','held-user','held-root','chapter','{}','waiting',1,1)",
  ).run();
  const lease = now() + 120;
  await bindings.DB.prepare(
    "INSERT INTO comic_jobs(id,user_id,parent_id,kind,payload,state,lease_until,claim_token,attempts,created_at,updated_at) VALUES ('held-leaf','held-user','held-chapter','page','{}','running',?,'live-token',1,1,1)",
  )
    .bind(lease)
    .run();
  const stub = bindings.APPLICATION.get(
    bindings.APPLICATION.idFromName("lease-deferral-execution-test"),
  );
  const response = await stub.fetch("https://jobs.internal/run", {
    method: "POST",
    body: JSON.stringify({ id: "held-leaf" }),
  });
  expect(response.status).toBe(503);
  expect(Number(response.headers.get("Retry-After"))).toBeGreaterThan(100);
  let ack = 0,
    retry = 0;
  // Isolate live test instances from other files' lazy module recompilation.
  const local = {
    ...bindings,
    APPLICATION: {
      idFromName: (name: string) =>
        bindings.APPLICATION.idFromName(
          name === "jobs"
            ? "lease-deferral-execution-test"
            : name === "download-recovery"
              ? "lease-deferral-record-test"
              : name,
        ),
      get: (id: DurableObjectId) => bindings.APPLICATION.get(id),
    },
  } as unknown as Env;
  await worker.queue(
    {
      messages: [
        {
          body: { kind: "comic-pages", ids: ["held-leaf"] },
          attempts: 2,
          ack: () => ack++,
          retry: () => retry++,
        },
      ],
    } as any,
    local,
  );
  expect(ack).toBe(1);
  expect(retry).toBe(0);
  await runInDurableObject(
    bindings.APPLICATION.get(
      bindings.APPLICATION.idFromName("lease-deferral-record-test"),
    ),
    async (_instance, state) => {
      const held = await state.storage.get<any>("download-hold/held-leaf");
      expect(held.id).toBe("held-leaf");
      expect(held.due).toBeGreaterThanOrEqual(lease * 1000);
    },
  );
  expect(
    await bindings.DB.prepare(
      "SELECT state,attempts,claim_token FROM comic_jobs WHERE id='held-leaf'",
    ).first(),
  ).toMatchObject({ state: "running", attempts: 1, claim_token: "live-token" });
});
