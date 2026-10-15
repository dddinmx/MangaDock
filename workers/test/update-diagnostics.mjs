import test from "node:test";
import assert from "node:assert/strict";
import {
  safeEvent,
  safeQuotaHold,
  durableStorageUsage,
} from "../scripts/update-diagnostics.mjs";
test("storage usage discovery sums real row metrics and does not treat absent samples as zero", async () => {
  for (const samples of [
    [],
    [
      { sum: { rowsWritten: 4, rowsRead: 7 } },
      { sum: { rowsWritten: 8, rowsRead: 10 } },
    ],
  ]) {
    const query = async (text) => {
      if (text.includes('__type(name: "Account")'))
        return {
          __type: {
            fields: [
              {
                name: "durableObjectsSqlStorageGroups",
                type: {
                  ofType: { ofType: { ofType: { name: "SqlStorage" } } },
                },
              },
            ],
          },
        };
      if (text.includes('__type(name: "SqlStorage")'))
        return {
          __type: { fields: [{ name: "sum", type: { name: "SqlSum" } }] },
        };
      if (text.includes('__type(name: "SqlSum")'))
        return {
          __type: { fields: [{ name: "rowsWritten" }, { name: "rowsRead" }] },
        };
      assert.ok(text.includes('date_geq: "2026-10-15"'));
      assert.ok(!text.includes("private"));
      return {
        viewer: { accounts: [{ durableObjectsSqlStorageGroups: samples }] },
      };
    };
    const result = await durableStorageUsage(query, "2026-10-15");
    assert.equal(result.rows_written, samples.length ? 12 : null);
    assert.equal(result.rows_read, samples.length ? 17 : null);
    assert.equal(result.samples, samples.length);
  }
});
test("quota reset diagnostics export only validated hold timestamps, never private control state", () => {
  const value = {
    version: 1,
    kind: "legacy_rows_written",
    until: 1792022460000,
    reset_at: 1792022400000,
    private_token: "private-token",
    values: { password: "private-password" },
  };
  assert.deepEqual(safeQuotaHold(value), {
    available: true,
    kind: "legacy_rows_written",
    until: value.until,
    reset_at: value.reset_at,
  });
  assert.ok(!JSON.stringify(safeQuotaHold(value)).includes("private"));
  for (const invalid of [
    null,
    {},
    { ...value, until: "private" },
    { ...value, reset_at: -1 },
    { ...value, kind: "private" },
  ])
    assert.deepEqual(safeQuotaHold(invalid), {
      available: false,
      invalid_schema: true,
    });
});
test("update log summaries exclude credentials, query strings, headers and raw error payloads", () => {
  const event = {
    timestamp: 123,
    $metadata: {
      trigger:
        "POST https://fixture.invalid/api/v1/updates?token=private-query",
      error: "private-error",
    },
    source: { password: "private-password" },
    $workers: {
      outcome: "ok",
      event: {
        request: {
          url: "https://fixture.invalid/api/v1/updates?token=private-query",
          method: "POST",
          headers: { Authorization: "private-header" },
        },
        response: { status: 200 },
      },
    },
  };
  const result = safeEvent(event);
  assert.deepEqual(result, {
    timestamp: 123,
    path: "/api/v1/updates",
    method: "POST",
    status: 200,
    outcome: "ok",
    event_type: undefined,
  });
  assert.ok(!JSON.stringify(result).includes("private"));
});
test("log summaries can recover a request path from metadata without exporting raw logs", () => {
  const result = safeEvent({
    timestamp: 123,
    $metadata: {
      trigger: "PUT https://fixture.invalid/api/v1/updates/mode?secret=private",
    },
    source: "raw private log",
  });
  assert.equal(result.path, "/api/v1/updates/mode");
  assert.ok(!JSON.stringify(result).includes("private"));
});
test("CPU evidence preserves invocation type and time without exporting Durable Object identifiers", () => {
  const result = safeEvent({
    source: {
      $workers: {
        eventType: "queue",
        cpuTimeMs: 20.647,
        durableObjectId: "private-id",
      },
    },
  });
  assert.equal(result.event_type, "queue");
  assert.equal(result.cpu_ms, 20.647);
  assert.equal(result.durable_object, true);
  assert.ok(!JSON.stringify(result).includes("private-id"));
});
test("control diagnostics retain only fixed storage phases and reason vocabulary", () => {
  const result = safeEvent({
    source: {
      message: JSON.stringify({
        component: "control-storage",
        error_code: "DURABLE_STORAGE_QUOTA",
        operation: "legacy-list",
        reason_words: [
          "account",
          "daily",
          "rows",
          "written",
          "exceeded",
          "private-token",
          "https://private.invalid",
          "SELECT",
          { credential: "private" },
        ],
      }),
    },
  });
  assert.equal(result.storage_operation, "legacy-list");
  assert.deepEqual(result.storage_reason_words, [
    "account",
    "daily",
    "rows",
    "written",
    "exceeded",
  ]);
  assert.ok(!JSON.stringify(result).includes("private"));
  const rejected = safeEvent({
    source: { component: "control-storage", operation: "private-operation" },
  });
  assert.equal(rejected.storage_operation, undefined);
});
test("short metadata messages do not discard richer structured source fields", () => {
  const result = safeEvent({
    source: {
      component: "control-storage",
      error_code: "DURABLE_STORAGE_QUOTA",
      operation: "legacy-list",
      reason_words: ["daily", "rows", "written"],
    },
    $metadata: {
      message: JSON.stringify({
        component: "control-storage",
        error_code: "DURABLE_STORAGE_QUOTA",
      }),
    },
  });
  assert.equal(result.storage_operation, "legacy-list");
  assert.deepEqual(result.storage_reason_words, ["daily", "rows", "written"]);
});
test("cloud failure summaries preserve the actual exception and HTTP status without exporting provider response text", () => {
  const result = safeEvent({
    timestamp: 123,
    source: {
      message: JSON.stringify({
        component: "cloud-compute",
        job_kind: "legacy-page",
        error_type: "HTTPError",
        status: 503,
        message: "private token URL",
        body: "private payload",
      }),
    },
  });
  assert.equal(result.component, "cloud-compute");
  assert.equal(result.error_type, "HTTPError");
  assert.equal(result.status, 503);
  assert.equal(result.job_kind, "legacy-page");
  assert.ok(!JSON.stringify(result).includes("private"));
});
