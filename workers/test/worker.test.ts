import {
  env,
  SELF,
  fetchMock,
  applyD1Migrations,
  runInDurableObject,
} from "cloudflare:test";
import { zipSync, strToU8, strFromU8, unzipSync, gunzipSync } from "fflate";
import { runJob, MAX_REMOTE_POLLS } from "../src/downloads";
import { scanLibrary } from "../src/scans";
import { indexNovel } from "../src/novels";
import { decodeImages, providerFor, unpackManhuagui } from "../src/providers";
import { beforeAll, beforeEach, afterEach, describe, it, expect } from "vitest";
import worker, { processJob, verifyPassword, pilotRoute } from "../src/index";
import type { Env } from "../src/index";
import { databaseEnv } from "../src/database";
import { mediaApi } from "../src/media";
import { DatabaseBudget, databasePaused } from "../src/database-budget";
import {
  createValidationReader,
  removeValidationReader,
  cleanFailedRunReader,
} from "../scripts/validation-reader.mjs";
const bindings = env as unknown as Env & {
  TEST_MIGRATIONS: Parameters<typeof applyD1Migrations>[1];
};
const origin = "https://mangadock.test",
  password = "test-password-strong-2026";
describe("R2 control storage", () => {
  it("serves separate concurrent Durable Object request contexts with R2-backed counters", async () => {
    const { ControlStorage } = await import("../src/control-storage");
    const stub = bindings.APPLICATION.get(
      bindings.APPLICATION.idFromName("r2-separate-contexts"),
    );
    await runInDurableObject(stub, async (instance: any, ctx) => {
      instance.databaseBudget = new DatabaseBudget(
        new ControlStorage(bindings.LIBRARY, ctx.storage, ctx.id.toString()),
      );
    });
    const responses = await Promise.all(
      Array.from({ length: 12 }, () =>
        stub.fetch("https://jobs.internal/resource-status"),
      ),
    );
    expect(responses.map((response) => response.status)).toEqual(
      Array(12).fill(200),
    );
  });
  it("migrates existing quota holds exactly and works after SQLite writes and later reads fail", async () => {
    const { ControlStorage } = await import("../src/control-storage");
    const { DatabaseBudget } = await import("../src/database-budget");
    const old = {
      day: new Date().toISOString().slice(0, 10),
      requests: 31,
      statements: 71,
      blockedUntil: Date.now() + 3600000,
      failures: 3,
      failureWindow: Date.now(),
      lastFailure: { kind: "rate_limit", at: Date.now(), status: 429 },
    };
    let reads = 0;
    const legacy = {
      list: async () => {
        reads++;
        return new Map([
          ["database-outbound-budget-v1", old],
          [
            "queue-budget",
            {
              day: Math.floor(Date.now() / 86400000),
              sent: 950,
              pausedUntil: 0,
            },
          ],
        ]);
      },
      getAlarm: async () => null,
      setAlarm: async () => {
        throw new Error("SQLite row write quota exceeded");
      },
      deleteAlarm: async () => {
        throw new Error("SQLite row write quota exceeded");
      },
    } as any;
    const store = new ControlStorage(
      bindings.LIBRARY,
      legacy,
      "migrate-held-budget",
    );
    const budget = new DatabaseBudget(
      store,
      Date.now,
      { requests: 200000, statements: 200000 },
      64,
    );
    await expect(budget.reserve(1)).rejects.toThrow("数据库保护");
    expect(await store.get("database-outbound-budget-v1")).toEqual(old);
    expect((await store.get<any>("queue-budget"))?.sent).toBe(950);
    await store.setAlarm(Date.now() + 30000);
    expect(await store.getAlarm()).toBeGreaterThan(Date.now());
    const rebuilt = new ControlStorage(
      bindings.LIBRARY,
      {
        list: async () => {
          throw new Error("reads unavailable");
        },
      } as any,
      "migrate-held-budget",
    );
    expect(await rebuilt.get("database-outbound-budget-v1")).toEqual(old);
    expect(reads).toBe(1);
  });
  it("conditional R2 transactions preserve increments from independent instances", async () => {
    const { ControlStorage } = await import("../src/control-storage");
    const empty = {
      list: async () => new Map(),
      getAlarm: async () => null,
    } as any;
    const a = new ControlStorage(
        bindings.LIBRARY,
        empty,
        "concurrent-r2-counter",
      ),
      b = new ControlStorage(bindings.LIBRARY, empty, "concurrent-r2-counter");
    const increment = (store: typeof a) =>
      store.transaction(async (tx) => {
        const value = (await tx.get("count")) || 0;
        await tx.put("count", value + 1);
      });
    await Promise.all([increment(a), increment(b)]);
    expect(await a.get("count")).toBe(2);
    await a.put({ "hold/a": 1, "hold/b": 2, other: 3 });
    expect([...(await a.list({ prefix: "hold/", limit: 1 }))]).toEqual([
      ["hold/a", 1],
    ]);
    expect(await a.delete(["hold/a", "missing"])).toBe(1);
    expect((await a.get(["hold/a", "hold/b"])).size).toBe(1);
  });
  it("inaccessible original counters fail closed without creating a zeroed R2 counter", async () => {
    const { ControlStorage } = await import("../src/control-storage");
    const store = new ControlStorage(
      bindings.LIBRARY,
      {
        list: async () => {
          throw new Error("SQLite read quota exceeded");
        },
      } as any,
      "fail-closed-migration",
    );
    await expect(store.put("database-outbound-budget-v1", {})).rejects.toThrow(
      "Cloudflare 状态存储",
    );
    expect(
      await bindings.LIBRARY.head(
        "_system/control-v1/fail-closed-migration.json",
      ),
    ).toBeNull();
  });
  it("persists the provider hold once, avoids more SQLite reads after reconstruction, and preserves counters after expiry", async () => {
    const { ControlStorage } = await import("../src/control-storage");
    let reads = 0,
      unavailable = true;
    const original = {
      day: new Date().toISOString().slice(0, 10),
      requests: 97311,
      statements: 106120,
      blockedUntil: Date.now() + 10000,
      failures: 1,
      failureWindow: Date.now(),
    };
    const legacy = {
      list: async () => {
        reads++;
        if (unavailable)
          throw new Error("Exceeded rows written on Durable Objects Free");
        return new Map([["database-outbound-budget-v1", original]]);
      },
      getAlarm: async () => null,
    } as any;
    const first = new ControlStorage(
      bindings.LIBRARY,
      legacy,
      "quota-hold-fixture",
    );
    const err = await first
      .get("database-outbound-budget-v1")
      .catch((error) => error);
    expect(err.message).toContain("系统将自动复查");
    expect(err.retryAfter).toBeGreaterThan(0);
    for (let i = 0; i < 10; i++)
      await expect(first.get("database-outbound-budget-v1")).rejects.toThrow(
        "Cloudflare 状态存储",
      );
    const rebuilt = new ControlStorage(
      bindings.LIBRARY,
      legacy,
      "quota-hold-fixture",
    );
    await expect(rebuilt.get("database-outbound-budget-v1")).rejects.toThrow(
      "Cloudflare 状态存储",
    );
    expect(reads).toBe(1);
    await bindings.LIBRARY.put(
      "_system/control-quota-hold-v1.json",
      JSON.stringify({
        version: 1,
        until: Date.now() - 1,
        kind: "legacy_rows_written",
      }),
    );
    unavailable = false;
    const expired = new ControlStorage(
      bindings.LIBRARY,
      legacy,
      "quota-hold-fixture",
    );
    expect(await expired.get("database-outbound-budget-v1")).toEqual(original);
    expect(reads).toBe(2);
  });
  it("a delayed provider reset retries within a bounded grace period instead of pausing another whole day", async () => {
    const { ControlStorage } = await import("../src/control-storage");
    const reset = Date.now() - 60000;
    await bindings.LIBRARY.put(
      "_system/control-quota-hold-v1.json",
      JSON.stringify({
        version: 1,
        until: reset,
        reset_at: reset,
        kind: "legacy_rows_written",
      }),
    );
    const store = new ControlStorage(
      bindings.LIBRARY,
      {
        list: async () => {
          throw new Error("Exceeded rows written on Durable Objects Free");
        },
      } as any,
      "reset-grace-fixture",
    );
    const error = await store
      .get("database-outbound-budget-v1")
      .catch((error) => error);
    expect(error.retryAfter).toBeGreaterThan(0);
    expect(error.retryAfter).toBeLessThanOrEqual(60);
    const hold = await (await bindings.LIBRARY.get(
      "_system/control-quota-hold-v1.json",
    ))!.json<any>();
    expect(hold.reset_at).toBe(reset);
    expect(hold.until).toBeLessThanOrEqual(reset + 900000);
    expect(
      await bindings.LIBRARY.head(
        "_system/control-v1/reset-grace-fixture.json",
      ),
    ).toBeNull();
  });
  it("a reset delayed beyond fifteen minutes keeps read probes bounded without renewing a day-long pause", async () => {
    const { ControlStorage } = await import("../src/control-storage");
    const reset = Date.now() - 1200000;
    await bindings.LIBRARY.put(
      "_system/control-quota-hold-v1.json",
      JSON.stringify({
        version: 1,
        until: reset,
        reset_at: reset,
        kind: "legacy_rows_written",
        read_probe_v2: true,
      }),
    );
    let reads = 0;
    const legacy = {
      list: async () => {
        reads++;
        throw new Error("Exceeded rows written on Durable Objects Free");
      },
    } as any;
    const store = new ControlStorage(
      bindings.LIBRARY,
      legacy,
      "late-reset-probe",
    );
    const error = await store
      .get("database-outbound-budget-v1")
      .catch((error) => error);
    expect(error.retryAfter).toBeGreaterThan(0);
    expect(error.retryAfter).toBeLessThanOrEqual(900);
    const hold = await (await bindings.LIBRARY.get(
      "_system/control-quota-hold-v1.json",
    ))!.json<any>();
    expect(hold.reset_at).toBe(reset);
    expect(hold.read_probe_v2).toBe(true);
    const rebuilt = new ControlStorage(
      bindings.LIBRARY,
      legacy,
      "late-reset-probe",
    );
    await expect(rebuilt.get("database-outbound-budget-v1")).rejects.toThrow(
      "Cloudflare 状态存储",
    );
    expect(reads).toBe(1);
    expect(
      await bindings.LIBRARY.head("_system/control-v1/late-reset-probe.json"),
    ).toBeNull();
  });
  it("caps an old renewed hold once and migrates the unchanged original counters only after a successful read", async () => {
    const { ControlStorage } = await import("../src/control-storage");
    const originalUntil = Date.now() + 86400000;
    await bindings.LIBRARY.put(
      "_system/control-quota-hold-v1.json",
      JSON.stringify({
        version: 1,
        until: originalUntil,
        reset_at: originalUntil,
        kind: "legacy_rows_written",
      }),
    );
    const counters = {
      requests: 97311,
      statements: 106120,
      failures: 3,
      blockedUntil: 1792108800000,
    };
    let reads = 0;
    const legacy = {
      list: async () => {
        reads++;
        return new Map([["database-outbound-budget-v1", counters]]);
      },
      getAlarm: async () => null,
    } as any;
    const first = new ControlStorage(
      bindings.LIBRARY,
      legacy,
      "old-renewed-hold",
    );
    const error = await first
      .get("database-outbound-budget-v1")
      .catch((error) => error);
    expect(error.retryAfter).toBeLessThanOrEqual(900);
    expect(reads).toBe(0);
    const hold = await (await bindings.LIBRARY.get(
      "_system/control-quota-hold-v1.json",
    ))!.json<any>();
    expect(hold.reset_at).toBe(originalUntil);
    expect(hold.read_probe_v2).toBe(true);
    await bindings.LIBRARY.put(
      "_system/control-quota-hold-v1.json",
      JSON.stringify({ ...hold, until: Date.now() - 1 }),
    );
    const recovered = new ControlStorage(
      bindings.LIBRARY,
      legacy,
      "old-renewed-hold",
    );
    expect(await recovered.get("database-outbound-budget-v1")).toEqual(
      counters,
    );
    expect(reads).toBe(1);
  });
});
it("the real SQLite DO alarm uses the poller, not the compute wake handler", async () => {
  const stub = bindings.APPLICATION.get(
    bindings.APPLICATION.idFromName("remote-polls"),
  );
  await runInDurableObject(stub, async (instance, ctx) => {
    await ctx.storage.deleteAll();
    await ctx.storage.deleteAlarm();
    await instance.alarm();
    expect(await ctx.storage.getAlarm()).toBeNull();
    expect(await ctx.storage.get("compute-wake")).toBeUndefined();
  });
});
let encoded: string;
beforeAll(async () => {
  await applyD1Migrations(bindings.DB, bindings.TEST_MIGRATIONS);
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    "PBKDF2",
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    {
      name: "PBKDF2",
      salt: new TextEncoder().encode("test-salt"),
      iterations: 100000,
      hash: "SHA-256",
    },
    key,
    256,
  );
  encoded =
    "pbkdf2:sha256:100000$test-salt$" +
    [...new Uint8Array(bits)]
      .map((x) => x.toString(16).padStart(2, "0"))
      .join("");
});
describe("Turso HTTP driver in actual workerd", () => {
  it("runs overlapping Durable Object requests without sharing foreign request I/O", async () => {
    const stub = bindings.APPLICATION.get(
      bindings.APPLICATION.idFromName("turso-io-regression"),
    );
    await runInDurableObject(stub, async (instance: any, ctx) => {
      instance.env = databaseEnv(
        {
          ...instance.env,
          DATABASE_PROVIDER: "turso",
          TURSO_DATABASE_URL: "libsql://fixture.turso.io",
          TURSO_AUTH_TOKEN: "synthetic-token-only",
        },
        new DatabaseBudget(ctx.storage),
      );
    });
    fetchMock
      .get("https://fixture.turso.io")
      .intercept({ path: "/v2/pipeline", method: "POST" })
      .reply(200, {
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
      })
      .delay(50)
      .times(24);
    const responses = await Promise.all(
      Array.from({ length: 12 }, (_, i) =>
        stub.fetch("https://jobs.internal/run", {
          method: "POST",
          body: JSON.stringify({ id: "io-missing-" + i }),
        }),
      ),
    );
    expect(responses.map((response) => response.status)).toEqual(
      Array(12).fill(200),
    );
  });
  it("uses the same HTTP request options as the deployed Worker", async () => {
    fetchMock
      .get("https://fixture.turso.io")
      .intercept({ path: "/v2/pipeline", method: "POST" })
      .reply(200, {
        baton: null,
        base_url: null,
        results: [
          {
            type: "ok",
            response: {
              type: "execute",
              result: {
                cols: [{ name: "answer", decltype: "INTEGER" }],
                rows: [[{ type: "integer", value: "42" }]],
                affected_row_count: 0,
                last_insert_rowid: null,
              },
            },
          },
          { type: "ok", response: { type: "close" } },
        ],
      });
    const store = new Map();
    const storage = {
      get: async (key: string) => store.get(key),
      put: async (key: string, value: unknown) => {
        store.set(key, value);
      },
      transaction: async (fn: any) => fn(storage),
    };
    const budget = new DatabaseBudget(storage);
    const { DB } = databaseEnv(
      {
        DATABASE_PROVIDER: "turso",
        TURSO_DATABASE_URL: "libsql://fixture.turso.io",
        TURSO_AUTH_TOKEN: "synthetic-token-only",
      },
      budget,
    );
    expect(await DB.prepare("SELECT 42 AS answer").first()).toEqual({
      answer: 42,
    });
    await budget.reserve(10000 - 1);
    let paused: unknown;
    try {
      await DB.prepare("SELECT 42 AS answer").first();
    } catch (error) {
      paused = error;
    }
    expect(databasePaused(paused)).toBe(true);
  });
  it("refuses redirects without forwarding database credentials", async () => {
    fetchMock
      .get("https://redirect-fixture.turso.io")
      .intercept({ path: "/v2/pipeline", method: "POST" })
      .reply(302, "", {
        headers: { location: "https://untrusted.example/steal" },
      });
    const { DB } = databaseEnv({
      DATABASE_PROVIDER: "turso",
      TURSO_DATABASE_URL: "libsql://redirect-fixture.turso.io",
      TURSO_AUTH_TOKEN: "synthetic-token-only",
    });
    await expect(DB.prepare("SELECT 42").first()).rejects.toThrow(
      "redirect refused",
    );
  });
});
beforeEach(async () => {
  for (const name of [
    "login",
    "web",
    "compute",
    "scheduler",
    "remote-polls",
    "queue-budget",
  ])
    await runInDurableObject(
      bindings.APPLICATION.get(bindings.APPLICATION.idFromName(name)),
      async (_instance, state) => state.storage.deleteAll(),
    );
  fetchMock.activate();
  fetchMock.disableNetConnect();
  await bindings.DB.batch(
    [
      "storage_gc",
      "upload_parts",
      "uploads",
      "compute_jobs",
      "remote_mounts",
      "anilist_links",
      "anilist_accounts",
      "reading_reports",
      "reading_time",
      "hidden_items",
      "update_check_items",
      "update_checks",
      "novel_group_permissions",
      "group_permissions",
      "comic_progress",
      "comic_jobs",
      "chapters",
      "comics",
      "progress",
      "jobs",
      "books",
      "sessions",
      "login_logs",
      "login_attempts",
      "users",
    ].map((table) => bindings.DB.prepare(`DELETE FROM ${table}`)),
  );
  await bindings.LIBRARY.delete(
    (await bindings.LIBRARY.list()).objects.map((obj) => obj.key),
  );
  await bindings.DB.prepare(
    "DELETE FROM library_groups WHERE name!='默认分组'",
  ).run();
  await bindings.DB.prepare("DELETE FROM settings").run();
  await bindings.DB.prepare("DELETE FROM storage_ledger").run();
  await bindings.DB.prepare(
    "INSERT INTO users(id,username,password_hash,role) VALUES (?,?,?,?)",
  )
    .bind("admin", "admin", encoded, "admin")
    .run();
});
afterEach(() => {
  fetchMock.assertNoPendingInterceptors();
  fetchMock.deactivate();
});
async function login(username = "admin", pwd = password) {
  const response = await SELF.fetch(origin + "/api/v1/auth/login", {
    method: "POST",
    headers: { Origin: origin, "Content-Type": "application/json" },
    body: JSON.stringify({ username, password: pwd }),
  });
  return {
    response,
    cookie: response.headers.get("Set-Cookie")?.split(";")[0] || "",
    payload: (await response.json()) as any,
  };
}
const auth = (cookie: string, csrf: string) => ({
  Cookie: cookie,
  Origin: origin,
  "Content-Type": "application/json",
  "X-CSRFToken": csrf,
});
const artifactBytes = new TextEncoder().encode("realistic-test-artifact-bytes");
async function completedJob(overrides: Record<string, unknown> = {}) {
  const digest = [
    ...new Uint8Array(await crypto.subtle.digest("SHA-256", artifactBytes)),
  ]
    .map((x) => x.toString(16).padStart(2, "0"))
    .join("");
  return {
    id: "upstream-job",
    status: "completed",
    metadata: { title: "测试小说", author: "测试作者" },
    artifact: {
      url: "/v1/jobs/upstream-job/artifact",
      size: artifactBytes.length,
      sha256: digest,
      ...overrides,
    },
  };
}
function mock(path: string, data: unknown, method = "GET") {
  return fetchMock
    .get("https://fanqie.dddinmx.cn")
    .intercept({ path, method })
    .reply(200, JSON.stringify({ ok: true, data }), {
      headers: { "content-type": "application/json" },
    });
}
async function insertJob(state = "queued", remote: string | null = null) {
  await bindings.DB.prepare(
    "INSERT INTO jobs(id,user_id,source_id,remote_id,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
  )
    .bind(
      "job",
      "admin",
      "123456789",
      remote,
      state,
      Math.floor(Date.now() / 1000),
      Math.floor(Date.now() / 1000),
    )
    .run();
}
describe("authentication and access", () => {
  it("protects files, progress, jobs and library", async () => {
    for (const path of [
      "/api/books",
      "/api/books/123456789/file",
      "/api/books/123456789/progress",
      "/api/jobs",
    ])
      expect((await SELF.fetch(origin + path)).status).toBe(401);
  });
  it("checks passwords, secure cookie, origin, CSRF and logout", async () => {
    expect(await verifyPassword("wrong", encoded)).toBe(false);
    const { response, cookie, payload } = await login();
    expect(response.status).toBe(200);
    expect(response.headers.get("Set-Cookie")).toContain(
      "HttpOnly; Secure; SameSite=Lax",
    );
    expect(
      (
        await SELF.fetch(origin + "/api/v1/auth/me", {
          headers: { Cookie: cookie },
        })
      ).status,
    ).toBe(200);
    expect(
      (
        await SELF.fetch(origin + "/api/jobs", {
          method: "POST",
          headers: {
            Cookie: cookie,
            Origin: origin,
            "Content-Type": "application/json",
          },
          body: "{}",
        })
      ).status,
    ).toBe(403);
    expect(
      (
        await SELF.fetch(origin + "/api/v1/auth/logout", {
          method: "POST",
          headers: {
            ...auth(cookie, payload.data.csrf_token),
            Origin: "https://attacker.test",
          },
          body: "{}",
        })
      ).status,
    ).toBe(403);
    expect(
      (
        await SELF.fetch(origin + "/api/v1/auth/logout", {
          method: "POST",
          headers: auth(cookie, payload.data.csrf_token),
          body: "{}",
        })
      ).status,
    ).toBe(200);
    expect(
      (await SELF.fetch(origin + "/api/books", { headers: { Cookie: cookie } }))
        .status,
    ).toBe(401);
  });
  it("verifies legacy 260k and new 600k passwords in the Durable Object", async () => {
    for (const key of ["TEST_LEGACY_HASH", "TEST_STRONG_HASH"]) {
      await bindings.DB.prepare("UPDATE users SET password_hash=? WHERE id=?")
        .bind((env as any)[key], "admin")
        .run();
      expect((await login()).response.status).toBe(200);
    }
  });
  it("creates durable downloads and rejects active or already stored duplicates", async () => {
    const { cookie, payload } = await login(),
      sent: any[] = [];
    const testEnv = {
      ...bindings,
      DOWNLOADS: {
        send: async (body: any) => {
          sent.push(body);
        },
      } as any,
    };
    const create = () =>
      pilotRoute(
        new Request(origin + "/api/jobs", {
          method: "POST",
          headers: auth(cookie, payload.data.csrf_token),
          body: JSON.stringify({ book_id: "123456789" }),
        }),
        testEnv,
      );
    expect((await create()).status).toBe(202);
    expect(sent).toHaveLength(1);
    expect((await create()).status).toBe(409);
    await bindings.DB.prepare("UPDATE jobs SET state='error'").run();
    await bindings.DB.prepare(
      "INSERT INTO books (id,source_id,title,author,object_key,size,sha256,created_at) VALUES ('123456789','123456789','stored','author','file',4,?,?)",
    )
      .bind("0".repeat(64), Math.floor(Date.now() / 1000))
      .run();
    expect((await create()).status).toBe(409);
    expect(sent).toHaveLength(1);
  });
  it("protects one-time source initialization and stores no credential in D1", async () => {
    const { cookie, payload } = await login();
    const request = () =>
      new Request(origin + "/api/source/register", {
        method: "POST",
        headers: auth(cookie, payload.data.csrf_token),
        body: JSON.stringify({
          instance: "edge-instance",
          registration: "fqr_" + "r".repeat(43),
        }),
      });
    expect((await pilotRoute(request(), bindings)).status).toBe(409);
    const fakeToken = "fqk_" + "a".repeat(43);
    mock("/v1/register", { token: fakeToken }, "POST");
    const response = await pilotRoute(request(), {
      ...bindings,
      FANQIE_API_TOKEN: undefined,
    });
    expect(response.status).toBe(200);
    expect(((await response.json()) as any).data.token).toBe(fakeToken);
  });
  it("rate limits before the metered database", async () => {
    for (let i = 0; i < 5; i++)
      expect((await login("admin", "wrong")).response.status).toBe(401);
    expect((await login()).response.status).toBe(429);
  });
  it("restricts download to admin", async () => {
    await bindings.DB.prepare(
      "INSERT INTO users (id,username,password_hash,role) VALUES ('reader','reader',?,'user')",
    )
      .bind(encoded)
      .run();
    const { cookie, payload } = await login("reader");
    expect(
      (
        await SELF.fetch(origin + "/api/jobs", {
          method: "POST",
          headers: auth(cookie, payload.data.csrf_token),
          body: JSON.stringify({ book_id: "123456789" }),
        })
      ).status,
    ).toBe(403);
  });
});
describe("source → streaming R2 → reading → D1 progress", () => {
  it("stores artifact, serves bytes/ranges and preserves newest per-user progress", async () => {
    await insertJob();
    mock("/v1/jobs", { id: "upstream-job" }, "POST");
    mock("/v1/jobs/upstream-job", await completedJob());
    fetchMock
      .get("https://fanqie.dddinmx.cn")
      .intercept({
        path: "/v1/jobs/upstream-job/artifact",
        headers: { authorization: "Bearer test-token" },
      })
      .reply(200, artifactBytes, {
        headers: { "content-length": String(artifactBytes.length) },
      });
    await processJob(bindings, "job");
    expect(
      (
        await bindings.DB.prepare("SELECT state FROM jobs WHERE id=?")
          .bind("job")
          .first<any>()
      )?.state,
    ).toBe("completed");
    const { cookie, payload } = await login(),
      headers = auth(cookie, payload.data.csrf_token);
    const file = await SELF.fetch(origin + "/api/books/123456789/file", {
      headers: { Cookie: cookie },
    });
    expect(file.status).toBe(200);
    expect(new Uint8Array(await file.arrayBuffer())).toEqual(artifactBytes);
    const range = await SELF.fetch(origin + "/api/books/123456789/file", {
      headers: { Cookie: cookie, Range: "bytes=0-3" },
    });
    expect(range.status).toBe(206);
    expect(await range.text()).toBe("real");
    const ms = Date.now();
    for (const data of [
      { chapter: 1, page: 2, scroll: 0, client_ms: ms },
      { chapter: 0, page: 0, scroll: 0, client_ms: ms - 10 },
    ])
      expect(
        (
          await SELF.fetch(origin + "/api/books/123456789/progress", {
            method: "PUT",
            headers,
            body: JSON.stringify(data),
          })
        ).status,
      ).toBe(200);
    const progress = (await (
      await SELF.fetch(origin + "/api/books/123456789/progress", {
        headers: { Cookie: cookie },
      })
    ).json()) as any;
    expect(progress.data.chapter).toBe(1);
    expect(progress.data.page).toBe(2);
    await bindings.DB.prepare(
      "INSERT INTO users (id,username,password_hash,role) VALUES ('other','other',?,'user')",
    )
      .bind(encoded)
      .run();
    const other = await login("other");
    expect(
      (
        (await (
          await SELF.fetch(origin + "/api/books/123456789/progress", {
            headers: { Cookie: other.cookie },
          })
        ).json()) as any
      ).data,
    ).toBeNull();
    expect(
      (
        await SELF.fetch(origin + "/api/books/123456789/progress", {
          method: "PUT",
          headers,
          body: JSON.stringify({
            chapter: -1,
            page: 0,
            scroll: 0,
            client_ms: ms,
          }),
        })
      ).status,
    ).toBe(400);
    await processJob(bindings, "job");
    expect(
      (
        await bindings.DB.prepare(
          "SELECT count(*) AS n FROM books",
        ).first<any>()
      )?.n,
    ).toBe(1);
  });
  it("rejects checksum mismatch before publication", async () => {
    await insertJob("running", "upstream-job");
    mock(
      "/v1/jobs/upstream-job",
      await completedJob({ sha256: "0".repeat(64) }),
    );
    fetchMock
      .get("https://fanqie.dddinmx.cn")
      .intercept({ path: "/v1/jobs/upstream-job/artifact" })
      .reply(200, artifactBytes);
    await processJob(bindings, "job");
    expect(
      await bindings.DB.prepare("SELECT id FROM books").first(),
    ).toBeNull();
    expect(
      (await bindings.DB.prepare("SELECT state FROM jobs").first<any>())?.state,
    ).toBe("error");
  });
  it("rejects artifact URLs from other hosts", async () => {
    await insertJob("running", "upstream-job");
    mock(
      "/v1/jobs/upstream-job",
      await completedJob({ url: "https://attacker.test/token" }),
    );
    await processJob(bindings, "job");
    expect(
      (await bindings.DB.prepare("SELECT state FROM jobs").first<any>())?.state,
    ).toBe("error");
  });
  it("rejects oversize artifacts before transfer", async () => {
    await insertJob("running", "upstream-job");
    mock("/v1/jobs/upstream-job", await completedJob({ size: 2147483649 }));
    await processJob(bindings, "job");
    expect(
      await bindings.DB.prepare("SELECT id FROM books").first(),
    ).toBeNull();
    expect(
      (await bindings.DB.prepare("SELECT state FROM jobs").first<any>())?.state,
    ).toBe("error");
  });
  it("enforces the library storage budget before transferring bytes", async () => {
    await insertJob("running", "upstream-job");
    mock("/v1/jobs/upstream-job", await completedJob());
    await processJob({ ...bindings, MAX_LIBRARY_BYTES: "10" }, "job");
    expect(
      await bindings.DB.prepare("SELECT id FROM books").first(),
    ).toBeNull();
    expect(
      (await bindings.DB.prepare("SELECT message FROM jobs").first<any>())
        ?.message,
    ).toContain("免费预算上限");
  });
  it("keeps pending work while upstream generates EPUB", async () => {
    await insertJob("running", "upstream-job");
    mock("/v1/jobs/upstream-job", { id: "upstream-job", status: "running" });
    await processJob(bindings, "job");
    expect(
      await bindings.DB.prepare(
        "SELECT state,lease_until,attempts FROM jobs",
      ).first(),
    ).toMatchObject({ state: "running", attempts: 1 });
    expect(
      (await bindings.DB.prepare("SELECT lease_until FROM jobs").first<{
        lease_until: number;
      }>())!.lease_until,
    ).toBeGreaterThanOrEqual(Math.floor(Date.now() / 1000) + 59);
    // Duplicate delivery during the remote poll delay must not query upstream again.
    await processJob(bindings, "job");
    expect(
      (await bindings.DB.prepare("SELECT attempts FROM jobs").first<{
        attempts: number;
      }>())!.attempts,
    ).toBe(1);
  });
  it("stops pending novels after the remote poll cap instead of re-enqueueing forever", async () => {
    await insertJob("running", "upstream-job");
    await bindings.DB.prepare("UPDATE jobs SET attempts=? WHERE id='job'")
      .bind(MAX_REMOTE_POLLS - 1)
      .run();
    mock("/v1/jobs/upstream-job", { id: "upstream-job", status: "running" });
    let sent = 0;
    await processJob(
      {
        ...bindings,
        DOWNLOADS: {
          send: async () => {
            sent++;
          },
        } as unknown as Queue,
      },
      "job",
    );
    expect(
      await bindings.DB.prepare(
        "SELECT state,attempts FROM jobs WHERE id='job'",
      ).first(),
    ).toMatchObject({ state: "error", attempts: MAX_REMOTE_POLLS });
    expect(sent).toBe(0);
    await processJob(bindings, "job");
    expect(
      (await bindings.DB.prepare(
        "SELECT attempts FROM jobs WHERE id='job'",
      ).first<{ attempts: number }>())!.attempts,
    ).toBe(MAX_REMOTE_POLLS);
  });
  it("does not retry uncertain upstream submissions", async () => {
    await insertJob();
    fetchMock
      .get("https://fanqie.dddinmx.cn")
      .intercept({ path: "/v1/jobs", method: "POST" })
      .reply(503, "unavailable");
    await processJob(bindings, "job");
    await processJob(bindings, "job");
    expect(
      (await bindings.DB.prepare("SELECT state FROM jobs").first<any>())?.state,
    ).toBe("error");
  });
});

const fullGet = (path: string, cookie: string) =>
  SELF.fetch(origin + path, { headers: { Cookie: cookie } });
async function comicFixture() {
  await bindings.DB.prepare(
    "INSERT INTO comics(id,name,provider,source_url,description,created_at,updated_at) VALUES ('c_test','原版漫画','baozimh_org','https://baozimh.org/manga/test','作品简介',?,?)",
  )
    .bind(Math.floor(Date.now() / 1000), Math.floor(Date.now() / 1000))
    .run();
  await bindings.DB.prepare(
    "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,manifest_key,page_count,state) VALUES ('ch_test','c_test',0,'第一话','remote-chapter','fixture/manifest',1,'ready')",
  ).run();
  await bindings.LIBRARY.put(
    "fixture/manifest",
    JSON.stringify([{ key: "fixture/page", width: 3, height: 4 }]),
    { httpMetadata: { contentType: "application/json" } },
  );
  await bindings.LIBRARY.put(
    "fixture/page",
    new Uint8Array([137, 80, 78, 71]),
    { httpMetadata: { contentType: "image/png" } },
  );
}
async function pauseTestQueue() {
  await runInDurableObject(
    bindings.APPLICATION.get(bindings.APPLICATION.idFromName("queue-budget")),
    async (_instance, state) => {
      await state.storage.put("queue-budget", {
        day: Math.floor(Date.now() / 86400000),
        sent: 0,
        pausedUntil: Math.floor(Date.now() / 1000) + 3600,
      });
    },
  );
}
it("manual update returns the original singular task id and task used by the frontend", async () => {
  await comicFixture();
  await pauseTestQueue();
  const { cookie, payload } = await login();
  const response = await SELF.fetch(origin + "/api/v1/updates", {
    method: "POST",
    headers: auth(cookie, payload.data.csrf_token),
    body: JSON.stringify({ comic_name: "原版漫画", format: "2" }),
  });
  const result = await response.json<any>();
  expect(result.ok).toBe(true);
  expect(result.data.task_id).toBeTypeOf("string");
  expect(response.status).toBe(201);
  expect(result.data.task_ids).toEqual([result.data.task_id]);
  expect(result.data.task.id).toBe(result.data.task_id);
  const job = await bindings.DB.prepare(
    "SELECT payload FROM comic_jobs WHERE id=?",
  )
    .bind(result.data.task_id)
    .first<any>();
  expect(JSON.parse(job.payload).is_update).toBe(true);
  const repeat = await SELF.fetch(origin + "/api/v1/updates", {
    method: "POST",
    headers: auth(cookie, payload.data.csrf_token),
    body: JSON.stringify({ comic_name: "原版漫画", format: "2" }),
  });
  expect(repeat.status).toBe(409);
  expect(
    (
      await bindings.DB.prepare(
        "SELECT COUNT(*) AS n FROM comic_jobs WHERE kind='source'",
      ).first<any>()
    ).n,
  ).toBe(1);
});
it("a single manual update preserves commas in comic names and rejects an empty selection", async () => {
  await comicFixture();
  await pauseTestQueue();
  await bindings.DB.prepare(
    "UPDATE comics SET name='One, Two' WHERE id='c_test'",
  ).run();
  const { cookie, payload } = await login();
  const send = (body: unknown) =>
    SELF.fetch(origin + "/api/v1/updates", {
      method: "POST",
      headers: auth(cookie, payload.data.csrf_token),
      body: JSON.stringify(body),
    });
  expect((await send({ comic_name: "One, Two", format: 2 })).status).toBe(201);
  expect((await send({ comic_name: "" })).status).toBe(400);
});
async function novelFixture(indexed = true) {
  const zip = zipSync({
    "META-INF/container.xml": strToU8(
      '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>',
    ),
    "book.opf": strToU8(
      '<package><manifest><item id="a" href="one.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest><spine><itemref idref="a"/></spine></package>',
    ),
    "nav.xhtml": strToU8(
      '<html><body><nav><a href="one.xhtml">第一章</a></nav></body></html>',
    ),
    "one.xhtml": strToU8(
      "<html><body><h1>第一章</h1><p>原文内容</p><script>bad()</script><p>原文内容</p></body></html>",
    ),
  });
  await bindings.LIBRARY.put("fixture/book", zip);
  await bindings.DB.prepare(
    "INSERT INTO books(id,source_id,title,author,object_key,size,sha256,created_at) VALUES ('123456789','123456789','原版小说','作者','fixture/book',?,?,?)",
  )
    .bind(zip.length, "1".repeat(64), Math.floor(Date.now() / 1000))
    .run();
  if (indexed) {
    await bindings.LIBRARY.put(
      "fixture/novel-chapter",
      JSON.stringify({
        index: 0,
        title: "第一章",
        paragraphs: ["第一章", "原文内容"],
        total_chapters: 1,
      }),
    );
    await bindings.LIBRARY.put(
      "fixture/novel-index",
      JSON.stringify({
        total_chapters: 1,
        chapters: [{ index: 0, title: "第一章", key: "fixture/novel-chapter" }],
      }),
    );
    await bindings.DB.prepare(
      "UPDATE books SET index_key='fixture/novel-index',chapter_count=1 WHERE id='123456789'",
    ).run();
  }
}
describe("full legacy routes and permissions", () => {
  it("renders original home, shelves, settings, admin pages and readers", async () => {
    await comicFixture();
    await novelFixture();
    const { cookie } = await login();
    for (const path of [
      "/",
      "/comics",
      "/comics/groups",
      "/search?q=原版",
      "/history",
      "/settings",
      "/statistics",
      "/users",
      "/users/admin",
      "/change_password",
      "/download",
      "/update",
      "/tasks",
      "/novels",
      "/novels/library",
      "/novels/groups",
      "/novels/search?q=原版",
      "/novels/history",
      "/novel/123456789",
      "/novel/123456789/read",
      "/comic/c_test",
      "/reader/c_test",
      "/settings/webdav",
      "/settings/scan_paths",
      "/anilist/connect",
    ]) {
      const response = await fullGet(path, cookie);
      expect(response.status, path).toBe(200);
      expect(response.headers.get("Content-Type")).toContain("text/html");
      expect(await response.text()).not.toContain("接口尚未迁移");
    }
  });
  it("enforces comic groups on shelf, detail, chapters, page lists, bytes, progress and covers", async () => {
    await comicFixture();
    await bindings.DB.prepare(
      "INSERT INTO users(id,username,password_hash,role) VALUES ('reader','reader',?,'user')",
    )
      .bind(encoded)
      .run();
    const { cookie } = await login("reader");
    expect(
      ((await (await fullGet("/api/comics", cookie)).json()) as any).items,
    ).toHaveLength(0);
    for (const path of [
      "/comic/c_test",
      "/reader/c_test",
      "/api/chapters/c_test",
      "/api/comics/c_test",
      "/api/comics/c_test/chapters/ch_test/pages",
      "/api/comics/c_test/chapters/ch_test/pages/0",
      "/get_progress/原版漫画",
      "/static/cover/原版漫画.jpg",
      "/api/comic-pages/原版漫画/ch_test.cbz?page=0",
    ])
      expect((await fullGet(path, cookie)).status, path).toBe(403);
    await bindings.DB.prepare(
      "INSERT INTO group_permissions VALUES ('reader','默认分组')",
    ).run();
    expect(
      ((await (await fullGet("/api/comics", cookie)).json()) as any).items,
    ).toHaveLength(1);
    expect(
      (await fullGet("/api/comics/c_test/chapters/ch_test/pages/0", cookie))
        .status,
    ).toBe(200);
  });
  it("supports Basic Auth clients without exposing unauthenticated files", async () => {
    await comicFixture();
    const headers = { Authorization: "Basic " + btoa("admin:" + password) };
    expect((await SELF.fetch(origin + "/api/comics", { headers })).status).toBe(
      200,
    );
    expect(
      (
        await SELF.fetch(
          origin + "/api/comics/c_test/chapters/ch_test/pages/0",
          { headers },
        )
      ).status,
    ).toBe(200);
    expect(
      (await SELF.fetch(origin + "/api/comics/c_test/chapters/ch_test/pages/0"))
        .status,
    ).toBe(401);
  });
  it("runs the whole-book verifier reader lifecycle against actual account, R2 and progress routes", async () => {
    await comicFixture();
    const session = await login();
    const request = async (path: string, method = "GET", body?: any) => {
      const response = await SELF.fetch(origin + path, {
        method,
        headers: auth(session.cookie, session.payload.data.csrf_token),
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      expect(response.status, await response.clone().text()).toBeLessThan(300);
      return response;
    };
    const input = {
      username: "md-verify-12345678-abc",
      password: "strong-verifier-password",
      role: "user",
      group_names: ["默认分组"],
    };
    const user = await createValidationReader(request, input);
    const reader = await login(input.username, input.password);
    expect(reader.payload.data.user.role).toBe("user");
    expect(reader.payload.data.user.id).toBe(user.id);
    expect(
      (
        await fullGet(
          "/api/comics/c_test/chapters/ch_test/pages/0",
          reader.cookie,
        )
      ).status,
    ).toBe(200);
    expect(
      (
        await SELF.fetch(origin + "/api/comics/c_test/progress", {
          method: "POST",
          headers: auth(reader.cookie, reader.payload.data.csrf_token),
          body: JSON.stringify({
            chapter_index: 0,
            page_index: 0,
            scroll_position: 600,
            client_progress_ms: Date.now(),
          }),
        })
      ).status,
    ).toBe(200);
    expect(
      (
        (await (
          await fullGet("/api/comics/c_test/progress", reader.cookie)
        ).json()) as any
      ).progress.scroll_position,
    ).toBe(600);
    expect(
      await bindings.DB.prepare(
        "SELECT 1 FROM comic_progress WHERE user_id='admin'",
      ).first(),
    ).toBeNull();
    await removeValidationReader(request, user);
    expect(
      await bindings.DB.prepare("SELECT 1 FROM users WHERE username=?")
        .bind(input.username)
        .first(),
    ).toBeNull();
    expect((await fullGet("/api/comics", reader.cookie)).status).toBe(401);
  }, 15000);
  it("cleans the exact newly created reader even when the account response is malformed", async () => {
    const session = await login();
    const request = async (path: string, method = "GET", body?: any) => {
      const response = await SELF.fetch(origin + path, {
        method,
        headers: auth(session.cookie, session.payload.data.csrf_token),
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      expect(response.status, await response.clone().text()).toBeLessThan(300);
      if (path === "/api/v1/users" && method === "POST") {
        const payload = (await response.json()) as any;
        return Response.json({ data: { id: payload.data.id } });
      }
      return response;
    };
    const input = {
      username: "md-verify-87654321-def",
      password: "strong-verifier-password",
      role: "user",
    };
    try {
      await expect(createValidationReader(request, input)).rejects.toThrow(
        "expected ordinary reader",
      );
    } finally {
      await removeValidationReader(request, { username: input.username });
    }
    expect(
      await bindings.DB.prepare("SELECT 1 FROM users WHERE username=?")
        .bind(input.username)
        .first(),
    ).toBeNull();
    expect(
      await bindings.DB.prepare("SELECT 1 FROM users WHERE id='admin'").first(),
    ).not.toBeNull();
  }, 15000);
  it("limits orphan cleanup to the exact ordinary verifier account identified in failed DELETE telemetry", async () => {
    const session = await login();
    for (const [id, username] of [
      ["1792030434398631", "md-verify-12345678-abc"],
      ["1792030438000002", "original-reader"],
      ["1792030440000001", "md-verify-87654321-def"],
    ])
      await bindings.DB.prepare(
        "INSERT INTO users(id,username,password_hash,role) VALUES (?,?,?,'user')",
      )
        .bind(id, username, "unused")
        .run();
    const request = async (path: string, method = "GET", body?: any) => {
      const response = await SELF.fetch(origin + path, {
        method,
        headers: auth(session.cookie, session.payload.data.csrf_token),
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      expect(response.status, await response.clone().text()).toBeLessThan(300);
      return response;
    };
    expect(await cleanFailedRunReader(request)).toBe(1);
    expect(await cleanFailedRunReader(request)).toBe(0);
    expect(
      await bindings.DB.prepare(
        "SELECT username FROM users WHERE id='1792030438000002'",
      ).first(),
    ).toEqual({ username: "original-reader" });
    expect(
      await bindings.DB.prepare(
        "SELECT username FROM users WHERE id='1792030440000001'",
      ).first(),
    ).toEqual({ username: "md-verify-87654321-def" });
  }, 15000);
  it("accepts an authenticated bodyless user DELETE while rejecting invalid CSRF and malformed JSON", async () => {
    const session = await login(),
      headers = auth(session.cookie, session.payload.data.csrf_token);
    const created = await SELF.fetch(origin + "/api/v1/users", {
      method: "POST",
      headers,
      body: JSON.stringify({
        username: "delete-empty-reader",
        password: "strong-reader-password",
        role: "user",
      }),
    });
    const id = ((await created.json()) as any).data.user.id;
    const url = origin + "/api/v1/users/" + id;
    expect(
      (
        await SELF.fetch(url, {
          method: "DELETE",
          headers: auth(session.cookie, "invalid"),
        })
      ).status,
    ).toBe(403);
    expect(
      (await SELF.fetch(url, { method: "DELETE", headers, body: "{" })).status,
    ).toBe(400);
    expect(
      await bindings.DB.prepare("SELECT 1 FROM users WHERE id=?")
        .bind(String(id))
        .first(),
    ).not.toBeNull();
    expect((await SELF.fetch(url, { method: "DELETE", headers })).status).toBe(
      200,
    );
    expect(
      await bindings.DB.prepare("SELECT 1 FROM users WHERE id=?")
        .bind(String(id))
        .first(),
    ).toBeNull();
  }, 15000);
  it("creates users, assigns groups and revokes sessions on password reset", async () => {
    const adminSession = await login(),
      headers = auth(adminSession.cookie, adminSession.payload.data.csrf_token);
    const response = await SELF.fetch(origin + "/api/v1/users", {
      method: "POST",
      headers,
      body: JSON.stringify({
        username: "new-reader",
        password: "new-strong-password",
        role: "user",
        groups: ["默认分组"],
      }),
    });
    expect(response.status, await response.clone().text()).toBe(201);
    const target = ((await response.json()) as any).data.user;
    expect(target.password_hash).toBeUndefined();
    const reader = await login("new-reader", "new-strong-password");
    expect(reader.response.status).toBe(200);
    expect(
      (
        await SELF.fetch(origin + "/users/" + target.id + "/password", {
          method: "POST",
          redirect: "manual",
          headers,
          body: JSON.stringify({
            password: "reset-strong-password",
            confirm_password: "reset-strong-password",
          }),
        })
      ).status,
    ).toBe(303);
    expect((await fullGet("/api/books", reader.cookie)).status).toBe(401);
    expect(
      (await login("new-reader", "reset-strong-password")).response.status,
    ).toBe(200);
  }, 15000);
  it("preserves image pagination contract, actual bytes and CBZ page order", async () => {
    await comicFixture();
    const { cookie } = await login(),
      chapters = (await (
        await fullGet("/api/chapters/c_test", cookie)
      ).json()) as any;
    expect(chapters.chapters[0].filename).toBe("ch_test.cbz");
    const manifest = (await (
      await fullGet("/api/comic-pages/原版漫画/ch_test.cbz", cookie)
    ).json()) as any;
    expect(manifest).toEqual({ pages: 1, dimensions: [[3, 4]] });
    const page = await fullGet(
      "/api/comic-pages/原版漫画/ch_test.cbz?page=0",
      cookie,
    );
    expect(page.headers.get("Content-Type")).toBe("image/png");
    expect(new Uint8Array(await page.arrayBuffer())).toEqual(
      new Uint8Array([137, 80, 78, 71]),
    );
    const file = await fullGet(
      "/api/comics/c_test/chapters/ch_test/file",
      cookie,
    );
    expect(
      Object.keys(unzipSync(new Uint8Array(await file.arrayBuffer()))),
    ).toEqual(["00001.png"]);
  });
  it("queues fresh EPUB indexing without reading or unzipping the input", async () => {
    await novelFixture(false);
    await expect(indexNovel(bindings, "123456789")).rejects.toMatchObject({
      status: 503,
    });
    await expect(indexNovel(bindings, "123456789")).rejects.toMatchObject({
      status: 503,
    });
    const jobs = await bindings.DB.prepare(
      "SELECT kind,payload FROM compute_jobs WHERE kind='index-epub'",
    ).all<any>();
    expect(jobs.results).toHaveLength(1);
    expect(JSON.parse(jobs.results[0].payload)).toMatchObject({
      book_id: "123456789",
      sha256: "1".repeat(64),
    });
    expect(
      (await bindings.LIBRARY.list({ prefix: "novels/" })).objects,
    ).toHaveLength(0);
  });
  it("reads already generated indexes and paragraphs without conversion", async () => {
    await novelFixture();
    const index = await indexNovel(bindings, "123456789");
    expect(index.chapters[0].title).toBe("第一章");
    const chapter = await (await bindings.LIBRARY.get(
      index.chapters[0].key,
    ))!.json<any>();
    expect(chapter.paragraphs).toEqual(["第一章", "原文内容"]);
    expect(JSON.stringify(chapter)).not.toContain("bad()");
  });
  it("deduplicates reading time and rejects old comic progress snapshots", async () => {
    await comicFixture();
    const { cookie, payload } = await login(),
      headers = auth(cookie, payload.data.csrf_token),
      stamp = Date.now();
    for (const [seconds, clock, page] of [
      [61, stamp, 2],
      [61, stamp + 1, 2],
      [30, stamp - 1, 1],
      [90, stamp + 2, 3],
    ])
      expect(
        (
          await SELF.fetch(origin + "/save_progress", {
            method: "POST",
            headers,
            body: JSON.stringify({
              comic_name: "原版漫画",
              chapter: 0,
              page,
              scroll_position: 12,
              client_progress_ms: clock,
              reading_time: seconds,
              reading_session_id: "test-session",
            }),
          })
        ).status,
      ).toBe(200);
    const progress = (await (
      await fullGet("/get_progress/原版漫画", cookie)
    ).json()) as any;
    expect(progress.last_page).toBe(3);
    expect(
      (
        await bindings.DB.prepare(
          "SELECT SUM(seconds) AS total FROM reading_time",
        ).first<any>()
      )?.total,
    ).toBe(90);
  });
  it("rechecks download capability and respects adult source controls", async () => {
    const { cookie, payload } = await login(),
      headers = auth(cookie, payload.data.csrf_token);
    expect(
      (
        await SELF.fetch(origin + "/api/v1/downloads", {
          method: "POST",
          headers,
          body: JSON.stringify({
            url: "https://www.kalamanhua.com/comic/100.html",
          }),
        })
      ).status,
    ).toBe(403);
    await bindings.DB.prepare(
      "INSERT INTO users(id,username,password_hash,role,can_download) VALUES ('reader','reader',?,'user',1)",
    )
      .bind(encoded)
      .run();
    const reader = await login("reader");
    expect(
      (
        await SELF.fetch(origin + "/api/v1/downloads", {
          method: "POST",
          headers: auth(reader.cookie, reader.payload.data.csrf_token),
          body: JSON.stringify({ url: "https://baozimh.org/manga/test" }),
        })
      ).status,
    ).toBe(403);
  });
  it("blocks non-admin group, user, settings and cloud compute writes", async () => {
    await bindings.DB.prepare(
      "INSERT INTO users(id,username,password_hash,role) VALUES ('reader','reader',?,'user')",
    )
      .bind(encoded)
      .run();
    const { cookie, payload } = await login("reader"),
      headers = auth(cookie, payload.data.csrf_token);
    for (const path of [
      "/api/v1/groups",
      "/api/v1/users",
      "/api/admin/compute/discover",
    ])
      expect(
        (
          await SELF.fetch(origin + path, {
            method: "POST",
            headers,
            body: "{}",
          })
        ).status,
      ).toBe(403);
    expect((await fullGet("/users", cookie)).status).toBe(403);
    expect((await fullGet("/api/admin/compute/jobs", cookie)).status).toBe(403);
  });
});

describe("comic downloads, cloud compute, migration and remote reading", () => {
  it("runs a durable comic source → pages → private R2 → ready chapter pipeline", async () => {
    const { cookie } = await login();
    const now = Math.floor(Date.now() / 1000),
      sent: any[] = [];
    const testEnv = {
      ...bindings,
      DOWNLOADS: {
        send: async (value: any) => {
          sent.push(value);
        },
        sendBatch: async (items: any[]) =>
          sent.push(...items.map((item) => item.body)),
      } as any,
    };
    await bindings.DB.prepare(
      "INSERT INTO comic_jobs(id,user_id,kind,payload,created_at,updated_at) VALUES ('source','admin','source',?,?,?)",
    )
      .bind(
        JSON.stringify({
          url: "https://baozimh.org/manga/test",
          provider: "baozimh_org",
          group: "默认分组",
          format: 2,
          max_chapters: 1,
        }),
        now,
        now,
      )
      .run();
    fetchMock
      .get("https://baozimh.org")
      .intercept({ path: "/manga/test" })
      .reply(200, '<h1>队列漫画</h1><div data-mid="42"></div>');
    fetchMock
      .get("https://v2.apikk.top")
      .intercept({ path: "/api/manga/get?mid=42&mode=all" })
      .reply(
        200,
        JSON.stringify({
          code: 200,
          data: {
            title: "队列漫画",
            chapters: [{ id: "7", attributes: { order: 1, title: "第一话" } }],
          },
        }),
      );
    await runJob(testEnv, "source");
    const chapterJob = sent.shift();
    expect(chapterJob.kind).toBe("comic");
    fetchMock
      .get("https://v2.apikk.top")
      .intercept({ path: "/api/v2/chapter/getinfo?m=42&c=7" })
      .reply(
        200,
        JSON.stringify({
          code: 200,
          data: {
            info: {
              images: {
                line: 2,
                images: [
                  { url: "/one.png", order: 1 },
                  { url: "/two.png", order: 2 },
                ],
              },
            },
          },
        }),
      );
    await runJob(testEnv, chapterJob.id);
    const png = Uint8Array.from(
      atob(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aGqkAAAAASUVORK5CYII=",
      ),
      (c) => c.charCodeAt(0),
    );
    for (const path of ["/one.png", "/two.png"])
      fetchMock
        .get("https://c-nd2-1.6wm.top")
        .intercept({ path })
        .reply(200, png, {
          headers: {
            "content-type": "image/png",
            "content-length": String(png.length),
          },
        });
    expect(sent).toHaveLength(1);
    expect(sent[0].kind).toBe("comic-pages");
    for (const body of sent)
      await worker.queue(
        {
          messages: [
            {
              body,
              attempts: 1,
              ack: () => {},
              retry: () => {
                throw new Error("Page batch must succeed");
              },
            },
          ],
        } as any,
        testEnv,
      );
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='source'",
        ).first<any>()
      ).state,
    ).toBe("completed");
    const comic = await bindings.DB.prepare(
        "SELECT id FROM comics",
      ).first<any>(),
      chapter = await bindings.DB.prepare(
        "SELECT id FROM chapters",
      ).first<any>();
    const pages = (await (
      await fullGet(
        `/api/comics/${comic.id}/chapters/${chapter.id}/pages`,
        cookie,
      )
    ).json()) as any;
    expect(pages.total_pages).toBe(2);
    const first = await fullGet(pages.pages[0].image_url, cookie);
    expect(new Uint8Array(await first.arrayBuffer())).toEqual(png);
  });
  it("loads a 50-comic page with a bounded number of progress queries", async () => {
    await bindings.DB.batch(
      Array.from({ length: 50 }, (_, i) =>
        bindings.DB.prepare(
          "INSERT INTO comics(id,name,provider,source_url,created_at,updated_at) VALUES (?,?,'import','',0,0)",
        ).bind("batch-" + i, "Batch " + i),
      ),
    );
    await bindings.DB.prepare(
      "INSERT INTO comic_progress(user_id,comic_id,chapter,page,client_ms,updated_at) VALUES ('admin','batch-5',7,3,1,1)",
    ).run();
    let queries = 0;
    const DB = {
      prepare: (sql: string) => {
        queries++;
        return bindings.DB.prepare(sql);
      },
      batch: bindings.DB.batch.bind(bindings.DB),
    };
    const response = await mediaApi(
      new Request(origin + "/api/comics?page_size=50"),
      { ...bindings, DB },
      { user_id: "admin", role: "admin" } as any,
    );
    const body = (await response!.json()) as any;
    expect(body.items).toHaveLength(50);
    expect(
      body.items.find((item: any) => item.id === "batch-5").progress,
    ).toMatchObject({ chapter_index: 7, page_index: 3 });
    expect(body.items.filter((item: any) => item.progress)).toHaveLength(1);
    expect(queries).toBeLessThan(6);
  });
  it("accepts only raster image bytes and leaves corrupt downloads unpublished", async () => {
    const now = Math.floor(Date.now() / 1000);
    await comicFixture();
    await bindings.DB.prepare(
      "INSERT INTO comic_jobs(id,user_id,comic_id,kind,payload,created_at,updated_at) VALUES ('bad','admin','c_test','page',?,?,?)",
    )
      .bind(
        JSON.stringify({
          url: "https://c-nd2-1.6wm.top/evil.svg",
          key: "fixture/evil",
          referer: "https://baozimh.org/",
        }),
        now,
        now,
      )
      .run();
    fetchMock
      .get("https://c-nd2-1.6wm.top")
      .intercept({ path: "/evil.svg" })
      .reply(200, "<svg><script>bad()</script></svg>", {
        headers: { "content-type": "image/svg+xml" },
      });
    await runJob(
      { ...bindings, DOWNLOADS: { send: async () => {} } as any },
      "bad",
    );
    expect(await bindings.LIBRARY.head("fixture/evil")).toBeNull();
  });
  it("bounds Fanqie comic polling independently of failed attempts and honours the poll lease", async () => {
    await comicFixture();
    const time = Math.floor(Date.now() / 1000);
    const data = {
      provider: "fanqie",
      remote_id: "remote",
      polls: 0,
      chapter: { book_id: "12345678", id: "12345679" },
    };
    await bindings.DB.prepare(
      "INSERT INTO comic_jobs(id,user_id,comic_id,kind,payload,created_at,updated_at) VALUES ('poll','admin','c_test','chapter',?,?,?)",
    )
      .bind(JSON.stringify(data), time, time)
      .run();
    let sent = 0;
    const testEnv = {
      ...bindings,
      DOWNLOADS: {
        send: async () => {
          sent++;
        },
      } as unknown as Queue,
    };
    fetchMock
      .get("https://fanqie.dddinmx.cn")
      .intercept({ path: "/v1/jobs/remote" })
      .reply(200, { ok: true, data: { status: "running" } });
    await runJob(testEnv, "poll");
    const row = await bindings.DB.prepare(
      "SELECT * FROM comic_jobs WHERE id='poll'",
    ).first<any>();
    expect(row.attempts).toBe(0);
    expect(JSON.parse(row.payload).polls).toBe(1);
    expect(row.lease_until).toBeGreaterThanOrEqual(time + 59);
    await runJob(testEnv, "poll");
    expect(sent).toBe(0);
    await bindings.DB.prepare(
      "UPDATE comic_jobs SET payload=?,lease_until=0 WHERE id='poll'",
    )
      .bind(JSON.stringify({ ...data, polls: MAX_REMOTE_POLLS - 1 }))
      .run();
    fetchMock
      .get("https://fanqie.dddinmx.cn")
      .intercept({ path: "/v1/jobs/remote" })
      .reply(200, { ok: true, data: { status: "running" } });
    await runJob(testEnv, "poll");
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='poll'",
        ).first<any>()
      ).state,
    ).toBe("error");
    expect(sent).toBe(0);
  });
  it("stops reclaiming a crashed compute job after three claims", async () => {
    const { cookie, payload } = await login(),
      headers = auth(cookie, payload.data.csrf_token);
    await bindings.DB.prepare(
      "INSERT INTO compute_jobs(id,kind,input_key,payload,state,attempts,created_at,updated_at) VALUES ('crashed','cover','fixture','{}','queued',2,0,0)",
    ).run();
    const claim = () =>
      SELF.fetch(origin + "/api/admin/compute/jobs/crashed/claim", {
        method: "POST",
        headers,
        body: "{}",
      });
    expect((await claim()).status).toBe(200);
    await bindings.DB.prepare(
      "UPDATE compute_jobs SET lease_until=0 WHERE id='crashed'",
    ).run();
    expect((await claim()).status).toBe(409);
    const list = await SELF.fetch(origin + "/api/admin/compute/jobs", {
      headers,
    });
    expect(list.status).toBe(200);
    expect(
      await bindings.DB.prepare(
        "SELECT state,attempts FROM compute_jobs WHERE id='crashed'",
      ).first<any>(),
    ).toMatchObject({ state: "error", attempts: 3 });
  });
  it("claims cloud jobs once and rejects incomplete or stale cover outputs", async () => {
    await comicFixture();
    await bindings.LIBRARY.put("fixture/cover", new Uint8Array([1]));
    await bindings.DB.prepare(
      "UPDATE comics SET cover_key='fixture/cover'",
    ).run();
    const { cookie, payload } = await login(),
      headers = auth(cookie, payload.data.csrf_token);
    expect(
      (
        await SELF.fetch(origin + "/api/admin/compute/discover", {
          method: "POST",
          headers,
          body: "{}",
        })
      ).status,
    ).toBe(200);
    const jobs = (
      (await (await fullGet("/api/admin/compute/jobs", cookie)).json()) as any
    ).data;
    const job = jobs.find((j: any) => j.kind === "cover");
    expect(
      (
        await SELF.fetch(origin + `/api/admin/compute/jobs/${job.id}/claim`, {
          method: "POST",
          headers,
          body: "{}",
        })
      ).status,
    ).toBe(200);
    expect(
      (
        await SELF.fetch(origin + `/api/admin/compute/jobs/${job.id}/claim`, {
          method: "POST",
          headers,
          body: "{}",
        })
      ).status,
    ).toBe(409);
    expect(
      (
        await SELF.fetch(
          origin + `/api/admin/compute/jobs/${job.id}/complete`,
          { method: "POST", headers, body: "{}" },
        )
      ).status,
    ).toBe(409);
    const lease = (
      await bindings.DB.prepare(
        "SELECT claim_token FROM compute_jobs WHERE id=?",
      )
        .bind(job.id)
        .first<any>()
    ).claim_token;
    Object.assign(headers, { "X-Compute-Lease": lease });
    for (const variant of ["original", "hero", "mobile"])
      await bindings.LIBRARY.put(
        `compute/${job.id}/${lease}/${variant}`,
        new Uint8Array([1]),
      );
    await bindings.DB.prepare("UPDATE comics SET cover_key='new-cover'").run();
    expect(
      (
        await SELF.fetch(
          origin + `/api/admin/compute/jobs/${job.id}/complete`,
          { method: "POST", headers, body: "{}" },
        )
      ).status,
    ).toBe(200);
    expect(
      (await bindings.DB.prepare("SELECT hero_key FROM comics").first<any>())
        .hero_key,
    ).toBeNull();
  });
  it("verifies multipart parts and restores only fully uploaded metadata", async () => {
    const { cookie, payload } = await login(),
      headers = auth(cookie, payload.data.csrf_token),
      bytes = new Uint8Array([1, 2, 3, 4]),
      sha = Array.from(
        new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
      )
        .map((b) => b.toString(16).padStart(2, "0"))
        .join("");
    const upload = (await (
        await SELF.fetch(origin + "/api/admin/uploads", {
          method: "POST",
          headers,
          body: JSON.stringify({ size: 4, sha256: sha, extension: "cbz" }),
        })
      ).json()) as any,
      id = upload.data.id;
    expect(
      (
        await SELF.fetch(origin + `/api/admin/uploads/${id}/parts/1`, {
          method: "PUT",
          headers: {
            ...headers,
            "Content-Type": "application/octet-stream",
            "X-Content-SHA256": "0".repeat(64),
          },
          body: bytes,
        })
      ).status,
    ).toBe(422);
    expect(
      (
        await SELF.fetch(origin + `/api/admin/uploads/${id}/parts/1`, {
          method: "PUT",
          headers: {
            ...headers,
            "Content-Type": "application/octet-stream",
            "X-Content-SHA256": sha,
          },
          body: bytes,
        })
      ).status,
    ).toBe(200);
    expect(
      (
        await SELF.fetch(origin + `/api/admin/uploads/${id}/complete`, {
          method: "POST",
          headers,
          body: "{}",
        })
      ).status,
    ).toBe(200);
    expect(
      new Uint8Array(
        await (
          await fullGet(`/api/admin/uploads/${id}/file`, cookie)
        ).arrayBuffer(),
      ),
    ).toEqual(bytes);
    expect(
      (
        await SELF.fetch(origin + "/api/admin/import/metadata", {
          method: "POST",
          headers,
          body: JSON.stringify({
            kind: "novels",
            records: [
              {
                id: "legacy",
                title: "旧小说",
                object_key: "missing",
                size: 4,
                sha256: sha,
              },
            ],
          }),
        })
      ).status,
    ).toBe(409);
  });
  it("encrypts integration credentials at rest and never renders them in settings", async () => {
    const { cookie, payload } = await login(),
      headers = auth(cookie, payload.data.csrf_token);
    const response = await SELF.fetch(origin + "/settings/webdav", {
      method: "POST",
      redirect: "manual",
      headers,
      body: JSON.stringify({
        url: "https://dav.example/dav/",
        username: "reader",
        password: "private-mount-password",
        root: "/books",
        cache_gb: 10,
      }),
    });
    expect(response.status, await response.clone().text()).toBe(303);
    const mount = await bindings.DB.prepare(
      "SELECT * FROM remote_mounts",
    ).first<any>();
    expect(mount.password).not.toContain("private-mount-password");
    const html = await (await fullGet("/settings/webdav", cookie)).text();
    expect(html).not.toContain("private-mount-password");
    expect(html).toContain("密码已保存");
  });
  it("reads remote CBZ central directory and verifies a page with HTTP ranges", async () => {
    const { encrypt } = await import("../src/security");
    const zip = zipSync({
      "002.png": new Uint8Array([137, 80, 78, 71, 2]),
      "001.png": new Uint8Array([137, 80, 78, 71, 1]),
    });
    await bindings.DB.prepare(
      "INSERT INTO remote_mounts(id,url,username,password) VALUES ('test','https://dav.example/books/','reader',?)",
    )
      .bind(await encrypt(bindings, "secret"))
      .run();
    await comicFixture();
    await bindings.DB.prepare(
      "UPDATE chapters SET object_key=NULL,manifest_key=NULL,remote_url='https://dav.example/books/book.cbz',mount_id='test',size=?,format='cbz' WHERE id='ch_test'",
    )
      .bind(zip.length)
      .run();
    fetchMock
      .get("https://dav.example")
      .intercept({
        path: "/books/book.cbz",
        headers: { range: "bytes=0-" + (zip.length - 1) },
      })
      .reply(206, zip);
    const tail = new DataView(zip.buffer),
      end = zip.length - 22,
      offset = tail.getUint32(end + 16, true),
      len = tail.getUint32(end + 12, true);
    fetchMock
      .get("https://dav.example")
      .intercept({
        path: "/books/book.cbz",
        headers: { range: `bytes=${offset}-${offset + len - 1}` },
      })
      .reply(206, zip.subarray(offset, offset + len));
    const { cookie } = await login();
    const list = (await (
      await fullGet("/api/comic-pages/原版漫画/ch_test.cbz", cookie)
    ).json()) as any;
    expect(list.pages).toBe(2);
    const manifest = (await (await bindings.LIBRARY.get(
      (
        await bindings.DB.prepare(
          "SELECT manifest_key FROM chapters",
        ).first<any>()
      ).manifest_key,
    ))!.json()) as any;
    expect(manifest[0].name).toBe("001.png");
    const page = manifest[0],
      header = new DataView(zip.buffer),
      start =
        page.offset +
        30 +
        header.getUint16(page.offset + 26, true) +
        header.getUint16(page.offset + 28, true);
    for (const [from, to] of [
      [page.offset, page.offset + 29],
      [start, start + page.compressed - 1],
    ])
      fetchMock
        .get("https://dav.example")
        .intercept({
          path: "/books/book.cbz",
          headers: { range: `bytes=${from}-${to}` },
        })
        .reply(206, zip.subarray(from, to + 1));
    const image = await fullGet(
      "/api/comic-pages/原版漫画/ch_test.cbz?page=0",
      cookie,
    );
    expect(new Uint8Array(await image.arrayBuffer())).toEqual(
      new Uint8Array([137, 80, 78, 71, 1]),
    );
  });
});

describe("cloud R2 library scan", () => {
  it("indexes a private CBZ prefix without copying its images, and keeps direct reading usable", async () => {
    const raw = zipSync({
      "01.png": new Uint8Array([137, 80, 78, 71, 1]),
      "02.png": new Uint8Array([137, 80, 78, 71, 2]),
    });
    await bindings.LIBRARY.put("scan-fixture/云端扫描漫画/第一话.cbz", raw);
    await bindings.DB.prepare(
      "INSERT INTO settings(key,value) VALUES ('scan_path:test','r2://library/scan-fixture/')",
    ).run();
    const scan = await scanLibrary(bindings, {
      user_id: "admin",
      role: "admin",
      username: "admin",
    } as any);
    expect(scan.task_ids).toHaveLength(1);
    await runJob(bindings, scan.task_ids[0]);
    const comic = await bindings.DB.prepare(
      "SELECT * FROM comics WHERE name='云端扫描漫画'",
    ).first<any>();
    expect(comic.provider).toBe("r2");
    const chapter = await bindings.DB.prepare(
      "SELECT * FROM chapters WHERE comic_id=?",
    )
      .bind(comic.id)
      .first<any>();
    expect(chapter.object_key).toBe("scan-fixture/云端扫描漫画/第一话.cbz");
    expect(chapter.manifest_key).toBeNull();
    const { cookie } = await login();
    const response = await SELF.fetch(
      origin +
        "/api/comic-pages/" +
        encodeURIComponent(comic.name) +
        "/" +
        encodeURIComponent("第一话.cbz") +
        "?page=0",
      { headers: { Cookie: cookie } },
    );
    expect(response.status).toBe(200);
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(
      new Uint8Array([137, 80, 78, 71, 1]),
    );
  });
});
import "./job-loop";

import { computeApi as reviewCompute } from "../src/compute";
import {
  tasks as reviewTasks,
  taskAction as reviewTask,
} from "../src/downloads";
import { statistics as reviewStats } from "../src/reading";
import { putBytes as reviewPut, reserve } from "../src/library";
const reviewUser = {
  user_id: "admin",
  username: "admin",
  role: "admin",
  can_download: 1,
  can_view_adult: 1,
  csrf: "x",
  token_hash: "x",
};
function reviewCount() {
  let count = 0;
  const DB = {
    prepare(sql: string) {
      count++;
      return bindings.DB.prepare(sql);
    },
    batch: bindings.DB.batch.bind(bindings.DB),
  };
  return { env: { ...bindings, DB } as Env, count: () => count };
}
describe("review regressions", () => {
  it("single task status remains bounded with historical tasks", async () => {
    await bindings.DB.batch(
      Array.from({ length: 80 }, (_, i) =>
        bindings.DB.prepare(
          "INSERT INTO comic_jobs(id,user_id,kind,payload,state,created_at,updated_at) VALUES (?,'admin','source','{}','completed',1,1)",
        ).bind("review-task" + i),
      ),
    );
    const counted = reviewCount();
    expect((await reviewTasks(counted.env, reviewUser)).length).toBe(80);
    expect(counted.count()).toBe(2);
    const single = reviewCount();
    expect(
      (await reviewTasks(single.env, reviewUser, "review-task0")).map(
        (row) => row.id,
      ),
    ).toEqual(["review-task0"]);
    expect(single.count()).toBe(2);
  });
  it("cancelled source cannot be resurrected by compute completion", async () => {
    await bindings.DB.prepare(
      "INSERT INTO comic_jobs(id,user_id,kind,payload,state,created_at,updated_at) VALUES ('review-parent','admin','source','{}','waiting',1,1)",
    ).run();
    await bindings.DB.prepare(
      `INSERT INTO compute_jobs(id,kind,input_key,payload,state,created_at,updated_at) VALUES ('review-compute','legacy-source','legacy-source/review-parent','{"parent_job":"review-parent"}','running',1,1)`,
    ).run();
    await reviewTask(bindings, reviewUser, "review-parent", "cancel");
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='review-parent'",
        ).first<any>()
      ).state,
    ).toBe("cancelled");
    let sends = 0;
    await expect(
      reviewCompute(
        new Request(
          origin + "/api/admin/compute/jobs/review-compute/complete",
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              source: { title: "Review comic", chapters: [{ id: "1" }] },
            }),
          },
        ),
        {
          ...bindings,
          DOWNLOADS: {
            send: async () => {
              sends++;
            },
          },
        } as any,
        reviewUser,
      ),
    ).rejects.toThrow();
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='review-parent'",
        ).first<any>()
      ).state,
    ).toBe("cancelled");
    expect(sends).toBe(0);
  });
  it("deletion collects compute artifacts and storage accounting", async () => {
    await comicFixture();
    await reviewPut(
      bindings,
      "compute/review-cover/hero",
      new Uint8Array(100),
      "image/png",
    );
    await bindings.DB.prepare(
      "UPDATE comics SET hero_key='compute/review-cover/hero' WHERE id='c_test'",
    ).run();
    const { cookie, payload } = await login();
    const response = await SELF.fetch(origin + "/comic/delete", {
      method: "POST",
      headers: auth(cookie, payload.data.csrf_token),
      body: JSON.stringify({ comic_name: "c_test" }),
      redirect: "manual",
    });
    expect(response.status).toBe(303);
    expect(
      await bindings.DB.prepare(
        "SELECT id FROM comics WHERE id='c_test'",
      ).first(),
    ).toBe(null);
    expect(await bindings.LIBRARY.head("compute/review-cover/hero")).toBe(null);
    expect(
      await bindings.DB.prepare(
        "SELECT size FROM storage_ledger WHERE key='compute/review-cover/hero'",
      ).first(),
    ).toBe(null);
  });
  it.each(["urlencoded", "multipart"])(
    "deletes through the original detail form's body CSRF token (%s)",
    async (encoding) => {
      await comicFixture();
      const { cookie, payload } = await login();
      const fields = {
        comic_name: "原版漫画",
        directory: "c_test",
        csrf_token: payload.data.csrf_token,
      };
      const body =
        encoding === "urlencoded"
          ? new URLSearchParams(fields)
          : new FormData();
      if (body instanceof FormData)
        for (const [key, value] of Object.entries(fields)) body.set(key, value);
      const response = await SELF.fetch(origin + "/comic/delete", {
        method: "POST",
        headers: { Cookie: cookie, Origin: origin },
        body,
        redirect: "manual",
      });
      expect(response.status).toBe(303);
      expect(
        await bindings.DB.prepare(
          "SELECT id FROM comics WHERE id='c_test'",
        ).first(),
      ).toBeNull();
    },
  );
  it("rejects an invalid form CSRF token without deleting the comic", async () => {
    await comicFixture();
    const { cookie } = await login();
    const response = await SELF.fetch(origin + "/comic/delete", {
      method: "POST",
      headers: { Cookie: cookie, Origin: origin },
      body: new URLSearchParams({
        comic_name: "原版漫画",
        csrf_token: "invalid",
      }),
    });
    expect(response.status).toBe(403);
    expect(
      await bindings.DB.prepare(
        "SELECT id FROM comics WHERE id='c_test'",
      ).first(),
    ).not.toBeNull();
  });
  it("keeps a committed deletion successful and the cleanup plan durable while Queue admission is paused", async () => {
    await comicFixture();
    await Promise.all(
      Array.from({ length: 101 }, (_, i) =>
        bindings.LIBRARY.put(`comics/c_test/delete-${i}`, new Uint8Array([1])),
      ),
    );
    await pauseTestQueue();
    const { cookie, payload } = await login();
    const response = await SELF.fetch(origin + "/comic/delete", {
      method: "POST",
      headers: { Cookie: cookie, Origin: origin },
      body: new URLSearchParams({
        comic_name: "原版漫画",
        csrf_token: payload.data.csrf_token,
      }),
      redirect: "manual",
    });
    expect(response.status).toBe(303);
    expect(
      await bindings.DB.prepare(
        "SELECT id FROM comics WHERE id='c_test'",
      ).first(),
    ).toBeNull();
    expect(
      await bindings.DB.prepare(
        "SELECT key FROM storage_gc WHERE key='comics/c_test/'",
      ).first(),
    ).not.toBeNull();
    expect(
      (await bindings.LIBRARY.list({ prefix: "comics/c_test/" })).objects,
    ).toHaveLength(1);
    const { cleanupStorage } = await import("../src/storage");
    await cleanupStorage(bindings);
    expect(
      (await bindings.LIBRARY.list({ prefix: "comics/c_test/" })).objects,
    ).toHaveLength(0);
  });
  it("statistics query count stays constant for 1000 ranked comics", async () => {
    await bindings.DB.batch(
      Array.from({ length: 1000 }, (_, i) =>
        bindings.DB.prepare(
          "INSERT INTO comics(id,name,provider,source_url,created_at,updated_at) VALUES (?,?, 'review','https://example.com',1,1)",
        ).bind("review" + i, "review" + i),
      ),
    );
    await bindings.DB.batch(
      Array.from({ length: 1000 }, (_, i) =>
        bindings.DB.prepare(
          "INSERT INTO reading_time(user_id,media_id,day,seconds) VALUES ('admin',?,'2026-10-13',60)",
        ).bind("review" + i),
      ),
    );
    const counted = reviewCount();
    await reviewStats(counted.env, reviewUser, "2026", "all");
    expect(counted.count()).toBe(3);
    await bindings.DB.prepare(
      "INSERT INTO group_permissions(user_id,group_name) VALUES ('admin','默认分组')",
    ).run();
    const normal = reviewCount();
    await reviewStats(
      normal.env,
      { ...reviewUser, role: "user" },
      "2026",
      "all",
    );
    expect(normal.count()).toBe(3);
  });
  it("already limited anonymous login performs no database operations", async () => {
    const counted = reviewCount();
    const request = () =>
      new Request(origin + "/api/v1/auth/login", {
        method: "POST",
        headers: {
          Origin: origin,
          "Content-Type": "application/json",
          "CF-Connecting-IP": "192.0.2.1",
        },
        body: JSON.stringify({ username: "nonexistent", password: "wrong" }),
      });
    for (let i = 0; i < 6; i++) await pilotRoute(request(), counted.env);
    const before = counted.count();
    const response = await pilotRoute(request(), counted.env);
    expect(response.status).toBe(429);
    expect(counted.count() - before).toBe(0);
  });
  it("failed R2 write releases its owned reservation", async () => {
    await expect(
      reviewPut(
        {
          ...bindings,
          LIBRARY: {
            head: bindings.LIBRARY.head.bind(bindings.LIBRARY),
            put: async () => {
              throw new Error("simulated storage failure");
            },
          },
        } as any,
        "compute/review-failed/artifact",
        new Uint8Array(100),
        "image/png",
      ),
    ).rejects.toThrow("simulated storage failure");
    expect(
      await bindings.DB.prepare(
        "SELECT size,state FROM storage_ledger WHERE key='compute/review-failed/artifact'",
      ).first(),
    ).toBe(null);
  });
});
import { boundedInflate } from "../src/remote-library";
import { deflateSync } from "fflate";
import {
  cleanupStorage,
  cleanupUploads,
  releaseReservation,
} from "../src/storage";
import { LoginGuard } from "../src/login-guard";
import { notifyCompute } from "../src/compute-scheduler";
import { runWebdavScan } from "../src/integrations";
async function seedCompute(
  id = "lease-job",
  payload: any = { parent_job: "lease-parent" },
  kind = "legacy-source",
) {
  const time = Math.floor(Date.now() / 1000);
  await bindings.DB.prepare(
    "INSERT OR IGNORE INTO comic_jobs(id,user_id,kind,payload,state,created_at,updated_at) VALUES ('lease-parent','admin','source','{}','waiting',?,?)",
  )
    .bind(time, time)
    .run();
  await bindings.DB.prepare(
    "INSERT INTO compute_jobs(id,kind,input_key,payload,created_at,updated_at) VALUES (?,?,?, ?,?,?)",
  )
    .bind(id, kind, "fixture/input", JSON.stringify(payload), time, time)
    .run();
  const response = await reviewCompute(
    new Request(origin + `/api/admin/compute/jobs/${id}/claim`, {
      method: "POST",
      body: "{}",
    }),
    bindings,
    reviewUser,
  );
  return ((await response!.json()) as any).data.lease_token;
}
const computeRequest = (
  id: string,
  action: string,
  token: string,
  data: any = {},
) =>
  new Request(origin + `/api/admin/compute/jobs/${id}/${action}`, {
    method: action.startsWith("output/") ? "PUT" : "POST",
    headers: { "Content-Type": "application/json", "X-Compute-Lease": token },
    body: JSON.stringify(data),
  });
describe("execution and storage safeguards", () => {
  it("rejects stale execution outputs after lease reclaim", async () => {
    const old = await seedCompute();
    await bindings.DB.prepare(
      "UPDATE compute_jobs SET lease_until=0 WHERE id='lease-job'",
    ).run();
    const next = (
      (await (await reviewCompute(
        computeRequest("lease-job", "claim", old),
        bindings,
        reviewUser,
      ))!.json()) as any
    ).data.lease_token;
    expect(next).not.toBe(old);
    await expect(
      reviewCompute(
        computeRequest("lease-job", "output/source", old, {
          title: "old",
          chapters: [{}],
        }),
        bindings,
        reviewUser,
      ),
    ).rejects.toThrow(/租约/);
    expect(
      (await bindings.LIBRARY.list({ prefix: "compute/lease-job/" })).objects,
    ).toHaveLength(0);
    const fake = { ...bindings, DOWNLOADS: { send: async () => {} } } as any;
    await reviewCompute(
      computeRequest("lease-job", "complete", next, {
        source: { title: "current", chapters: [{}] },
      }),
      fake,
      reviewUser,
    );
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='lease-parent'",
        ).first<any>()
      ).state,
    ).toBe("queued");
  });
  it("cancelling during output upload cannot publish or restart a task", async () => {
    const token = await seedCompute();
    const library = new Proxy(bindings.LIBRARY, {
      get(target, property) {
        if (property === "put")
          return async (...args: any[]) => {
            const result = await (target.put as any)(...args);
            await reviewTask(bindings, reviewUser, "lease-parent", "cancel");
            return result;
          };
        const value = (target as any)[property];
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    await expect(
      reviewCompute(
        computeRequest("lease-job", "output/source", token, {
          title: "late",
          chapters: [{}],
        }),
        { ...bindings, LIBRARY: library },
        reviewUser,
      ),
    ).rejects.toThrow();
    await cleanupStorage(bindings);
    expect(
      (await bindings.LIBRARY.list({ prefix: "compute/lease-job/" })).objects,
    ).toHaveLength(0);
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='lease-parent'",
        ).first<any>()
      ).state,
    ).toBe("cancelled");
  });
  it("last successful conversion preserves partial chapter failure", async () => {
    await comicFixture();
    await bindings.DB.prepare(
      "INSERT INTO comic_jobs(id,user_id,comic_id,kind,payload,state,created_at,updated_at) VALUES ('partial-root','admin','c_test','source','{}','waiting',1,1)",
    ).run();
    await bindings.DB.prepare(
      "INSERT INTO comic_jobs(id,user_id,comic_id,parent_id,kind,payload,state,created_at,updated_at) VALUES ('bad-child','admin','c_test','partial-root','chapter','{}','error',1,1),('good-child','admin','c_test','partial-root','chapter','{}','waiting',1,1)",
    ).run();
    const token = await seedCompute(
      "partial-compute",
      { parent_job: "good-child", chapter_id: "ch_test" },
      "legacy-chapter",
    );
    await bindings.LIBRARY.put(
      `compute/partial-compute/${token}/page/0`,
      new Uint8Array([1]),
    );
    await reviewCompute(
      computeRequest("partial-compute", "complete", token, {
        pages: [{ width: 1, height: 1 }],
      }),
      bindings,
      reviewUser,
    );
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='partial-root'",
        ).first<any>()
      ).state,
    ).toBe("error");
  });
  it("failed multipart initialization releases the reservation", async () => {
    const library = new Proxy(bindings.LIBRARY, {
      get(target, property) {
        if (property === "createMultipartUpload")
          return async () => {
            throw new Error("multipart unavailable");
          };
        const value = (target as any)[property];
        return typeof value === "function" ? value.bind(target) : value;
      },
    });
    const { uploadApi } = await import("../src/uploads");
    await expect(
      uploadApi(
        new Request(origin + "/api/admin/uploads", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            size: 123,
            sha256: "a".repeat(64),
            extension: "cbz",
          }),
        }),
        { ...bindings, LIBRARY: library },
        reviewUser,
      ),
    ).rejects.toThrow("multipart unavailable");
    expect(
      await bindings.DB.prepare("SELECT key FROM storage_ledger").all(),
    ).toMatchObject({ results: [] });
  });
  it("shared import survives comic deletion until its final reference is removed", async () => {
    await comicFixture();
    const key = "imports/" + "b".repeat(64) + ".cbz";
    await reviewPut(bindings, key, new Uint8Array(4), "application/zip");
    await bindings.DB.prepare(
      "UPDATE chapters SET object_key=?,manifest_key=NULL WHERE id='ch_test'",
    )
      .bind(key)
      .run();
    await bindings.DB.prepare(
      "INSERT INTO books(id,source_id,title,author,object_key,size,sha256,created_at) VALUES ('shared','shared','shared','shared',?,4,?,1)",
    )
      .bind(key, "b".repeat(64))
      .run();
    const { cookie, payload } = await login();
    expect(
      (
        await SELF.fetch(origin + "/comic/delete", {
          method: "POST",
          headers: auth(cookie, payload.data.csrf_token),
          body: JSON.stringify({ comic_name: "c_test" }),
          redirect: "manual",
        })
      ).status,
    ).toBe(303);
    expect(await bindings.LIBRARY.head(key)).not.toBeNull();
    expect(
      await bindings.DB.prepare("SELECT size FROM storage_ledger WHERE key=?")
        .bind(key)
        .first(),
    ).toEqual({ size: 4 });
  });
  it("reservation owner prevents concurrent uploads and foreign releases", async () => {
    const { reserve } = await import("../src/library");
    const owner = await reserve(
      bindings,
      "imports/exclusive.cbz",
      100,
      "first-owner",
    );
    await expect(
      reserve(bindings, "imports/exclusive.cbz", 100, "second-owner"),
    ).rejects.toThrow(/上传/);
    await releaseReservation(bindings, "imports/exclusive.cbz", "second-owner");
    expect(
      await bindings.DB.prepare("SELECT owner FROM storage_ledger").first(),
    ).toEqual({ owner });
    await releaseReservation(bindings, "imports/exclusive.cbz", owner);
    expect(
      await bindings.DB.prepare("SELECT owner FROM storage_ledger").first(),
    ).toBeNull();
  });
  it("bounded inflate rejects forged lengths before full expansion", () => {
    const content = new Uint8Array(10 * 1024 * 1024),
      compressed = deflateSync(content);
    expect(() => boundedInflate(compressed, 100)).toThrow(/上限/);
    const normal = new TextEncoder().encode("normal page contents");
    expect(boundedInflate(deflateSync(normal), normal.length)).toEqual(normal);
  });
  it("paused WebDAV performs no upstream request and resume keeps its checkpoint", async () => {
    await bindings.DB.prepare(
      "INSERT INTO remote_mounts(id,url,username,password,state) VALUES ('default','https://example.com/dav/','','unused','paused')",
    ).run();
    await bindings.DB.prepare(
      "INSERT INTO comic_jobs(id,user_id,kind,payload,created_at,updated_at) VALUES ('scan','admin','webdav',?,1,1)",
    )
      .bind(
        JSON.stringify({
          mount_id: "default",
          url: "https://example.com/dav/",
          depth: 0,
        }),
      )
      .run();
    await runJob(
      { ...bindings, DOWNLOADS: { send: async () => {} } } as any,
      "scan",
    );
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='scan'",
        ).first<any>()
      ).state,
    ).toBe("waiting");
    const { cookie, payload } = await login();
    await SELF.fetch(origin + "/settings/webdav/control", {
      method: "POST",
      headers: auth(cookie, payload.data.csrf_token),
      body: JSON.stringify({ action: "resume" }),
      redirect: "manual",
    });
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='scan'",
        ).first<any>()
      ).state,
    ).toBe("queued");
    await SELF.fetch(origin + "/settings/webdav/control", {
      method: "POST",
      headers: auth(cookie, payload.data.csrf_token),
      body: JSON.stringify({ action: "stop" }),
      redirect: "manual",
    });
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='scan'",
        ).first<any>()
      ).state,
    ).toBe("cancelled");
  });
  it("idle automation probes consume no database requests and wake sequence is not lost", async () => {
    const stub = bindings.APPLICATION.get(
      bindings.APPLICATION.idFromName("scheduler"),
    );
    const probe = () =>
      stub
        .fetch(origin + "/api/compute-ready", {
          headers: { Authorization: "Bearer test-wake-token" },
        })
        .then((r) => r.json<any>());
    expect((await probe()).data.pending).toBe(false);
    await notifyCompute(bindings);
    const observed = (await probe()).data;
    expect(observed).toMatchObject({ pending: true, sequence: 1, limit: 5 });
    expect((await probe()).data.pending).toBe(false); // leased/cooldown; no duplicate runner
    await notifyCompute(bindings);
    await stub.fetch(origin + "/api/compute-ready/finished", {
      method: "POST",
      headers: { Authorization: "Bearer test-wake-token" },
      body: JSON.stringify({ sequence: observed.sequence, pending: false }),
    });
    await runInDurableObject(stub, async (_instance, state) => {
      expect((await state.storage.get<any>("compute-wake")).pending).toBe(true);
    });
  });
  it("compute wake rejects a bearer token other than COMPUTE_WAKE_TOKEN", async () => {
    const stub = bindings.APPLICATION.get(
      bindings.APPLICATION.idFromName("scheduler"),
    );
    const response = await stub.fetch(origin + "/api/compute-ready", {
      headers: { Authorization: "Bearer not-the-wake-token" },
    });
    expect(response.status).toBe(401);
  });
});
import { fencedEnv } from "../src/job-state";
describe("terminal state fencing", () => {
  it("failure callbacks mark both child and root without reviving cancelled tasks", async () => {
    await bindings.DB.prepare(
      "INSERT INTO comic_jobs(id,user_id,kind,payload,state,created_at,updated_at) VALUES ('root-fail','admin','source','{}','waiting',1,1),('child-fail','admin','chapter','{}','waiting',1,1)",
    ).run();
    await bindings.DB.prepare(
      "UPDATE comic_jobs SET parent_id='root-fail' WHERE id='child-fail'",
    ).run();
    const token = await seedCompute(
      "failure-callback",
      { parent_job: "child-fail" },
      "legacy-chapter",
    );
    await bindings.DB.prepare(
      "UPDATE compute_jobs SET attempts=3 WHERE id='failure-callback'",
    ).run();
    await reviewCompute(
      computeRequest("failure-callback", "fail", token),
      bindings,
      reviewUser,
    );
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='child-fail'",
        ).first<any>()
      ).state,
    ).toBe("error");
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='root-fail'",
        ).first<any>()
      ).state,
    ).toBe("error");
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM compute_jobs WHERE id='failure-callback'",
        ).first<any>()
      ).state,
    ).toBe("error");
  });
  it("mutation predicates cannot bypass cancellation through OR", async () => {
    await comicFixture();
    await bindings.DB.prepare(
      "INSERT INTO comic_jobs(id,user_id,kind,payload,state,claim_token,created_at,updated_at) VALUES ('fenced','admin','source','{}','cancelled','old',1,1)",
    ).run();
    const restricted = fencedEnv(bindings, "comic_jobs", {
      id: "fenced",
      claim_token: "old",
      payload: "{}",
    });
    await restricted.DB.prepare(
      "UPDATE comics SET name='unexpected' WHERE id='c_test' OR name='原版漫画'",
    ).run();
    expect(
      (
        await bindings.DB.prepare(
          "SELECT name FROM comics WHERE id='c_test'",
        ).first<any>()
      ).name,
    ).toBe("原版漫画");
  });
  it("failed executions cannot publish subsequent chapter changes", async () => {
    await comicFixture();
    await bindings.DB.prepare(
      "INSERT INTO comic_jobs(id,user_id,kind,payload,state,claim_token,created_at,updated_at) VALUES ('fenced','admin','source','{}','error','old',1,1)",
    ).run();
    const restricted = fencedEnv(bindings, "comic_jobs", {
      id: "fenced",
      claim_token: "old",
      payload: "{}",
    });
    await restricted.DB.prepare(
      "UPDATE chapters SET title='unexpected' WHERE id='ch_test'",
    ).run();
    expect(
      (
        await bindings.DB.prepare(
          "SELECT title FROM chapters WHERE id='ch_test'",
        ).first<any>()
      ).title,
    ).toBe("第一话");
  });
});
it("root cancellation fences grandchildren before cancellation cascade completes", async () => {
  await comicFixture();
  await bindings.DB.prepare(
    "INSERT INTO comic_jobs(id,user_id,kind,payload,state,created_at,updated_at) VALUES ('root-stop','admin','source','{}','cancelled',1,1),('child-stop','admin','chapter','{}','waiting',1,1)",
  ).run();
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET parent_id='root-stop' WHERE id='child-stop'",
  ).run();
  const token = await seedCompute(
    "root-stopped-compute",
    { parent_job: "child-stop", chapter_id: "ch_test" },
    "legacy-chapter",
  );
  await expect(
    reviewCompute(
      computeRequest("root-stopped-compute", "complete", token, {
        pages: [{}],
      }),
      bindings,
      reviewUser,
    ),
  ).rejects.toThrow(/停止/);
  const response = await reviewCompute(
    new Request(origin + "/api/admin/compute/jobs"),
    bindings,
    reviewUser,
  );
  expect(((await response!.json()) as any).data).not.toContainEqual(
    expect.objectContaining({ id: "root-stopped-compute" }),
  );
  expect(
    (
      await bindings.DB.prepare(
        "SELECT state FROM comic_jobs WHERE id='root-stop'",
      ).first<any>()
    ).state,
  ).toBe("cancelled");
});
it("resource diagnostics require an administrator and report persisted jobs limits without SQL in that object", async () => {
  expect((await SELF.fetch(origin + "/api/admin/resource-status")).status).toBe(
    401,
  );
  await bindings.DB.prepare(
    "INSERT INTO users(id,username,password_hash,role) VALUES ('budget-user','budget-user',?,'user')",
  )
    .bind(encoded)
    .run();
  const member = await login("budget-user");
  expect(
    (
      await SELF.fetch(origin + "/api/admin/resource-status", {
        headers: { Cookie: member.cookie },
      })
    ).status,
  ).toBe(403);
  const administrator = await login();
  const response = await SELF.fetch(origin + "/api/admin/resource-status", {
    headers: { Cookie: administrator.cookie },
  });
  expect(response.status).toBe(200);
  const body = (await response.json()) as any;
  expect(body.database.request_limit).toBe(10000);
  expect(body.database.requests).toBe(0);
  expect(body.database.paused).toBe(false);
});

import { repairNativeFallback } from "../src/native-fallback-repair";
import { repairNativeStorage } from "../src/native-storage-repair";
async function nativePageFixture() {
  await comicFixture();
  await bindings.DB.prepare(
    "UPDATE chapters SET format='images',state='downloading',page_count=2 WHERE id='ch_test'",
  ).run();
  await bindings.DB.prepare(
    "INSERT INTO comic_jobs(id,user_id,comic_id,kind,payload,state,created_at,updated_at) VALUES ('native-root','admin','c_test','source',?,'waiting',1,1)",
  )
    .bind(
      JSON.stringify({
        source_planned: true,
        source_cursor: 1,
        source_total: 1,
      }),
    )
    .run();
  await bindings.DB.prepare(
    "INSERT INTO comic_jobs(id,user_id,comic_id,parent_id,kind,payload,state,created_at,updated_at) VALUES ('native-child','admin','c_test','native-root','chapter',?,'waiting',1,1)",
  )
    .bind(
      JSON.stringify({
        chapter_id: "ch_test",
        provider: "baozimh_org",
        format: 0,
      }),
    )
    .run();
  await bindings.DB.prepare(
    "INSERT INTO comic_jobs(id,user_id,comic_id,parent_id,kind,payload,state,size,created_at,updated_at) VALUES ('native-good','admin','c_test','native-child','page',?,'completed',4,1,1),('native-bad','admin','c_test','native-child','page',?,'queued',0,1,1)",
  )
    .bind(
      JSON.stringify({ chapter_id: "ch_test", key: "fixture/page", format: 0 }),
      JSON.stringify({
        chapter_id: "ch_test",
        key: "fixture/missing-page",
        url: "https://c-nd3-1.6wm.top/failure.jpg",
        referer: "https://baozimh.org/",
        format: 0,
      }),
    )
    .run();
  const manifest = [{ key: "fixture/page" }, { key: "fixture/missing-page" }];
  await bindings.LIBRARY.put("fixture/manifest", JSON.stringify(manifest));
  const sends: any[] = [];
  const local = {
    ...bindings,
    DOWNLOADS: {
      send: async (body: any, options: any) => sends.push({ body, options }),
      sendBatch: async (messages: any[]) => sends.push(...messages),
    },
  } as unknown as Env;
  return { local, sends, manifest };
}
const reservationPng = Uint8Array.from(
  atob(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aGqkAAAAASUVORK5CYII=",
  ),
  (c) => c.charCodeAt(0),
);
function nativeImageReply() {
  fetchMock
    .get("https://c-nd3-1.6wm.top")
    .intercept({ path: "/failure.jpg" })
    .reply(200, reservationPng, {
      headers: {
        "content-type": "image/png",
        "content-length": String(reservationPng.length),
      },
    });
}
it("a normal image uses six SQL statements while actually persisting the image and fencing completion", async () => {
  const { local } = await nativePageFixture();
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='queued' WHERE id='native-good'",
  ).run();
  let statements = 0;
  let rowsRead = 0,
    rowsWritten = 0;
  const measured = (result: any) => {
    rowsRead += result.meta.rows_read;
    rowsWritten += result.meta.rows_written;
    return result;
  };
  const wrap = (raw: any): any => ({
    bind: (...args: any[]) => wrap(raw.bind(...args)),
    first: async (column?: string) => {
      statements++;
      const result = measured(await raw.all());
      const row = result.results[0] || null;
      return column && row ? row[column] : row;
    },
    all: (...args: any[]) => {
      statements++;
      return raw.all(...args).then(measured);
    },
    run: (...args: any[]) => {
      statements++;
      return raw.run(...args).then(measured);
    },
  });
  const db = {
    prepare: (sql: string) => wrap(bindings.DB.prepare(sql)),
    batch: bindings.DB.batch.bind(bindings.DB),
  };
  nativeImageReply();
  await runJob({ ...local, DB: db } as Env, "native-bad");
  expect(statements).toBe(6);
  expect(rowsWritten).toBeGreaterThanOrEqual(4);
  expect(rowsWritten).toBeLessThan(40);
  console.info(
    JSON.stringify({
      d1_local_metering: true,
      statements,
      rows_read: rowsRead,
      rows_written: rowsWritten,
    }),
  );
  expect(
    await bindings.DB.prepare(
      "SELECT state FROM comic_jobs WHERE id='native-bad'",
    ).first("state"),
  ).toBe("completed");
  expect((await bindings.LIBRARY.head("fixture/missing-page"))?.size).toBe(
    reservationPng.length,
  );
});
it("stopped ancestors prevent image claims and maintenance re-enqueueing instead of repeatedly spending quota", async () => {
  const { local, sends } = await nativePageFixture();
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='error' WHERE id='native-root'",
  ).run();
  await runJob(local, "native-bad");
  expect(
    await bindings.DB.prepare(
      "SELECT state,attempts FROM comic_jobs WHERE id='native-bad'",
    ).first(),
  ).toEqual({ state: "queued", attempts: 0 });
  const { maintenance } = await import("../src/index");
  await maintenance(local);
  expect(sends).toHaveLength(0);
});
it("a crashed final page attempt terminates its chapter and source rather than waiting forever", async () => {
  const { local } = await nativePageFixture();
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='running',attempts=5,lease_until=0 WHERE id='native-bad'",
  ).run();
  const { maintenance } = await import("../src/index");
  await maintenance(local);
  for (const id of ["native-bad", "native-child", "native-root"])
    expect(
      await bindings.DB.prepare("SELECT state FROM comic_jobs WHERE id=?")
        .bind(id)
        .first("state"),
    ).toBe("error");
});
it.each([
  "legacy abandoned reservation",
  "expired leased reservation",
  "stored ledger without an object",
])(
  "finishes the native book after %s without dropping existing pages",
  async (scenario) => {
    const { local } = await nativePageFixture();
    const time = Math.floor(Date.now() / 1000);
    await bindings.DB.prepare(
      "INSERT INTO storage_ledger(key,size,state,created_at,owner,lease_until) VALUES ('fixture/missing-page',?,?,?, ?,?)",
    )
      .bind(
        reservationPng.length,
        scenario.startsWith("stored") ? "stored" : "reserved",
        time - 1800,
        scenario.startsWith("stored") ? null : "abandoned-owner",
        scenario.startsWith("expired") ? time - 1 : 0,
      )
      .run();
    nativeImageReply();
    await runJob(local, "native-bad");
    expect(
      await bindings.DB.prepare(
        "SELECT state FROM comic_jobs WHERE id='native-root'",
      ).first("state"),
    ).toBe("completed");
    expect(
      await bindings.DB.prepare(
        "SELECT state,attempts,size FROM comic_jobs WHERE id='native-good'",
      ).first(),
    ).toEqual({ state: "completed", attempts: 0, size: 4 });
    expect(
      await bindings.DB.prepare(
        "SELECT state,owner,lease_until FROM storage_ledger WHERE key='fixture/missing-page'",
      ).first(),
    ).toEqual({ state: "stored", owner: null, lease_until: 0 });
    expect(
      new Uint8Array(
        await (await bindings.LIBRARY.get(
          "fixture/missing-page",
        ))!.arrayBuffer(),
      ),
    ).toEqual(reservationPng);
  },
);
it("a live reservation defers only its page and does not terminate the book", async () => {
  const { local, sends } = await nativePageFixture(),
    time = Math.floor(Date.now() / 1000);
  await bindings.DB.prepare(
    "INSERT INTO storage_ledger(key,size,created_at,owner,lease_until) VALUES ('fixture/missing-page',?,?,'live-owner',?)",
  )
    .bind(reservationPng.length, time, time + 300)
    .run();
  nativeImageReply();
  await runJob(local, "native-bad");
  expect(
    await bindings.DB.prepare(
      "SELECT state,attempts,message FROM comic_jobs WHERE id='native-bad'",
    ).first(),
  ).toEqual({
    state: "queued",
    attempts: 1,
    message: "文件正在上传，请稍后重试",
  });
  expect(
    await bindings.DB.prepare(
      "SELECT state FROM comic_jobs WHERE id='native-root'",
    ).first("state"),
  ).toBe("waiting");
  expect(sends[0].options.delaySeconds).toBeGreaterThanOrEqual(300);
  expect(
    await bindings.DB.prepare(
      "SELECT owner FROM storage_ledger WHERE key='fixture/missing-page'",
    ).first("owner"),
  ).toBe("live-owner");
});
it("reservation refusal cancels the fetched image body instead of leaving a connection open", async () => {
  const time = Math.floor(Date.now() / 1000);
  await reserve(bindings, "fixture/live-transfer", 10, "live-owner");
  let cancelled = false;
  const body = new ReadableStream<Uint8Array>({
    cancel() {
      cancelled = true;
    },
  });
  const { putStream } = await import("../src/storage");
  await expect(
    putStream(bindings, "fixture/live-transfer", body, 10, "image/png"),
  ).rejects.toThrow("上传");
  expect(cancelled).toBe(true);
});
it("comic integrity audits require an administrator and inspect R2 without changing download state", async () => {
  await nativePageFixture();
  const path = "/api/admin/comics/c_test/audit?task_id=native-root";
  expect((await SELF.fetch(origin + path)).status).toBe(401);
  const { cookie } = await login();
  const response = await fullGet(path, cookie);
  expect(response.status).toBe(200);
  const data = ((await response.json()) as any).data;
  expect(data.task.state).toBe("waiting");
  expect(data.chapters).toHaveLength(1);
  const manifest = (await (
    await fullGet("/api/admin/comics/c_test/audit?chapter_id=ch_test", cookie)
  ).json()) as any;
  expect(manifest.data.keys).toEqual(["fixture/page", "fixture/missing-page"]);
  await bindings.DB.prepare(
    "UPDATE users SET role='user',can_download=0 WHERE id='admin'",
  ).run();
  expect((await fullGet(path, cookie)).status).toBe(403);
});
it("the storage-conflict repair preserves completed pages, is idempotent, and never revives cancellations", async () => {
  const { local, sends } = await nativePageFixture();
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='error',message='部分章节失败' WHERE id='native-root'",
  ).run();
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='error' WHERE id='native-child'",
  ).run();
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='error',attempts=1,message='文件正在上传，请稍后重试' WHERE id='native-bad'",
  ).run();
  expect(await repairNativeStorage(local, "native-root")).toMatchObject({
    repaired: true,
    pages: 1,
    chapters: 1,
  });
  expect(
    await bindings.DB.prepare(
      "SELECT state,attempts FROM comic_jobs WHERE id='native-bad'",
    ).first(),
  ).toEqual({ state: "queued", attempts: 0 });
  expect(
    await bindings.DB.prepare(
      "SELECT state,attempts FROM comic_jobs WHERE id='native-good'",
    ).first(),
  ).toEqual({ state: "completed", attempts: 0 });
  expect(sends).toHaveLength(1);
  expect(await repairNativeStorage(local, "native-root")).toEqual({
    repaired: false,
    already_repaired: true,
  });
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='cancelled',payload=json_remove(payload,'$.native_storage_repaired') WHERE id='native-root'",
  ).run();
  await expect(repairNativeStorage(local, "native-root")).rejects.toThrow(
    "只能修复",
  );
});
it("a transient page error retries only that page; exhausted retries use one-page cloud fallback without cancelling siblings", async () => {
  const { local, sends } = await nativePageFixture();
  fetchMock
    .get("https://c-nd3-1.6wm.top")
    .intercept({ path: "/failure.jpg" })
    .reply(502, "unavailable")
    .times(2);
  await runJob(local, "native-bad");
  const first = await bindings.DB.prepare(
    "SELECT state,attempts,lease_until FROM comic_jobs WHERE id='native-bad'",
  ).first<any>();
  expect(first).toMatchObject({ state: "queued", attempts: 1 });
  expect(first.lease_until).toBeGreaterThan(Math.floor(Date.now() / 1000));
  expect(sends).toHaveLength(1);
  expect(sends[0].options.delaySeconds).toBe(60);
  expect(
    (await bindings.DB.prepare("SELECT id FROM compute_jobs").all()).results,
  ).toHaveLength(0);
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET attempts=4,lease_until=0 WHERE id='native-bad'",
  ).run();
  await runJob(local, "native-bad");
  expect(
    await bindings.DB.prepare(
      "SELECT state FROM comic_jobs WHERE id='native-bad'",
    ).first("state"),
  ).toBe("waiting");
  expect(
    await bindings.DB.prepare(
      "SELECT state FROM comic_jobs WHERE id='native-good'",
    ).first("state"),
  ).toBe("completed");
  expect(
    await bindings.DB.prepare(
      "SELECT state FROM comic_jobs WHERE id='native-root'",
    ).first("state"),
  ).toBe("waiting");
  const compute = await bindings.DB.prepare(
    "SELECT kind,payload FROM compute_jobs",
  ).first<any>();
  expect(compute.kind).toBe("legacy-page");
  expect(JSON.parse(compute.payload).parent_job).toBe("native-bad");
  expect((await bindings.LIBRARY.get("fixture/page"))?.size).toBe(4);
});
it("one-page cloud completion fills the original R2 key and completes the original chapter and root", async () => {
  const { local, manifest } = await nativePageFixture();
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='waiting' WHERE id='native-bad'",
  ).run();
  const data = JSON.parse(
    (
      await bindings.DB.prepare(
        "SELECT payload FROM comic_jobs WHERE id='native-bad'",
      ).first<any>()
    ).payload,
  );
  const token = await seedCompute(
    "native-page-compute",
    { ...data, provider: "baozimh_org", parent_job: "native-bad" },
    "legacy-page",
  );
  const png = new Uint8Array([
    137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 13, 73, 72, 68, 82,
  ]);
  await reviewCompute(
    new Request(
      origin + "/api/admin/compute/jobs/native-page-compute/output/page/0",
      {
        method: "PUT",
        headers: { "X-Compute-Lease": token, "Content-Type": "image/png" },
        body: png,
      },
    ),
    local,
    reviewUser,
  );
  await reviewCompute(
    computeRequest("native-page-compute", "complete", token),
    local,
    reviewUser,
  );
  expect(
    await (await bindings.LIBRARY.get("fixture/missing-page"))?.arrayBuffer(),
  ).toEqual(png.buffer);
  expect(
    await (await bindings.LIBRARY.get("fixture/manifest"))?.json(),
  ).toEqual(manifest);
  expect((await bindings.LIBRARY.get("fixture/page"))?.size).toBe(4);
  for (const id of ["native-bad", "native-child", "native-root"])
    expect(
      await bindings.DB.prepare("SELECT state FROM comic_jobs WHERE id=?")
        .bind(id)
        .first("state"),
    ).toBe("completed");
  expect(
    await bindings.DB.prepare(
      "SELECT state FROM chapters WHERE id='ch_test'",
    ).first("state"),
  ).toBe("ready");
  await cleanupStorage(bindings);
  expect(
    await bindings.LIBRARY.head(`compute/native-page-compute/${token}/page/0`),
  ).toBeNull();
  expect((await bindings.LIBRARY.get("fixture/missing-page"))?.size).toBe(
    png.length,
  );
});
it("cloud failures retain parents for two retries, enforce backoff, preserve safe error metadata, and terminate at attempt three", async () => {
  const { local } = await nativePageFixture();
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='waiting' WHERE id='native-bad'",
  ).run();
  let token = await seedCompute(
    "native-failure",
    { parent_job: "native-bad" },
    "legacy-page",
  );
  for (let attempt = 1; attempt <= 3; attempt++) {
    const result = await reviewCompute(
      computeRequest("native-failure", "fail", token, {
        error_type: "HTTPError",
        status: 503,
        message: "private secret URL",
      }),
      local,
      reviewUser,
    );
    expect(result!.status).toBe(200);
    const compute = await bindings.DB.prepare(
      "SELECT state,attempts,message FROM compute_jobs WHERE id='native-failure'",
    ).first<any>();
    expect(compute.attempts).toBe(attempt);
    expect(JSON.parse(compute.message)).toEqual({
      error_type: "HTTPError",
      status: 503,
    });
    expect(compute.message).not.toContain("private");
    if (attempt < 3) {
      expect(compute.state).toBe("queued");
      expect(
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='native-root'",
        ).first("state"),
      ).toBe("waiting");
      await expect(
        reviewCompute(
          computeRequest("native-failure", "claim", token),
          local,
          reviewUser,
        ),
      ).rejects.toThrow("正在运行");
      const jobs = await reviewCompute(
        new Request(origin + "/api/admin/compute/jobs"),
        local,
        reviewUser,
      );
      const listed = await jobs!.json<any>();
      expect(listed.data.some((j: any) => j.id === "native-failure")).toBe(
        false,
      );
      expect(listed.maintenance_pending).toBe(true);
      await bindings.DB.prepare(
        "UPDATE compute_jobs SET lease_until=0 WHERE id='native-failure'",
      ).run();
      token = (
        await (await reviewCompute(
          computeRequest("native-failure", "claim", token),
          local,
          reviewUser,
        ))!.json<any>()
      ).data.lease_token;
    } else {
      expect(compute.state).toBe("error");
      expect(
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='native-root'",
        ).first("state"),
      ).toBe("error");
      expect(
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='native-good'",
        ).first("state"),
      ).toBe("completed");
    }
  }
});
it("the one-time old-fallback repair keeps completed pages and attempt counts and never revives cancelled roots", async () => {
  const { local, sends } = await nativePageFixture();
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='error' WHERE id IN ('native-root','native-child')",
  ).run();
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='cancelled',message='改由云端兼容下载',attempts=1 WHERE id='native-bad'",
  ).run();
  await bindings.DB.prepare(
    "INSERT INTO compute_jobs(id,kind,input_key,payload,state,created_at,updated_at) VALUES ('old-fallback','legacy-chapter','old-input',?,'error',1,1)",
  )
    .bind(JSON.stringify({ parent_job: "native-child" }))
    .run();
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='cancelled' WHERE id='native-root'",
  ).run();
  await expect(repairNativeFallback(local, "native-root")).rejects.toThrow(
    "只能修复",
  );
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='error' WHERE id='native-root'",
  ).run();
  expect(await repairNativeFallback(local, "native-root")).toMatchObject({
    repaired: true,
    chapters: 1,
    pages: 1,
  });
  expect(
    await bindings.DB.prepare(
      "SELECT state,attempts FROM comic_jobs WHERE id='native-bad'",
    ).first(),
  ).toMatchObject({ state: "queued", attempts: 1 });
  expect(
    await bindings.DB.prepare(
      "SELECT state FROM comic_jobs WHERE id='native-good'",
    ).first("state"),
  ).toBe("completed");
  expect(sends).toHaveLength(1);
  expect(sends[0].body.ids).toEqual(["native-bad"]);
  expect(await repairNativeFallback(local, "native-root")).toMatchObject({
    already_repaired: true,
  });
  expect(sends).toHaveLength(1);
});
it("one-page compute callbacks cannot publish after the source root was cancelled", async () => {
  const { local } = await nativePageFixture();
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='waiting' WHERE id='native-bad'",
  ).run();
  const token = await seedCompute(
    "cancel-page-compute",
    { parent_job: "native-bad", key: "fixture/missing-page" },
    "legacy-page",
  );
  await bindings.DB.prepare(
    "UPDATE comic_jobs SET state='cancelled' WHERE id='native-root'",
  ).run();
  await expect(
    reviewCompute(
      computeRequest("cancel-page-compute", "fail", token),
      local,
      reviewUser,
    ),
  ).rejects.toThrow("任务已停止");
  await expect(
    reviewCompute(
      computeRequest("cancel-page-compute", "output/page/0", token),
      local,
      reviewUser,
    ),
  ).rejects.toThrow("任务已停止");
  expect(await bindings.LIBRARY.head("fixture/missing-page")).toBeNull();
});
it("administrator diagnostics remain available through the jobs component when the web budget is exhausted", async () => {
  const web = bindings.APPLICATION.get(bindings.APPLICATION.idFromName("web"));
  await runInDurableObject(web, async (_instance, state) =>
    state.storage.put("database-outbound-budget-v1", {
      day: new Date().toISOString().slice(0, 10),
      requests: 10000,
      statements: 12000,
      blockedUntil: 0,
      failures: 0,
      failureWindow: Date.now(),
    }),
  );
  const session = await login();
  const response = await SELF.fetch(origin + "/api/admin/resource-status", {
    headers: { Cookie: session.cookie },
  });
  expect(response.status).toBe(200);
  const report = await response.json<any>();
  expect(report.database.paused).toBe(false);
  expect(report.components.web).toMatchObject({
    requests: 10000,
    statements: 12000,
    paused: true,
    pause_reason: "request_limit",
  });
});
it("an R2 rejection before reading an image aborts the backpressured transfer and releases its reservation", async () => {
  let cancelled = false;
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new Uint8Array(1024 * 1024));
    },
    cancel() {
      cancelled = true;
    },
  });
  const key = "comics/stream-rejection/page";
  const library = new Proxy(bindings.LIBRARY, {
    get(target, property) {
      if (property === "put")
        return async () => {
          throw new Error("synthetic R2 unavailable");
        };
      const value = (target as any)[property];
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  const { putStream } = await import("../src/storage");
  let timer: any;
  try {
    await expect(
      Promise.race([
        putStream(
          { ...bindings, LIBRARY: library },
          key,
          body,
          1024 * 1024,
          "image/png",
        ),
        new Promise((_resolve, reject) => {
          timer = setTimeout(
            () => reject(new Error("transfer remained stuck")),
            1000,
          );
        }),
      ]),
    ).rejects.toThrow("文件传输失败");
    expect(cancelled).toBe(true);
    expect(
      await bindings.DB.prepare("SELECT key FROM storage_ledger WHERE key=?")
        .bind(key)
        .first(),
    ).toBeNull();
  } finally {
    clearTimeout(timer);
  }
});
