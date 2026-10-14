import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { createClient } from "@libsql/client/web";
import { tursoUrl } from "../src/database.ts";

// Return only request metadata. Never export headers, bodies, raw log payloads,
// query strings, database credentials or exception text from provider responses.
export function safeEvent(event) {
  let source = event.source;
  if (typeof source === "string") {
    try {
      source = JSON.parse(source);
    } catch {
      source = {};
    }
  }
  const workers = event.$workers || source?.$workers || {},
    request = workers.event?.request || source?.request || {},
    response = workers.event?.response || source?.response || {};
  let path;
  try {
    path = new URL(request.url).pathname;
  } catch {
    const trigger = event.$metadata?.trigger || "";
    try {
      path = new URL(trigger.replace(/^[A-Z]+\s+/, "")).pathname;
    } catch {}
  }
  return {
    timestamp: event.timestamp,
    path,
    method: request.method,
    status: response.status,
    outcome: workers.outcome,
    event_type: workers.eventType,
  };
}
async function diagnostics() {
  const { url } = JSON.parse(await readFile("deployment.json", "utf8"));
  const worker = new URL(url).hostname.split(".")[0];
  if (worker !== "mangadock-turso-staging")
    throw new Error("Update diagnostics are restricted to staging");
  const report = { worker, read_only: true };
  try {
    const response = await fetch(
      `https://api.cloudflare.com/client/v4/accounts/${process.env.CLOUDFLARE_ACCOUNT_ID}/workers/observability/telemetry/query`,
      {
        method: "POST",
        redirect: "error",
        signal: AbortSignal.timeout(30000),
        headers: {
          Authorization: `Bearer ${process.env.CLOUDFLARE_API_TOKEN}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          queryId: "mangadock-update-diagnostic",
          dry: true,
          view: "events",
          limit: 50,
          timeframe: { from: Date.now() - 21600000, to: Date.now() },
          parameters: {
            filters: [
              {
                key: "$metadata.service",
                operation: "eq",
                type: "string",
                value: worker,
              },
            ],
            needle: { value: "/api/v1/updates", matchCase: false },
          },
        }),
      },
    );
    const data = await response.json();
    if (!response.ok || data.success === false)
      report.logs = {
        available: false,
        status: response.status,
        error_codes: (data.errors || []).map((e) => e.code),
      };
    else {
      const events = data.result?.events?.events || [];
      report.logs = {
        available: true,
        matching_count: data.result?.events?.count,
        events: events.map(safeEvent),
      };
    }
  } catch (error) {
    report.logs = { available: false, error_type: error.name };
  }
  const client = createClient({
    url: tursoUrl(process.env.TURSO_DATABASE_URL),
    authToken: process.env.TURSO_AUTH_TOKEN,
    intMode: "number",
    fetch: (input, init = {}) =>
      fetch(input, {
        ...init,
        redirect: "error",
        signal: AbortSignal.timeout(30000),
      }),
  });
  try {
    report.recent_update_tasks = (
      await client.execute({
        sql: "SELECT j.id,j.state,j.created_at,j.updated_at,j.attempts,j.lease_until,j.total_chapters,j.completed_chapters,json_extract(j.payload,'$.source_cursor') AS source_cursor,json_extract(j.payload,'$.source_total') AS source_total,c.name AS comic_name FROM comic_jobs j LEFT JOIN comics c ON c.id=j.comic_id WHERE j.kind='source' AND j.created_at>=? AND json_extract(j.payload,'$.is_update')=1 ORDER BY j.created_at DESC LIMIT 10",
        args: [Math.floor(Date.now() / 1000) - 21600],
      })
    ).rows;
    const task = report.recent_update_tasks[0];
    if (task) {
      report.child_jobs = (
        await client.execute({
          sql: "SELECT id,kind,state,attempts,lease_until,created_at,updated_at,remote_poll_at FROM comic_jobs WHERE parent_id=? ORDER BY id LIMIT 25",
          args: [task.id],
        })
      ).rows;
      report.compute_jobs = (
        await client.execute({
          sql: "SELECT id,kind,state,attempts,lease_until,created_at,updated_at FROM compute_jobs WHERE json_extract(payload,'$.parent_job') IN (SELECT id FROM comic_jobs WHERE id=? OR parent_id=?) ORDER BY created_at DESC LIMIT 10",
          args: [task.id, task.id],
        })
      ).rows;
    }
    report.recent_update_checks = (
      await client.execute({
        sql: "SELECT id,state,total,completed,failed,created_at,updated_at FROM update_checks WHERE user_id='admin' AND created_at>=? ORDER BY created_at DESC LIMIT 5",
        args: [Math.floor(Date.now() / 1000) - 21600],
      })
    ).rows;
  } finally {
    client.close();
  }
  for (const [key, path] of [
    ["worker_schedules", `workers/scripts/${worker}/schedules`],
    ["queue_delivery", "queues?per_page=100"],
  ]) {
    const response = await fetch(
      `https://api.cloudflare.com/client/v4/accounts/${process.env.CLOUDFLARE_ACCOUNT_ID}/${path}`,
      {
        redirect: "error",
        signal: AbortSignal.timeout(20000),
        headers: {
          Authorization: `Bearer ${process.env.CLOUDFLARE_API_TOKEN}`,
        },
      },
    );
    const data = await response.json();
    report[key] =
      !response.ok || !data.success
        ? { available: false, status: response.status }
        : key === "queue_delivery"
          ? data.result
              .filter((q) => q.queue_name === worker)
              .map((q) => ({
                name: q.queue_name,
                delivery_paused: q.settings?.delivery_paused,
              }))
          : (Array.isArray(data.result)
              ? data.result
              : data.result.schedules || []
            ).map((s) => ({ cron: s.cron }));
  }
  await writeFile("update-diagnostics.json", JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
}
if (process.argv[1] === fileURLToPath(import.meta.url))
  diagnostics().catch((error) => {
    console.error("::error::Update diagnostics failed: " + error.name);
    process.exitCode = 1;
  });
