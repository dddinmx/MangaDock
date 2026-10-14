import { createClient } from "@libsql/client/web";
import { tursoUrl } from "../src/database.ts";
const url = "https://mangadock-turso-staging.dddinmx.workers.dev";
const id = process.env.STOP_TASK_ID;
const from = Date.parse(process.env.STOP_CREATED_FROM) / 1000;
const to = Date.parse(process.env.STOP_CREATED_TO) / 1000;
if (
  !/^[a-zA-Z0-9:_-]{1,160}$/.test(id || "") ||
  !Number.isFinite(from) ||
  !Number.isFinite(to) ||
  to < from ||
  to - from > 600
)
  throw new Error("Invalid audited CI task window");
const client = createClient({
  url: tursoUrl(process.env.TURSO_DATABASE_URL),
  authToken: process.env.TURSO_AUTH_TOKEN,
});
try {
  const { rows } = await client.execute({
    sql: "SELECT j.id,j.state,j.kind,j.created_at,json_extract(j.payload,'$.url') AS source_url,c.id AS comic_id,c.created_at AS comic_created,c.chapter_count FROM comic_jobs j LEFT JOIN comics c ON c.id=j.comic_id WHERE j.id=?",
    args: [id],
  });
  const row = rows[0];
  if (
    !row ||
    row.kind !== "source" ||
    row.source_url !== "https://baozimh.org/manga/dafengdagengren-chuyingshe" ||
    row.created_at < from ||
    row.created_at > to
  )
    throw new Error("Task does not match the audited CI download");
  const login = await fetch(url + "/api/v1/auth/login", {
    method: "POST",
    headers: { Origin: url, "Content-Type": "application/json" },
    body: JSON.stringify({
      username: "admin",
      password: process.env.MANGADOCK_ADMIN_PASSWORD,
    }),
  });
  if (!login.ok) throw new Error("Stop login failed");
  const cookie = login.headers.get("set-cookie")?.split(";")[0];
  const csrf = (await login.json()).data.csrf_token;
  const headers = {
    Origin: url,
    Cookie: cookie,
    "X-CSRFToken": csrf,
    "Content-Type": "application/json",
  };
  if (["queued", "running", "waiting"].includes(row.state)) {
    const response = await fetch(
      `${url}/api/v1/tasks/${encodeURIComponent(id)}/cancel`,
      { method: "POST", headers, body: "{}" },
    );
    if (!response.ok) throw new Error("Audited task cancellation failed");
  }
  // Remove only metadata newly created by this CI run, restoring the user's deletion.
  let removed = false;
  if (row.comic_id && row.comic_created >= from && row.comic_created <= to) {
    const response = await fetch(url + "/comic/delete", {
      method: "POST",
      headers,
      body: JSON.stringify({ comic_name: row.comic_id }),
      redirect: "manual",
    });
    if (response.status !== 303) throw new Error("CI comic cleanup failed");
    removed = true;
  }
  const after = await client.execute({
    sql: "SELECT state FROM comic_jobs WHERE id=?",
    args: [id],
  });
  if (["queued", "running", "waiting"].includes(after.rows[0]?.state))
    throw new Error("CI task remains active");
  console.log(
    JSON.stringify({
      task_id: id,
      state: after.rows[0]?.state,
      ci_comic_removed: removed,
    }),
  );
} catch (error) {
  console.error("::error::Audited CI stop failed: " + error.name);
  process.exitCode = 1;
} finally {
  client.close();
}
