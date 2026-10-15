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
  let consoleData = source;
  for (const value of [source?.message, event.$metadata?.message]) {
    if (typeof value === "string")
      try {
        consoleData = { ...consoleData, ...JSON.parse(value) };
      } catch {}
  }
  const component = [
    "application",
    "remote-polls",
    "pilot-api",
    "cloud-compute",
    "control-storage",
  ].includes(consoleData?.component)
    ? consoleData.component
    : undefined;
  const errorType = [
    "Error",
    "TypeError",
    "LibsqlError",
    "DatabasePausedError",
    "ControlStorageUnavailableError",
    "HttpError",
    "HTTPError",
    "ReadTimeout",
    "ConnectTimeout",
    "TimeoutError",
    "ConnectionError",
    "UnidentifiedImageError",
    "ValueError",
    "NameError",
    "KeyError",
    "RuntimeError",
    "UnknownError",
    "SSLError",
    "OSError",
    "URLError",
  ].includes(consoleData?.error_type)
    ? consoleData.error_type
    : undefined;
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
    status:
      response.status ||
      (component === "cloud-compute" &&
      Number.isInteger(consoleData?.status) &&
      consoleData.status >= 400 &&
      consoleData.status <= 599
        ? consoleData.status
        : undefined),
    outcome: workers.outcome,
    event_type: workers.eventType,
    ...(Number.isFinite(workers.cpuTimeMs) && workers.cpuTimeMs >= 0
      ? { cpu_ms: workers.cpuTimeMs }
      : {}),
    ...(typeof workers.durableObjectId === "string"
      ? { durable_object: true }
      : {}),
    ...(component ? { component } : {}),
    ...(component === "control-storage" &&
    [
      "control-read",
      "r2-read",
      "r2-write",
      "legacy-list",
      "legacy-alarm",
    ].includes(consoleData?.operation)
      ? { storage_operation: consoleData.operation }
      : {}),
    ...(component === "control-storage" &&
    Array.isArray(consoleData?.reason_words)
      ? {
          storage_reason_words: consoleData.reason_words
            .filter(
              (word) =>
                typeof word === "string" &&
                /^(?:account|exceeded|daily|maximum|number|rows|row|written|writes|write|read|reads|sqlite|durable|object|objects|storage|limit|limits|usage|reset|disabled|unavailable|timeout|invalid|list|alarm|quota|request|requests|different|context|perform|behalf|permitted|not|supported|operation|failed|free|plan)$/.test(
                  word,
                ),
            )
            .slice(0, 48),
        }
      : {}),
    ...(errorType ? { error_type: errorType } : {}),
    ...(Array.isArray(consoleData?.frames) &&
    consoleData.frames.some((frame) =>
      /\b(?:index|worker)\.js:\d+:\d+\b/.test(String(frame)),
    )
      ? {
          frames: consoleData.frames
            .map(
              (frame) =>
                String(frame).match(/\b(?:index|worker)\.js:\d+:\d+\b/)?.[0],
            )
            .filter(Boolean)
            .slice(0, 4),
        }
      : {}),
    ...(component === "cloud-compute" &&
    [
      "legacy-page",
      "legacy-chapter",
      "legacy-source",
      "cover",
      "archive",
      "pdf",
      "banner",
      "index-epub",
      "fanqie-archive",
    ].includes(consoleData?.job_kind)
      ? { job_kind: consoleData.job_kind }
      : {}),
    ...(component && typeof consoleData?.subrequest_limit === "boolean"
      ? { subrequest_limit: consoleData.subrequest_limit }
      : {}),
    ...(component &&
    /^[A-Z_]{3,64}$/.test(String(consoleData?.error_code || ""))
      ? { error_code: consoleData.error_code }
      : {}),
  };
}
export function safeQuotaHold(value) {
  if (
    value?.version !== 1 ||
    value?.kind !== "legacy_rows_written" ||
    !Number.isSafeInteger(value.until) ||
    value.until <= 0 ||
    (value.reset_at !== undefined &&
      (!Number.isSafeInteger(value.reset_at) || value.reset_at <= 0))
  )
    return { available: false, invalid_schema: true };
  return {
    available: true,
    kind: value.kind,
    until: value.until,
    reset_at: value.reset_at ?? value.until,
  };
}
export async function durableStorageUsage(query, day) {
  const shape =
    "name type { name ofType { name ofType { name ofType { name ofType { name } } } } }";
  const typeName = (type) => {
    for (let depth = 0; depth < 5 && type; depth++, type = type.ofType)
      if (type.name) return type.name;
  };
  const account = await query(
    `{ __type(name: "Account") { fields { ${shape} } } }`,
  );
  const datasets = (account.__type?.fields || [])
    .filter((field) =>
      /^durableObjects\w*(?:Storage|Sql)\w*Groups$/.test(field.name),
    )
    .slice(0, 6);
  const schemas = {};
  for (const dataset of datasets) {
    const name = typeName(dataset.type);
    if (!/^[A-Za-z][A-Za-z0-9_]+$/.test(name || "")) continue;
    const row = await query(
      `{ __type(name: "${name}") { fields { ${shape} } } }`,
    );
    const sumType = typeName(
      row.__type?.fields?.find((field) => field.name === "sum")?.type,
    );
    if (!/^[A-Za-z][A-Za-z0-9_]+$/.test(sumType || "")) continue;
    const sum = await query(
      `{ __type(name: "${sumType}") { fields { name } } }`,
    );
    const fields = (sum.__type?.fields || []).map((field) => field.name);
    schemas[dataset.name] = fields.filter((field) =>
      /row|write|read/i.test(field),
    );
    const written = fields.find((field) =>
      /^(?:sql|sqlite)?rowsWritten$/i.test(field),
    );
    const read = fields.find((field) =>
      /^(?:sql|sqlite)?rowsRead$/i.test(field),
    );
    if (!written) continue;
    const selection = [written, read].filter(Boolean).join(" ");
    const result = await query(
      `query($accountTag: string) { viewer { accounts(filter: {accountTag: $accountTag}) { ${dataset.name}(filter: {date_geq: "${day}", date_leq: "${day}"}, limit: 1000) { sum { ${selection} } } } } }`,
    );
    const samples = result.viewer?.accounts?.[0]?.[dataset.name] || [];
    const total = (field) => {
      if (!field || samples.length === 0) return null;
      const values = samples.map((sample) => sample.sum?.[field]);
      if (!values.every((value) => Number.isSafeInteger(value) && value >= 0))
        return null;
      const count = values.reduce((sum, value) => sum + value, 0);
      return Number.isSafeInteger(count) ? count : null;
    };
    return {
      available: true,
      day,
      dataset: dataset.name,
      samples: samples.length,
      rows_written: total(written),
      rows_read: total(read),
    };
  }
  return { available: false, day, schemas };
}
async function diagnostics() {
  const taskId = process.env.DIAGNOSTIC_TASK_ID || "";
  if (taskId && !/^[a-zA-Z0-9:_-]{1,160}$/.test(taskId))
    throw new Error("Invalid diagnostic task ID");
  const { url } = JSON.parse(await readFile("deployment.json", "utf8"));
  const worker = new URL(url).hostname.split(".")[0];
  if (worker !== "mangadock-turso-staging")
    throw new Error("Update diagnostics are restricted to staging");
  const report = { worker, read_only: true };
  try {
    const hold = await fetch(
      `https://api.cloudflare.com/client/v4/accounts/${process.env.CLOUDFLARE_ACCOUNT_ID}/r2/buckets/${worker}/objects/_system/control-quota-hold-v1.json`,
      {
        headers: {
          Authorization: `Bearer ${process.env.CLOUDFLARE_API_TOKEN}`,
        },
        redirect: "error",
        signal: AbortSignal.timeout(15000),
      },
    );
    report.control_quota_hold = hold.ok
      ? safeQuotaHold(await hold.json())
      : { available: false, status: hold.status };
    report.observed_at = new Date().toISOString();
  } catch {
    report.control_quota_hold = { available: false };
  }
  try {
    report.durable_storage_usage = await durableStorageUsage(
      async (query) => {
        const response = await fetch(
          "https://api.cloudflare.com/client/v4/graphql",
          {
            method: "POST",
            headers: {
              Authorization: `Bearer ${process.env.CLOUDFLARE_API_TOKEN}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              query,
              variables: { accountTag: process.env.CLOUDFLARE_ACCOUNT_ID },
            }),
            redirect: "error",
            signal: AbortSignal.timeout(15000),
          },
        );
        const payload = await response.json();
        if (!response.ok || payload.errors?.length)
          throw new Error("Analytics unavailable");
        return payload.data;
      },
      new Date().toISOString().slice(0, 10),
    );
  } catch {
    report.durable_storage_usage = { available: false };
  }
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
  for (const [needle, key] of [
    ["control-storage", "control_storage_logs"],
    ["application", "application_logs"],
    ["cron", "cron_logs"],
    ["error_type", "error_logs"],
    ["LibsqlError", "provider_error_logs"],
    ['"subrequest_limit":true', "subrequest_error_logs"],
    ["cloud-compute", "compute_error_logs"],
    ["cpuTimeMs", "cpu_events"],
  ]) {
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
          queryId: "mangadock-background-diagnostic",
          dry: true,
          view: "events",
          limit: 30,
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
            needle: { value: needle },
          },
        }),
      },
    );
    const data = await response.json();
    report[key] =
      response.ok && data.success !== false
        ? (data.result?.events?.events || []).map(safeEvent)
        : { available: false, status: response.status };
  }
  if (process.env.MANGADOCK_ADMIN_PASSWORD) {
    const response = await fetch(url + "/api/v1/auth/login", {
      method: "POST",
      headers: { Origin: url, "Content-Type": "application/json" },
      body: JSON.stringify({
        username: "admin",
        password: process.env.MANGADOCK_ADMIN_PASSWORD,
      }),
      signal: AbortSignal.timeout(30000),
    });
    if (response.ok) {
      const cookie = response.headers.get("set-cookie")?.split(";")[0] || "";
      const status = await fetch(url + "/api/admin/resource-status", {
        headers: { Origin: url, Cookie: cookie },
        signal: AbortSignal.timeout(30000),
      });
      report.resource_status = status.ok
        ? await status.json()
        : { available: false, status: status.status };
    } else
      report.resource_status = { available: false, status: response.status };
  }
  const contentResponse = await fetch(
    `https://api.cloudflare.com/client/v4/accounts/${process.env.CLOUDFLARE_ACCOUNT_ID}/workers/scripts/${worker}`,
    {
      headers: { Authorization: `Bearer ${process.env.CLOUDFLARE_API_TOKEN}` },
      redirect: "error",
      signal: AbortSignal.timeout(30000),
    },
  );
  if (contentResponse.ok) {
    const code = await contentResponse.text();
    let redactedCode = code;
    for (const key of [
      "TURSO_AUTH_TOKEN",
      "CLOUDFLARE_API_TOKEN",
      "CLOUDFLARE_ACCOUNT_ID",
      "MANGADOCK_ADMIN_PASSWORD",
      "COMPUTE_WAKE_TOKEN",
    ])
      if (process.env[key])
        redactedCode = redactedCode.replaceAll(process.env[key], "[redacted]");
    await writeFile("deployed-worker-content.txt", redactedCode);
    report.deployed_code = {
      control_reason_diagnostics: code.includes("reason_words"),
      native_batch: code.includes("missing.slice(0, 200)"),
      native_recovery: code.includes("recoverNativeChapters"),
      completeness_check: code.includes(
        "aggregate.total !== aggregate.page_count",
      ),
      web_budget_override: code.includes("DB_WEB_DAILY_REQUEST_LIMIT"),
      component_snapshots:
        /\[\s*"jobs",\s*"web",\s*"compute",\s*"login",\s*"updates"\s*\]/.test(
          code,
        ),
    };
  } else
    report.deployed_code = { available: false, status: contentResponse.status };
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
    report.recent_download_tasks = (
      await client.execute({
        sql: "SELECT j.id,j.state,j.created_at,j.updated_at,c.name AS comic_name,(json_extract(j.payload,'$.url')='https://baozimh.org/manga/dafengdagengren-chuyingshe') AS ci_test_source FROM comic_jobs j INDEXED BY comic_jobs_kind_created LEFT JOIN comics c ON c.id=j.comic_id WHERE j.kind='source' AND j.created_at>=? ORDER BY j.created_at DESC LIMIT 20",
        args: [Math.floor(Date.now() / 1000) - 21600],
      })
    ).rows;
    report.recent_update_tasks = (
      await client.execute({
        sql:
          "SELECT j.id,j.state,j.created_at,j.updated_at,j.attempts,j.lease_until,j.total_chapters,j.completed_chapters,json_extract(j.payload,'$.source_cursor') AS source_cursor,json_extract(j.payload,'$.source_total') AS source_total,c.name AS comic_name FROM comic_jobs j LEFT JOIN comics c ON c.id=j.comic_id WHERE j.kind='source' AND " +
          (taskId
            ? "j.id=?"
            : "j.created_at>=? AND json_extract(j.payload,'$.is_update')=1 ORDER BY j.created_at DESC LIMIT 10"),
        args: [taskId || Math.floor(Date.now() / 1000) - 21600],
      })
    ).rows;
    const task = report.recent_update_tasks[0];
    if (task) {
      report.child_jobs = (
        await client.execute({
          sql: "SELECT j.id,j.kind,j.state,j.attempts,j.lease_until,j.created_at,j.updated_at,j.remote_poll_at,c.page_count AS expected_pages,c.state AS chapter_state,json_extract(j.payload,'$.provider') AS provider,json_extract(j.payload,'$.chapter.id') AS source_chapter_id,json_extract(j.payload,'$.chapter.mid') AS source_mid,(SELECT COUNT(*) FROM comic_jobs p WHERE p.parent_id=j.id AND p.kind='page') AS persisted_pages,(SELECT COUNT(*) FROM comic_jobs p WHERE p.parent_id=j.id AND p.kind='page' AND p.state='completed') AS completed_pages FROM comic_jobs j LEFT JOIN chapters c ON c.id=json_extract(j.payload,'$.chapter_id') WHERE j.parent_id=? ORDER BY j.id LIMIT 25",
          args: [task.id],
        })
      ).rows;
      report.compute_jobs = (
        await client.execute({
          sql: "SELECT id,kind,state,attempts,lease_until,created_at,updated_at,CASE WHEN json_valid(message) THEN json_extract(message,'$.error_type') ELSE NULL END AS error_type,CASE WHEN json_valid(message) THEN json_extract(message,'$.status') ELSE NULL END AS http_status FROM compute_jobs WHERE json_extract(payload,'$.parent_job') IN (SELECT id FROM comic_jobs WHERE id=? OR parent_id=? OR parent_id IN (SELECT id FROM comic_jobs WHERE parent_id=?)) ORDER BY created_at DESC LIMIT 10",
          args: [task.id, task.id, task.id],
        })
      ).rows;
      report.page_states = (
        await client.execute({
          sql: "SELECT state,attempts,COUNT(*) AS n,MIN(lease_until) AS min_lease,MAX(lease_until) AS max_lease FROM comic_jobs WHERE parent_id IN (SELECT id FROM comic_jobs WHERE parent_id=?) AND kind='page' GROUP BY state,attempts",
          args: [task.id],
        })
      ).rows;
      const failures = (
        await client.execute({
          sql: "SELECT j.id,j.message,j.attempts,j.updated_at,json_extract(j.payload,'$.key') AS object_key,l.state AS reservation_state,l.size AS reserved_size,l.created_at AS reserved_at,(l.owner IS NOT NULL) AS reservation_owned FROM comic_jobs j LEFT JOIN storage_ledger l ON l.key=json_extract(j.payload,'$.key') WHERE j.parent_id IN (SELECT id FROM comic_jobs WHERE parent_id=?) AND j.kind='page' AND j.state='error' ORDER BY j.updated_at,j.id LIMIT 12",
          args: [task.id],
        })
      ).rows;
      const safeMessages = new Set([
        "存储预留冲突",
        "文件正在上传，请稍后重试",
        "主任务已停止",
        "作业租约已失效",
        "图片超过 20 MB",
        "来源未返回图片",
        "文件传输失败或长度不一致",
        "来源或存储暂时不可用",
        "书库已达到存储预算上限，未写入文件",
        "文件长度无效",
      ]);
      report.first_page_failures = failures.map(({ message, ...row }) => ({
        ...row,
        reason:
          safeMessages.has(message) || /^图片 HTTP \d{3}$/.test(message)
            ? message
            : "unclassified",
      }));
    }
    report.storage_totals = (
      await client.execute(
        "SELECT book_bytes+ledger_bytes-overlap_bytes+reservation_bytes AS library_bytes FROM storage_totals WHERE id=1",
      )
    ).rows;
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
    ["worker_limits", `workers/scripts/${worker}/settings`],
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
        : key === "worker_limits"
          ? Object.fromEntries(
              (data.result.bindings || [])
                .filter(
                  (b) =>
                    b.type === "plain_text" &&
                    [
                      "MAX_LIBRARY_BYTES",
                      "COMPUTE_REF",
                      "DB_JOB_DAILY_REQUEST_LIMIT",
                      "DB_JOB_DAILY_STATEMENT_LIMIT",
                      "DB_WEB_DAILY_REQUEST_LIMIT",
                      "DB_WEB_DAILY_STATEMENT_LIMIT",
                      "DB_DAILY_REQUEST_LIMIT",
                      "DB_DAILY_STATEMENT_LIMIT",
                    ].includes(b.name),
                )
                .map((b) => [b.name, b.text]),
            )
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
