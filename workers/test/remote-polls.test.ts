import { applyD1Migrations, env, fetchMock } from "cloudflare:test";
import { beforeAll, beforeEach, afterEach, expect, it } from "vitest";
import { zipSync, strToU8 } from "fflate";
import { now, type Env } from "../src/index";
import { runJob } from "../src/downloads";
import { DatabasePausedError } from "../src/database-budget";
import {
  remotePollAlarm,
  remotePollRequest,
  recoverRemotePolls,
  wakeRemotePolls,
} from "../src/remote-polls";

const bindings = env as unknown as Env & { TEST_MIGRATIONS: any };
function memoryStorage() {
  const data = new Map<string, unknown>();
  let alarm: number | null = null,
    tail = Promise.resolve();
  const state = {
    get: async (key: string) => structuredClone(data.get(key)),
    put: async (key: string, value: unknown) => {
      data.set(key, structuredClone(value));
    },
    delete: async (key: string) => data.delete(key),
    getAlarm: async () => alarm,
    setAlarm: async (at: number) => {
      alarm = at;
    },
    deleteAlarm: async () => {
      alarm = null;
    },
    transaction: (fn: any) => {
      const result = tail.then(() => fn(state));
      tail = result.catch(() => {});
      return result;
    },
  };
  return state as unknown as DurableObjectStorage;
}
let state: DurableObjectStorage, local: Env, sent: any[];
async function sql(query: string, ...args: unknown[]) {
  return bindings.DB.prepare(query)
    .bind(...args)
    .run();
}
async function job(id = "rp-job", due: number | null = null) {
  await sql(
    "INSERT INTO comic_jobs(id,user_id,comic_id,kind,payload,remote_poll_at,created_at,updated_at) VALUES (?,'rp-user','rp-comic','chapter',?,?,?,?)",
    id,
    JSON.stringify({
      provider: "fanqie",
      remote_id: id,
      chapter_id: "rp-chapter",
      chapter: { book_id: "12345678", id: "12345679" },
    }),
    due,
    now(),
    now(),
  );
}
async function row(id = "rp-job") {
  return bindings.DB.prepare("SELECT * FROM comic_jobs WHERE id=?")
    .bind(id)
    .first<any>();
}
function remote(id: string, status = "running") {
  fetchMock
    .get("https://fanqie.dddinmx.cn")
    .intercept({ path: `/v1/jobs/${id}` })
    .reply(200, { ok: true, data: { status } });
}
beforeAll(async () => applyD1Migrations(bindings.DB, bindings.TEST_MIGRATIONS));
beforeEach(async () => {
  state = memoryStorage();
  sent = [];
  local = {
    ...bindings,
    APPLICATION: {
      idFromName: (name: string) => name,
      get: (name: string) => ({
        fetch: (url: string, init: RequestInit) =>
          name === "remote-polls"
            ? remotePollRequest(new Request(url, init), state)
            : Promise.resolve(new Response("{}")),
      }),
    },
    DOWNLOADS: {
      send: async (body: any) => {
        sent.push(body);
      },
      sendBatch: async (items: any[]) => {
        sent.push(...items);
      },
    },
  } as unknown as Env;
  await sql(
    "DELETE FROM compute_jobs WHERE json_extract(payload,'$.parent_job') LIKE 'rp-%'",
  );
  await sql(
    "DELETE FROM comic_jobs WHERE id LIKE 'rp-%' OR remote_poll_at IS NOT NULL",
  );
  await sql(
    "INSERT OR IGNORE INTO users(id,username,password_hash,role,can_download) VALUES ('rp-user','rp-user','x','admin',1)",
  );
  await sql(
    "INSERT OR IGNORE INTO comics(id,name,provider,source_url,created_at,updated_at) VALUES ('rp-comic','rp-comic','fanqie','fanqie-comic://12345678',0,0)",
  );
  await sql(
    "INSERT OR IGNORE INTO chapters(id,comic_id,ordinal,title,source_id) VALUES ('rp-chapter','rp-comic',0,'one','12345679')",
  );
  fetchMock.activate();
  fetchMock.disableNetConnect();
});
afterEach(() => {
  fetchMock.assertNoPendingInterceptors();
  fetchMock.deactivate();
});

it("waiting through repeated upstream checks sends no Queue messages and respects backoff and duplicate deliveries", async () => {
  await job();
  for (let i = 0; i < 5; i++) {
    remote("rp-job");
    await runJob(local, "rp-job");
    const parked = await row();
    expect(parked.state).toBe("queued");
    expect(parked.attempts).toBe(0);
    expect(JSON.parse(parked.payload).polls).toBe(i + 1);
    expect(parked.remote_poll_at).toBe(parked.lease_until);
    expect(parked.lease_until - now()).toBeGreaterThanOrEqual(
      Math.min(60 * 2 ** i, 300) - 2,
    );
    await runJob(local, "rp-job"); // no second HTTP request before the lease expires
    await sql(
      "UPDATE comic_jobs SET lease_until=0,remote_poll_at=? WHERE id='rp-job'",
      now(),
    );
  }
  expect(sent).toEqual([]);
  expect(await state.getAlarm()).toBeGreaterThan(Date.now());
});
it("an alarm checks at most five due jobs and leaves future and extra jobs alone", async () => {
  for (let i = 0; i < 7; i++) await job(`rp-${i}`, now());
  await job("rp-future", now() + 3600);
  for (let i = 0; i < 5; i++) remote(`rp-${i}`);
  await remotePollAlarm(local, state);
  for (let i = 0; i < 5; i++)
    expect(JSON.parse((await row(`rp-${i}`)).payload).polls).toBe(1);
  expect(JSON.parse((await row("rp-5")).payload).polls).toBeUndefined();
  expect(JSON.parse((await row("rp-future")).payload).polls).toBeUndefined();
  expect(sent).toEqual([]);
  expect(await state.getAlarm()).toBeGreaterThanOrEqual((now() + 59) * 1000);
});
it("submits an upstream job once and subsequent checks reuse its saved id", async () => {
  await job("rp-new");
  await sql(
    "UPDATE comic_jobs SET payload=? WHERE id='rp-new'",
    JSON.stringify({
      provider: "fanqie",
      chapter_id: "rp-chapter",
      chapter: { book_id: "12345678", id: "12345679" },
    }),
  );
  fetchMock
    .get("https://fanqie.dddinmx.cn")
    .intercept({ path: "/v1/jobs", method: "POST" })
    .reply(200, { ok: true, data: { id: "rp-remote" } });
  remote("rp-remote");
  await runJob(local, "rp-new");
  expect(JSON.parse((await row("rp-new")).payload).remote_id).toBe("rp-remote");
  await sql(
    "UPDATE comic_jobs SET lease_until=0,remote_poll_at=? WHERE id='rp-new'",
    now(),
  );
  remote("rp-remote");
  await remotePollAlarm(local, state);
  expect(JSON.parse((await row("rp-new")).payload).polls).toBe(2);
  expect(sent).toEqual([]);
});
it("a completed archive is stored in R2 and creates exactly one conversion job without another Queue message", async () => {
  const bytes = zipSync({ "1.jpg": strToU8("test-image") });
  const sha = Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
  )
    .map((v) => v.toString(16).padStart(2, "0"))
    .join("");
  const key = `fanqie/12345678/${sha}.zip`;
  await bindings.LIBRARY.delete(key);
  await job("rp-done", now());
  fetchMock
    .get("https://fanqie.dddinmx.cn")
    .intercept({ path: "/v1/jobs/rp-done" })
    .reply(200, {
      ok: true,
      data: {
        status: "completed",
        artifact: {
          size: bytes.length,
          sha256: sha,
          url: "/v1/jobs/rp-done/artifact",
        },
      },
    });
  fetchMock
    .get("https://fanqie.dddinmx.cn")
    .intercept({ path: "/v1/jobs/rp-done/artifact" })
    .reply(200, bytes, { headers: { "Content-Length": String(bytes.length) } });
  await remotePollAlarm(local, state);
  expect(await row("rp-done")).toMatchObject({
    state: "waiting",
    remote_poll_at: null,
  });
  expect(
    new Uint8Array(await (await bindings.LIBRARY.get(key))!.arrayBuffer()),
  ).toEqual(bytes);
  const conversions = await bindings.DB.prepare(
    "SELECT id FROM compute_jobs WHERE kind='fanqie-archive' AND json_extract(payload,'$.parent_job')='rp-done'",
  ).all();
  expect(conversions.results).toHaveLength(1);
  await runJob(local, "rp-done");
  expect(sent).toEqual([]);
});
it("cancellation removes the due marker, stops alarms, and makes no upstream request", async () => {
  await job("rp-cancel", now());
  await sql("UPDATE comic_jobs SET state='cancelled' WHERE id='rp-cancel'");
  await remotePollAlarm(local, state);
  expect(await row("rp-cancel")).toMatchObject({
    state: "cancelled",
    remote_poll_at: null,
  });
  expect(await state.getAlarm()).toBeNull();
  expect(sent).toEqual([]);
});
it("maintenance can recover a lost wake without re-submitting or using the Queue", async () => {
  await job("rp-lost", now() + 120);
  expect(await state.getAlarm()).toBeNull();
  await recoverRemotePolls(local);
  expect(await state.getAlarm()).toBeGreaterThanOrEqual((now() + 119) * 1000);
  expect(sent).toEqual([]);
});
it("existing remote failures retry through the alarm and retain the failed-attempt cap", async () => {
  await job("rp-fail", now());
  await sql("UPDATE comic_jobs SET attempts=4 WHERE id='rp-fail'");
  fetchMock
    .get("https://fanqie.dddinmx.cn")
    .intercept({ path: "/v1/jobs/rp-fail" })
    .reply(503, "unavailable");
  await remotePollAlarm(local, state);
  expect(await row("rp-fail")).toMatchObject({
    state: "error",
    remote_poll_at: null,
    attempts: 5,
  });
  expect(sent).toEqual([]);
  expect(await state.getAlarm()).toBeNull();
});
it("a transient upstream failure parks an existing remote job rather than resubmitting it", async () => {
  await job("rp-fail", now());
  fetchMock
    .get("https://fanqie.dddinmx.cn")
    .intercept({ path: "/v1/jobs/rp-fail" })
    .reply(503, "unavailable");
  await remotePollAlarm(local, state);
  expect(await row("rp-fail")).toMatchObject({ state: "queued", attempts: 1 });
  expect((await row("rp-fail")).remote_poll_at).toBeGreaterThan(now());
  expect(sent).toEqual([]);
});
it("database pauses persist their retry time and new wakes cannot shorten the pause", async () => {
  let calls = 0;
  const paused = {
    ...local,
    DB: {
      prepare: () => {
        calls++;
        throw new DatabasePausedError(600);
      },
    },
  } as unknown as Env;
  await remotePollAlarm(paused, state);
  expect(calls).toBe(1);
  const alarm = await state.getAlarm();
  expect(alarm).toBeGreaterThanOrEqual((now() + 599) * 1000);
  await wakeRemotePolls(local, now());
  expect(await state.getAlarm()).toBe(alarm);
  expect(sent).toEqual([]);
});
it("an empty sweep stops without another alarm or a Queue message", async () => {
  await remotePollAlarm(local, state);
  expect(await state.getAlarm()).toBeNull();
  expect(sent).toEqual([]);
});
it("a wake racing with an empty final query is preserved instead of being lost", async () => {
  const db = {
    prepare: () => ({
      bind: () => ({ all: async () => ({ results: [] }) }),
      first: async () => {
        await wakeRemotePolls(local, now() + 120);
        return null;
      },
    }),
  };
  await remotePollAlarm({ ...local, DB: db } as unknown as Env, state);
  expect(await state.getAlarm()).toBeGreaterThanOrEqual((now() + 119) * 1000);
});
