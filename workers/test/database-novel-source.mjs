import test from "node:test";
import assert from "node:assert/strict";
import { verifyNovelSource } from "../scripts/verify-novel-source.mjs";
import { initializeNovelSource } from "../scripts/initialize-novel-source.mjs";

const url = "https://mangadock-turso-staging.dddinmx.workers.dev";
function fixture({
  searchStatus = 200,
  html = '<form action="/novels/fanqie/download">直接下载</form>',
} = {}) {
  const calls = [];
  const fetch = async (input, options) => {
    calls.push([input, options]);
    assert.equal(options.redirect, "error");
    if (input.endsWith("/api/v1/auth/login")) {
      assert.equal(options.method, "POST");
      return Response.json(
        { data: { csrf_token: "synthetic-csrf" } },
        { headers: { "set-cookie": "md_session=synthetic-cookie; HttpOnly" } },
      );
    }
    assert.equal(options.method || "GET", "GET");
    assert.equal(options.headers.Cookie, "md_session=synthetic-cookie");
    if (input.includes("/api/search?"))
      return Response.json(
        { ok: true, data: { items: [{ book_id: "1234567890" }] } },
        { status: searchStatus },
      );
    assert.equal(input, url + "/novels/fanqie");
    return new Response(html);
  };
  return { calls, fetch };
}
test("restored novel source verification searches and checks the form without creating jobs or writing owner progress", async () => {
  const f = fixture();
  const report = await verifyNovelSource(url, "synthetic-password", f.fetch);
  assert.equal(report.source_search_available, true);
  assert.equal(report.download_entry_available, true);
  assert.equal(report.download_exercised, false);
  assert.equal(report.new_download_created, false);
  assert.equal(report.original_user_progress_untouched, true);
  assert.equal(f.calls.length, 3);
  assert.doesNotMatch(JSON.stringify(report), /synthetic/);
});
test("novel source check fails on unavailable source or missing download form", async () => {
  for (const f of [
    fixture({ searchStatus: 503 }),
    fixture({ html: "unavailable" }),
  ])
    await assert.rejects(verifyNovelSource(url, "synthetic-password", f.fetch));
});
test("novel availability verification refuses production and missing credentials before networking", async () => {
  let calls = 0;
  const fetch = async () => {
    calls++;
    throw Error("unexpected request");
  };
  await assert.rejects(
    verifyNovelSource("https://production.example", "synthetic", fetch),
  );
  await assert.rejects(verifyNovelSource(url, "", fetch));
  assert.equal(calls, 0);
});

test("source initialization adds one missing credential pair and preserves all existing settings without touching progress", async () => {
  const original = [
    { name: "COMPUTE_REF", type: "plain_text", text: "a".repeat(40) },
    { name: "TURSO_AUTH_TOKEN", type: "secret_text" },
    { name: "LIBRARY", type: "r2_bucket", bucket_name: "existing-library" },
  ];
  let settings = 0,
    registrations = 0,
    stored;
  const request = async (input, options) => {
    assert.equal(options.redirect, "error");
    if (input.startsWith("https://api.cloudflare.com/")) {
      settings++;
      return Response.json({
        success: true,
        result: {
          bindings:
            settings === 1
              ? original
              : [
                  ...original,
                  ...Object.keys(stored).map((name) => ({
                    name,
                    type: "secret_text",
                  })),
                ],
        },
      });
    }
    if (input.endsWith("/api/v1/auth/login"))
      return Response.json(
        { data: { csrf_token: "synthetic-csrf" } },
        { headers: { "set-cookie": "md_session=synthetic" } },
      );
    assert.equal(input, url + "/api/source/register");
    assert.equal(options.method, "POST");
    const data = JSON.parse(options.body);
    assert.match(data.instance, /^[a-f0-9]{32}$/);
    assert.match(data.registration, /^fqr_[A-Za-z0-9_-]{43}$/);
    registrations++;
    return Response.json({
      ok: true,
      data: { token: "fqk_" + "x".repeat(43) },
    });
  };
  const report = await initializeNovelSource(
    {
      CLOUDFLARE_ACCOUNT_ID: "a".repeat(32),
      CLOUDFLARE_API_TOKEN: "synthetic",
      MANGADOCK_ADMIN_PASSWORD: "synthetic",
    },
    request,
    async (pair) => {
      stored = pair;
    },
  );
  assert.equal(registrations, 1);
  assert.equal(settings, 2);
  assert.equal(report.compute_ref_unchanged, true);
  assert.equal(report.new_registration, true);
  assert.deepEqual(Object.keys(stored).sort(), [
    "FANQIE_API_TOKEN",
    "FANQIE_INSTANCE_ID",
  ]);
  assert.doesNotMatch(JSON.stringify(report), /fqk_|synthetic/);
});

test("source initialization preserves an existing pair and refuses partial credentials without registration", async () => {
  for (const complete of [false, true]) {
    let calls = 0;
    const bindings = [
      { name: "FANQIE_API_TOKEN", type: "secret_text" },
      ...(complete
        ? [{ name: "FANQIE_INSTANCE_ID", type: "secret_text" }]
        : []),
    ];
    const request = async () => {
      calls++;
      return Response.json({ success: true, result: { bindings } });
    };
    const init = initializeNovelSource(
      {
        CLOUDFLARE_ACCOUNT_ID: "a".repeat(32),
        CLOUDFLARE_API_TOKEN: "synthetic",
        MANGADOCK_ADMIN_PASSWORD: "synthetic",
      },
      request,
      async () => {
        throw Error("must not write");
      },
    );
    if (complete)
      assert.equal((await init).existing_credentials_preserved, true);
    else await assert.rejects(init, /partially configured/);
    assert.equal(calls, 1);
  }
});
