import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseBudget, DatabasePausedError } from "../src/database-budget.ts";
function storage() {
  const data = new Map();
  const stats = { writes: 0 };
  let tail = Promise.resolve();
  return {
    stats,
    get: async (key) => structuredClone(data.get(key)),
    put: async (key, value) => {
      stats.writes++;
      data.set(key, structuredClone(value));
    },
    transaction(fn) {
      const next = tail.then(() => fn(this));
      tail = next.catch(() => {});
      return next;
    },
  };
}
test("64-credit leases reduce 4096 counter writes to 64 without overspending under concurrency", async () => {
  const store = storage();
  const budget = new DatabaseBudget(
    store,
    Date.now,
    { requests: 4096, statements: 4096 },
    64,
  );
  const attempts = await Promise.allSettled(
    Array.from({ length: 4200 }, () => budget.reserve(1)),
  );
  assert.equal(
    attempts.filter((item) => item.status === "fulfilled").length,
    4096,
  );
  assert.equal(store.stats.writes, 64);
  assert.equal((await budget.snapshot()).statements, 4096);
});
test("partial credit blocks stop at the exact ceiling and failed persistence grants no credits", async () => {
  const store = storage();
  const budget = new DatabaseBudget(
    store,
    Date.now,
    { requests: 70, statements: 70 },
    64,
  );
  for (let i = 0; i < 70; i++) await budget.reserve(1);
  await assert.rejects(budget.reserve(1), DatabasePausedError);
  assert.equal((await budget.snapshot()).requests, 70);
  assert.equal(store.stats.writes, 2);
  const broken = new DatabaseBudget(
    {
      ...storage(),
      put: async () => {
        throw new Error("write failed");
      },
    },
    Date.now,
    {},
    64,
  );
  await assert.rejects(broken.reserve(1), /write failed/);
  await assert.rejects(broken.reserve(1), /write failed/);
});
test("a snapshot does not pause usable leased credits, while reconstruction still loses them conservatively", async () => {
  const store = storage();
  const budget = new DatabaseBudget(
    store,
    Date.now,
    { requests: 64, statements: 64 },
    64,
  );
  await budget.reserve(1);
  const active = await budget.snapshot();
  assert.equal(active.requests, 64);
  assert.equal(active.unused_request_credits, 63);
  assert.equal(active.paused, false);
  assert.equal(
    (
      await new DatabaseBudget(
        store,
        Date.now,
        { requests: 64, statements: 64 },
        64,
      ).snapshot()
    ).paused,
    true,
  );
  for (let i = 0; i < 63; i++) await budget.reserve(1);
  assert.equal((await budget.snapshot()).paused, true);
});
test("reconstruction loses unused credits conservatively and a new UTC day discards old credits", async () => {
  let time = Date.UTC(2026, 9, 14, 23, 59);
  const store = storage();
  const first = new DatabaseBudget(
    store,
    () => time,
    { requests: 100, statements: 100 },
    64,
  );
  await first.reserve(1);
  const rebuilt = new DatabaseBudget(
    store,
    () => time,
    { requests: 100, statements: 100 },
    64,
  );
  for (let i = 0; i < 36; i++) await rebuilt.reserve(1);
  await assert.rejects(rebuilt.reserve(1), DatabasePausedError);
  time += 60000;
  for (let i = 0; i < 100; i++) await rebuilt.reserve(1);
  await assert.rejects(rebuilt.reserve(1), DatabasePausedError);
});
test("large SQL batches consume their true size and provider holds override unused leased credits", async () => {
  const store = storage();
  const budget = new DatabaseBudget(
    store,
    Date.now,
    { requests: 1000, statements: 130 },
    64,
  );
  await budget.reserve(100);
  await budget.reserve(30);
  await assert.rejects(budget.reserve(1), DatabasePausedError);
  assert.equal((await budget.snapshot()).statements, 130);
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => Response.json({}, { status: 429 });
    const held = new DatabaseBudget(storage(), Date.now, {}, 64);
    await held.guardedFetch(request());
    await assert.rejects(held.reserve(1), DatabasePausedError);
  } finally {
    globalThis.fetch = original;
  }
});
test("rejected loops do not repeatedly write the DO budget counter", async () => {
  const store = storage(),
    budget = new DatabaseBudget(store);
  await budget.reserve(10000);
  const writes = store.stats.writes;
  for (let i = 0; i < 100; i++)
    await assert.rejects(budget.reserve(1), DatabasePausedError);
  assert.equal(store.stats.writes, writes);
});
const request = (steps = 1) =>
  new Request("https://fixture.turso.io/v2/pipeline", {
    method: "POST",
    body: JSON.stringify({
      requests: [
        {
          type: "batch",
          batch: {
            steps: Array.from({ length: steps }, () => ({
              stmt: { sql: "SELECT 1" },
            })),
          },
        },
      ],
    }),
  });
test("daily SQL budget is atomic under concurrency and survives DO reconstruction", async () => {
  const store = storage(),
    budget = new DatabaseBudget(store);
  const attempts = await Promise.allSettled(
    Array.from({ length: 21 }, () => budget.reserve(500)),
  );
  assert.equal(
    attempts.filter((result) => result.status === "fulfilled").length,
    20,
  );
  await assert.rejects(
    new DatabaseBudget(store).reserve(1),
    DatabasePausedError,
  );
});
test("outbound request cap includes empty requests and resets once per UTC day", async () => {
  let time = Date.UTC(2026, 9, 13, 23, 59);
  const store = storage(),
    budget = new DatabaseBudget(store, () => time);
  for (let i = 0; i < 2000; i++) await budget.reserve(0);
  await assert.rejects(budget.reserve(0), DatabasePausedError);
  time += 60000;
  await new DatabaseBudget(store, () => time).reserve(1);
});
test("oversized batches are blocked before sending any request to Turso", async () => {
  const original = globalThis.fetch;
  let sent = 0;
  try {
    globalThis.fetch = async () => {
      sent++;
      return Response.json({});
    };
    await assert.rejects(
      new DatabaseBudget(storage()).guardedFetch(request(10001)),
      DatabasePausedError,
    );
    assert.equal(sent, 0);
  } finally {
    globalThis.fetch = original;
  }
});
test("three network failures open a persisted circuit and prevent further provider calls", async () => {
  const original = globalThis.fetch,
    store = storage();
  let time = Date.UTC(2026, 9, 13),
    sent = 0;
  try {
    globalThis.fetch = async () => {
      sent++;
      throw new TypeError("fixture outage");
    };
    const budget = new DatabaseBudget(store, () => time);
    for (let i = 0; i < 3; i++)
      await assert.rejects(budget.guardedFetch(request()));
    for (let i = 0; i < 20; i++)
      await assert.rejects(
        new DatabaseBudget(store, () => time, {
          requests: 200000,
          statements: 200000,
        }).guardedFetch(request()),
        DatabasePausedError,
      );
    assert.equal(sent, 3);
    time += 600001;
    await assert.rejects(budget.guardedFetch(request()));
    assert.equal(sent, 4);
  } finally {
    globalThis.fetch = original;
  }
});
test("quota errors inside HTTP-200 SQL batches pause until the next UTC month", async () => {
  const original = globalThis.fetch,
    store = storage();
  let time = Date.UTC(2026, 9, 13),
    sent = 0;
  try {
    globalThis.fetch = async () => {
      sent++;
      return Response.json({
        results: [
          {
            type: "ok",
            response: {
              result: {
                step_errors: [
                  {
                    code: "USAGE_LIMIT_EXCEEDED",
                    message: "monthly quota exceeded",
                  },
                ],
              },
            },
          },
        ],
      });
    };
    const budget = new DatabaseBudget(store, () => time);
    await budget.guardedFetch(request());
    time += 86400000;
    await assert.rejects(
      new DatabaseBudget(store, () => time).guardedFetch(request()),
      DatabasePausedError,
    );
    assert.equal(sent, 1);
    time = Date.UTC(2026, 10, 1);
    await budget.guardedFetch(request());
    assert.equal(sent, 2);
  } finally {
    globalThis.fetch = original;
  }
});
test("401, 403 and 429 stop immediate retries without counting rejected attempts as provider calls", async () => {
  const original = globalThis.fetch;
  try {
    for (const status of [401, 403, 429]) {
      let sent = 0;
      globalThis.fetch = async () => {
        sent++;
        return Response.json({}, { status });
      };
      const budget = new DatabaseBudget(storage());
      await budget.guardedFetch(request());
      await assert.rejects(budget.guardedFetch(request()), DatabasePausedError);
      assert.equal(sent, 1);
    }
  } finally {
    globalThis.fetch = original;
  }
});
test("ordinary constraint errors do not lock out valid work", async () => {
  const original = globalThis.fetch;
  let sent = 0;
  try {
    globalThis.fetch = async () => {
      sent++;
      return Response.json({
        results: [
          {
            type: "error",
            error: { code: "SQLITE_CONSTRAINT", message: "fixture duplicate" },
          },
        ],
      });
    };
    const budget = new DatabaseBudget(storage());
    await budget.guardedFetch(request());
    await budget.guardedFetch(request());
    assert.equal(sent, 2);
  } finally {
    globalThis.fetch = original;
  }
});
test("configured budgets admit more normal traffic and expose recovery time", async () => {
  const store = storage(),
    clock = () => Date.UTC(2026, 9, 13, 23, 59),
    budget = new DatabaseBudget(store, clock, {
      requests: 10000,
      statements: 50000,
    });
  for (let i = 0; i < 2100; i++) await budget.reserve(1);
  await budget.reserve(47900);
  await assert.rejects(
    budget.reserve(1),
    (error) => error instanceof DatabasePausedError && error.retryAfter === 60,
  );
});
test("invalid budget configuration is rejected", () => {
  assert.throws(() => new DatabaseBudget(storage(), Date.now, { requests: 0 }));
  assert.throws(
    () => new DatabaseBudget(storage(), Date.now, { statements: Infinity }),
  );
});

test("budget snapshots expose actual limits and recovery without SQL or counter writes", async () => {
  let time = Date.UTC(2026, 9, 14, 6);
  const store = storage();
  const budget = new DatabaseBudget(store, () => time, {
    requests: 2,
    statements: 20,
  });
  assert.equal((await budget.snapshot()).requests, 0);
  assert.equal(store.stats.writes, 0);
  await budget.reserve(1);
  await budget.reserve(1);
  const writes = store.stats.writes;
  const exhausted = await budget.snapshot();
  assert.equal(exhausted.requests, 2);
  assert.equal(exhausted.paused, true);
  assert.equal(exhausted.retry_after, 18 * 3600);
  assert.equal(store.stats.writes, writes);
  time = Date.UTC(2026, 9, 15);
  const fresh = await budget.snapshot();
  assert.equal(fresh.requests, 0);
  assert.equal(fresh.paused, false);
  assert.equal(store.stats.writes, writes);
});

test("successful provider responses do not rewrite an unchanged failure counter", async () => {
  const original = globalThis.fetch,
    store = storage(),
    budget = new DatabaseBudget(store);
  try {
    globalThis.fetch = async () => Response.json({ results: [] });
    await budget.guardedFetch(request());
    await budget.guardedFetch(request());
    assert.equal(store.stats.writes, 2);
    assert.equal((await budget.snapshot()).requests, 2);
  } finally {
    globalThis.fetch = original;
  }
});
test("raising both job limits preserves usage and pauses, and still enforces the SQL ceiling", async () => {
  const store = storage();
  const old = new DatabaseBudget(store, Date.now, {
    requests: 1,
    statements: 1,
  });
  await old.reserve(1);
  await assert.rejects(old.reserve(1), DatabasePausedError);
  const raised = new DatabaseBudget(store, Date.now, {
    requests: 200000,
    statements: 200000,
  });
  const snapshot = await raised.snapshot();
  assert.equal(snapshot.requests, 1);
  assert.equal(snapshot.statements, 1);
  assert.equal(snapshot.request_limit, 200000);
  assert.equal(snapshot.statement_limit, 200000);
  assert.equal(snapshot.paused, false);
  await raised.reserve(199999);
  await assert.rejects(raised.reserve(1), DatabasePausedError);
  const held = await raised.snapshot();
  assert.equal(held.requests, 2);
  assert.equal(held.statements, 200000);
});
test("failure snapshots distinguish Worker subrequest errors from provider server errors without raw messages", async () => {
  const original = globalThis.fetch;
  try {
    for (const kind of ["worker_subrequest_limit", "server_error"]) {
      const budget = new DatabaseBudget(
        storage(),
        () => Date.UTC(2026, 9, 14),
        { requests: 200000, statements: 200000 },
      );
      globalThis.fetch = async () => {
        if (kind === "worker_subrequest_limit")
          throw new Error("Too many subrequests; private-token-must-not-leak");
        return Response.json({}, { status: 503 });
      };
      for (let i = 0; i < 3; i++) {
        if (kind === "worker_subrequest_limit")
          await assert.rejects(budget.guardedFetch(request()));
        else await budget.guardedFetch(request());
      }
      const snapshot = await budget.snapshot();
      assert.equal(snapshot.paused, true);
      assert.equal(snapshot.pause_reason, kind);
      assert.equal(snapshot.last_failure.kind, kind);
      assert.equal(snapshot.retry_after, 600);
      assert.doesNotMatch(JSON.stringify(snapshot), /private-token/);
    }
  } finally {
    globalThis.fetch = original;
  }
});

test("an explicitly raised compute budget keeps all 10000 used credits and real failure holds", async () => {
  const clock = () => Date.UTC(2026, 9, 15, 12);
  for (const held of [false, true]) {
    const store = storage(),
      state = {
        day: "2026-10-15",
        requests: 10000,
        statements: 10176,
        blockedUntil: held ? clock() + 600000 : 0,
        failures: held ? 3 : 0,
        failureWindow: clock(),
        ...(held
          ? { lastFailure: { kind: "server_error", at: clock(), status: 503 } }
          : {}),
      };
    await store.put("database-outbound-budget-v1", state);
    const budget = new DatabaseBudget(
      store,
      clock,
      { requests: 200000, statements: 200000 },
      64,
    );
    const before = await budget.snapshot();
    assert.equal(before.requests, 10000);
    assert.equal(before.statements, 10176);
    assert.equal(before.paused, held);
    if (held) {
      await assert.rejects(budget.reserve(1), DatabasePausedError);
      assert.deepEqual(await store.get("database-outbound-budget-v1"), state);
    } else {
      await budget.reserve(1);
      const after = await budget.snapshot();
      assert.equal(after.requests, 10064);
      assert.equal(after.statements, 10240);
    }
  }
});
