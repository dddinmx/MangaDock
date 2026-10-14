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
