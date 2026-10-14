import test from "node:test";
import assert from "node:assert/strict";
import { safeEvent } from "../scripts/update-diagnostics.mjs";
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
  const result = safeEvent({ source: { message: JSON.stringify({
    component: "control-storage", error_code: "DURABLE_STORAGE_QUOTA",
    operation: "legacy-list", reason_words: ["account", "daily", "rows", "written", "exceeded", "private-token", "https://private.invalid", "SELECT", { credential: "private" }],
  }) } });
  assert.equal(result.storage_operation, "legacy-list");
  assert.deepEqual(result.storage_reason_words, ["account", "daily", "rows", "written", "exceeded"]);
  assert.ok(!JSON.stringify(result).includes("private"));
  const rejected = safeEvent({ source: { component: "control-storage", operation: "private-operation" } });
  assert.equal(rejected.storage_operation, undefined);
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
