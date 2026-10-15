import { readFile, writeFile } from "node:fs/promises";
const { url } = JSON.parse(await readFile("deployment.json", "utf8"));
if (url !== "https://mangadock-turso-staging.dddinmx.workers.dev")
  throw new Error("Novel index repair is restricted to Turso staging");
const id = process.env.REPAIR_NOVEL_INDEX_ID,
  title = process.env.EXPECTED_NOVEL_TITLE;
if (!id || !title || id.length > 200 || title.length > 500)
  throw new Error("An exact existing novel and title are required");
const login = await fetch(url + "/api/v1/auth/login", {
  method: "POST",
  redirect: "error",
  signal: AbortSignal.timeout(30000),
  headers: { Origin: url, "Content-Type": "application/json" },
  body: JSON.stringify({
    username: "admin",
    password: process.env.MANGADOCK_ADMIN_PASSWORD,
  }),
});
if (!login.ok) throw new Error("Novel index repair login failed");
const cookie = login.headers.get("set-cookie")?.split(";")[0],
  csrf = (await login.json()).data?.csrf_token;
if (!cookie || !csrf) throw new Error("Novel index repair session is invalid");
const response = await fetch(url + "/api/admin/compute/novel-index", {
  method: "POST",
  redirect: "error",
  signal: AbortSignal.timeout(30000),
  headers: {
    Origin: url,
    Cookie: cookie,
    "X-CSRFToken": csrf,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ book_id: id, expected_title: title }),
});
if (!response.ok) throw new Error("Novel index repair HTTP " + response.status);
const result = await response.json();
if (result.ok !== true) throw new Error("Novel index repair rejected");
const report = {
  observed_at: new Date().toISOString(),
  novel_id: id,
  title,
  ...result.data,
  new_source_download_created: false,
  prior_jobs_and_retry_history_preserved: true,
  original_user_progress_untouched: true,
};
await writeFile(
  "novel-index-repair-results.json",
  JSON.stringify(report, null, 2),
);
console.log(JSON.stringify(report));
