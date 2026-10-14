import test from "node:test";
import assert from "node:assert/strict";
import { waitForStaging } from "../scripts/wait-staging-ready.mjs";
const url = "https://mangadock-turso-staging.dddinmx.workers.dev";
test("readiness honors provider holds, then performs authenticated maintenance without creating a download", async () => {
  let time = 0;
  const calls = [],
    sleeps = [];
  const result = await waitForStaging(url, "synthetic-password", {
    clock: () => time,
    sleep: async (ms) => {
      sleeps.push(ms);
      time += ms;
    },
    fetch: async (path, init) => {
      calls.push({ path, init });
      if (calls.length === 1)
        return new Response("{}", {
          status: 503,
          headers: { "Retry-After": "120" },
        });
      if (path.endsWith("login"))
        return Response.json(
          { data: { csrf_token: "synthetic-csrf" } },
          { headers: { "Set-Cookie": "md_session=synthetic; HttpOnly" } },
        );
      assert.equal(path, url + "/api/admin/maintenance");
      assert.equal(init.headers.Cookie, "md_session=synthetic");
      assert.equal(init.headers["X-CSRFToken"], "synthetic-csrf");
      assert.equal(init.headers.Origin, url);
      return Response.json({ ok: true });
    },
  });
  assert.deepEqual(sleeps, [120000]);
  assert.equal(calls.length, 3);
  assert.equal(result.ready, true);
  assert.equal(result.new_download, false);
  assert.ok(!JSON.stringify(result).includes("synthetic"));
});
test("readiness never loops on invalid credentials or a hold lasting beyond its deadline", async () => {
  for (const status of [401, 403, 503]) {
    let calls = 0;
    await assert.rejects(
      waitForStaging(url, "synthetic-password", {
        fetch: async () => {
          calls++;
          return new Response("{}", {
            status,
            headers: { "Retry-After": "3600" },
          });
        },
        sleep: async () => {
          throw new Error("Unexpected retry");
        },
      }),
    );
    assert.equal(calls, 1);
  }
});
