import test from "node:test";
import assert from "node:assert/strict";
import { verifyNovelSource } from "../scripts/verify-novel-source.mjs";

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
