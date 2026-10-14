import { writeFile, readFile } from "node:fs/promises";
import { pbkdf2Sync, randomBytes, randomUUID } from "node:crypto";
import { spawnSync } from "node:child_process";
import { checkD1Budget } from "./d1-budget.mjs";
process.on("uncaughtException", (error) => {
  console.error(
    "::error::" +
      String(error.message)
        .replaceAll("%", "%25")
        .replaceAll("\n", "%0A")
        .replaceAll("\r", "%0D"),
  );
  process.exit(1);
});
const target = process.argv[2] || "staging";
if (!["staging", "production"].includes(target))
  throw new Error("只允许 staging 或 production");
const account = process.env.CLOUDFLARE_ACCOUNT_ID,
  token = process.env.CLOUDFLARE_API_TOKEN,
  password = process.env.MANGADOCK_ADMIN_PASSWORD;
const provider = process.env.DATABASE_PROVIDER || "turso";
if (
  target === "production" &&
  provider === "turso" &&
  /staging/i.test(process.env.TURSO_DATABASE_URL || "")
)
  throw new Error(
    "正式环境必须使用独立的 Turso 数据库，不能共用 staging 数据库",
  );
if (!["d1", "turso"].includes(provider))
  throw new Error("DATABASE_PROVIDER must be d1 or turso");
const tursoToken = process.env.TURSO_AUTH_TOKEN || process.env.TURSO_TOKEN;
if (provider === "turso" && (!process.env.TURSO_DATABASE_URL || !tursoToken))
  throw new Error("TURSO_DATABASE_URL / TURSO_TOKEN Secrets are missing");
if (!/^[a-f0-9]{32}$/i.test(account || "") || !token)
  throw new Error("请配置 CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN");
if (!password || password.length < 6 || password.length > 512)
  throw new Error("请配置至少 6 字符的 MANGADOCK_ADMIN_PASSWORD Secret");
// Fail before database migration or resource changes when CI cannot run the executor.
const providedWake = process.env.COMPUTE_WAKE_TOKEN || "";
if (
  providedWake.length < 32 ||
  providedWake.length > 4096 ||
  /[\r\n]/.test(providedWake)
)
  throw new Error(
    "请配置独立的 COMPUTE_WAKE_TOKEN CI Secret（至少 32 字符），部署和云执行器使用同一值",
  );
const queueLimit = Number(process.env.MANGADOCK_QUEUE_DAILY_MESSAGES || 1000),
  queuePause = Number(process.env.MANGADOCK_QUEUE_PAUSE_UNTIL || 0);
if (
  !Number.isInteger(queueLimit) ||
  queueLimit < 1 ||
  queueLimit > 1000 ||
  !Number.isInteger(queuePause) ||
  queuePause < 0
)
  throw new Error("Invalid free queue budget or pause time");
const downloadRequests = Number(
  process.env.MANGADOCK_DB_DOWNLOAD_REQUESTS || 200000,
);
if (
  !Number.isSafeInteger(downloadRequests) ||
  downloadRequests < 1 ||
  downloadRequests > 200000
)
  throw new Error(
    "MANGADOCK_DB_DOWNLOAD_REQUESTS must be an integer from 1 to 200000",
  );
const downloadStatements = Number(
  process.env.MANGADOCK_DB_DOWNLOAD_STATEMENTS || 200000,
);
if (
  !Number.isSafeInteger(downloadStatements) ||
  downloadStatements < 1 ||
  downloadStatements > 200000
)
  throw new Error(
    "MANGADOCK_DB_DOWNLOAD_STATEMENTS must be an integer from 1 to 200000",
  );
const webRequests = Number(
  process.env.MANGADOCK_DB_WEB_REQUESTS ||
    (target === "staging"
      ? 200000
      : process.env.MANGADOCK_DB_DAILY_REQUESTS || 10000),
);
const webStatements = Number(
  process.env.MANGADOCK_DB_WEB_STATEMENTS ||
    (target === "staging"
      ? 200000
      : process.env.MANGADOCK_DB_DAILY_STATEMENTS || 50000),
);
for (const [name, value] of [
  ["MANGADOCK_DB_WEB_REQUESTS", webRequests],
  ["MANGADOCK_DB_WEB_STATEMENTS", webStatements],
])
  if (!Number.isSafeInteger(value) || value < 1 || value > 200000)
    throw new Error(name + " must be an integer from 1 to 200000");
const libraryLimit = Number(
  process.env.MANGADOCK_MAX_LIBRARY_BYTES ||
    (target === "production" ? 8000000000 : 9000000000),
);
if (
  !Number.isSafeInteger(libraryLimit) ||
  libraryLimit < 1 ||
  libraryLimit > 1000000000000
)
  throw new Error("MANGADOCK_MAX_LIBRARY_BYTES 必须是有效的正整数，最多 1 TB");
const prefix = `mangadock-${provider === "turso" ? "turso" : "full"}-${target}`;
async function cf(path, method = "GET", data, allowMissing = false) {
  const response = await fetch(
    `https://api.cloudflare.com/client/v4/accounts/${account}${path}`,
    {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: data ? JSON.stringify(data) : undefined,
      signal: AbortSignal.timeout(30000),
    },
  );
  const payload = await response.json();
  if (allowMissing && response.status === 404) return null;
  if (!response.ok || !payload.success)
    throw new Error(
      `Cloudflare ${method} ${path.split("?")[0]} HTTP ${response.status}; error codes ${(payload.errors || []).map((e) => e.code).join(",")}`,
    );
  return payload.result;
}
async function pages(path) {
  const results = [];
  for (let page = 1; page <= 100; page++) {
    const part = await cf(`${path}?page=${page}&per_page=100`);
    results.push(...part);
    if (part.length < 100) return results;
  }
  throw new Error("资源列表超过分页上限");
}
// These calls provision free-compatible resources only. They never subscribe to Workers Paid or change billing plans.
let db;
if (provider === "d1") {
  await checkD1Budget();
  const databases = await pages("/d1/database");
  db =
    databases.find((d) => d.name === prefix) ||
    (await cf("/d1/database", "POST", { name: prefix }));
} else {
  const init = spawnSync(
    process.execPath,
    ["--experimental-strip-types", "scripts/turso-migrate.mjs"],
    { stdio: "inherit", env: { ...process.env, TURSO_AUTH_TOKEN: tursoToken } },
  );
  if (init.status !== 0)
    throw new Error(
      "Turso initialization failed; no Cloudflare resources were changed",
    );
}
async function listR2Buckets() {
  const buckets = [];
  let cursor = "";
  for (let page = 0; page < 100; page++) {
    const params = new URLSearchParams({ per_page: "1000" });
    if (cursor) params.set("cursor", cursor);
    const response = await fetch(
      `https://api.cloudflare.com/client/v4/accounts/${account}/r2/buckets?${params}`,
      {
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        signal: AbortSignal.timeout(30000),
      },
    );
    const payload = await response.json();
    if (!response.ok || !payload.success)
      throw new Error(
        `Cloudflare GET /r2/buckets HTTP ${response.status}; error codes ${(payload.errors || []).map((e) => e.code).join(",")}`,
      );
    const part = Array.isArray(payload.result)
      ? payload.result
      : payload.result?.buckets || [];
    let added = 0;
    for (const bucket of part) {
      if (buckets.some((known) => known.name === bucket.name)) continue;
      buckets.push(bucket);
      added++;
    }
    const next = payload.result_info?.cursor || payload.result?.cursor || "";
    if (
      !next ||
      next === cursor ||
      payload.result_info?.is_truncated === false ||
      added === 0
    )
      return buckets;
    cursor = next;
  }
  throw new Error("R2 bucket 列表超过分页上限");
}
const buckets = await listR2Buckets();
if (!buckets.some((b) => b.name === prefix))
  await cf("/r2/buckets", "POST", { name: prefix });
const queues = await pages("/queues");
if (!queues.some((q) => q.queue_name === prefix))
  await cf("/queues", "POST", { queue_name: prefix });
// Generated resource IDs are deployment artifacts, never secrets or checked-in account-specific config.
const config = {
  name: prefix,
  main: "src/index.ts",
  compatibility_date: "2025-10-01",
  assets: {
    directory: "./public",
    binding: "ASSETS",
    html_handling: "none",
    run_worker_first: [
      "/",
      "/api/*",
      "/login",
      "/logout",
      "/login-cover/*",
      "/reader",
      "/reader/*",
      "/novels",
      "/novels/*",
      "/novel/*",
      "/comics",
      "/comics/*",
      "/comic/*",
      "/comic_groups",
      "/comic_groups/*",
      "/admin_hidden/*",
      "/settings",
      "/settings/*",
      "/change_password",
      "/search",
      "/history",
      "/statistics",
      "/users",
      "/users/*",
      "/download",
      "/update",
      "/update/*",
      "/tasks",
      "/progress/*",
      "/task_status/*",
      "/cancel_task/*",
      "/delete_task/*",
      "/delete_tasks",
      "/save_progress",
      "/get_progress/*",
      "/anilist/*",
      "/static/comic/*",
      "/static/cover/*",
    ],
  },
  vars: {
    CONTROL_STORAGE_PROVIDER: "r2",
    DATABASE_PROVIDER: provider,
    COMPUTE_REF:
      process.env.GITHUB_SHA ||
      spawnSync("git", ["rev-parse", "HEAD"], {
        encoding: "utf8",
      }).stdout.trim(),
    FANQIE_API_URL: "https://fanqie.dddinmx.cn",
    QUEUE_DAILY_MESSAGE_LIMIT: String(queueLimit),
    QUEUE_SEND_PAUSED_UNTIL: String(queuePause),
    DB_JOB_DAILY_REQUEST_LIMIT: String(downloadRequests),
    DB_JOB_DAILY_STATEMENT_LIMIT: String(downloadStatements),
    DB_WEB_DAILY_REQUEST_LIMIT: String(webRequests),
    DB_WEB_DAILY_STATEMENT_LIMIT: String(webStatements),
    DB_DAILY_REQUEST_LIMIT: process.env.MANGADOCK_DB_DAILY_REQUESTS || "10000",
    DB_DAILY_STATEMENT_LIMIT:
      process.env.MANGADOCK_DB_DAILY_STATEMENTS || "50000",
    MAX_ARTIFACT_BYTES: "33554432",
    MAX_LIBRARY_BYTES: String(libraryLimit),
  },
  d1_databases:
    provider === "d1"
      ? [
          {
            binding: "DB",
            database_name: prefix,
            database_id: db.uuid,
            migrations_dir: "migrations",
          },
        ]
      : [],
  r2_buckets: [{ binding: "LIBRARY", bucket_name: prefix }],
  queues: {
    producers: [{ binding: "DOWNLOADS", queue: prefix }],
    consumers: [
      {
        queue: prefix,
        max_batch_size: 1,
        max_retries: 1,
        max_concurrency: target === "staging" ? 5 : 1,
      },
    ],
  },
  triggers: { crons: ["*/15 * * * *"] },
  observability: { enabled: true },
  durable_objects: {
    bindings: [
      { name: "AUTH_SERVICE", class_name: "PasswordVerifier" },
      { name: "APPLICATION", class_name: "ApplicationService" },
    ],
  },
  migrations: [
    { tag: "auth-v1", new_sqlite_classes: ["PasswordVerifier"] },
    { tag: "app-v1", new_sqlite_classes: ["ApplicationService"] },
  ],
};
const filename = `wrangler.${target}.generated.json`;
await writeFile(filename, JSON.stringify(config, null, 2));
if (provider === "d1") {
  const migrate = spawnSync(
    "npx",
    [
      "wrangler",
      "d1",
      "migrations",
      "apply",
      prefix,
      "--remote",
      "--config",
      filename,
    ],
    { stdio: "inherit", env: { ...process.env, CI: "true" } },
  );
  if (migrate.status !== 0) throw new Error("D1 migrations 失败");
  const salt = randomBytes(16).toString("hex"),
    encoded = `pbkdf2:sha256:600000$${salt}$${pbkdf2Sync(password, salt, 600000, 32, "sha256").toString("hex")}`;
  await cf(`/d1/database/${db.uuid}/query`, "POST", {
    sql: "INSERT INTO users(id,username,password_hash,role) VALUES ('admin','admin',?,'admin') ON CONFLICT(username) DO NOTHING",
    params: [encoded],
  });
}
// Preserve established Worker Secrets across releases. Only first enrolment generates new source credentials.
// A 404 from the secrets list means the Worker does not exist yet.
const listedSecrets = await cf(
  `/workers/scripts/${config.name}/secrets`,
  "GET",
  undefined,
  true,
);
const workerExisted = listedSecrets !== null;
const existingSecrets = listedSecrets || [];
const hasSource =
  existingSecrets.some((secret) => secret.name === "FANQIE_API_TOKEN") &&
  existingSecrets.some((secret) => secret.name === "FANQIE_INSTANCE_ID");
const integrationKey = existingSecrets.some(
  (secret) => secret.name === "SETTINGS_ENCRYPTION_KEY",
)
  ? null
  : randomBytes(32).toString("base64url");
// The deployment and Actions executor must have the same independently supplied secret.
const computeWakeToken = providedWake;
let sourceToken = process.env.FANQIE_API_TOKEN,
  instance = process.env.FANQIE_INSTANCE_ID;
if (sourceToken && !instance)
  throw new Error(
    "FANQIE_API_TOKEN and FANQIE_INSTANCE_ID must be configured together",
  );
const fanqieReady = Boolean(sourceToken);
function showWrangler(result) {
  if (result.stdout) process.stdout.write(result.stdout);
  if (result.stderr) process.stderr.write(result.stderr);
}
function wrangler(args, input) {
  return spawnSync("npx", ["wrangler", ...args, "--config", filename], {
    input,
    encoding: "utf8",
    env: { ...process.env, CI: "true" },
  });
}
// Upload secrets before deploy so the published version inherits them.
// secret put creates the Worker on first deploy; secret bulk updates an existing one.
// If the latest version is not deployed, fall back to versions secret bulk.
function uploadSecrets(values, createIfMissing) {
  const entries = Object.entries(values).filter(
    ([, value]) => value != null && value !== "",
  );
  if (!entries.length) return;
  if (createIfMissing) {
    for (const [name, value] of entries) {
      const result = wrangler(["secret", "put", name], String(value));
      showWrangler(result);
      if (result.status !== 0) throw new Error("Worker Secret 配置失败");
    }
    return;
  }
  const payload = JSON.stringify(Object.fromEntries(entries));
  let result = wrangler(["secret", "bulk"], payload);
  showWrangler(result);
  if (result.status !== 0) {
    const text = `${result.stdout || ""}\n${result.stderr || ""}`;
    if (
      !/versions secret bulk|isn't currently deployed|is not currently deployed/i.test(
        text,
      )
    )
      throw new Error("Worker Secret 配置失败");
    result = wrangler(["versions", "secret", "bulk"], payload);
    showWrangler(result);
    if (result.status !== 0) throw new Error("Worker Secret 配置失败");
  }
}
uploadSecrets(
  {
    ...(computeWakeToken ? { COMPUTE_WAKE_TOKEN: computeWakeToken } : {}),
    ...(provider === "turso"
      ? {
          TURSO_DATABASE_URL: process.env.TURSO_DATABASE_URL,
          TURSO_AUTH_TOKEN: tursoToken,
        }
      : {}),
    ...(integrationKey ? { SETTINGS_ENCRYPTION_KEY: integrationKey } : {}),
    ...(fanqieReady
      ? { FANQIE_API_TOKEN: sourceToken, FANQIE_INSTANCE_ID: instance }
      : {}),
  },
  !workerExisted,
);
const deploy = spawnSync("npx", ["wrangler", "deploy", "--config", filename], {
  stdio: "inherit",
  env: { ...process.env, CI: "true" },
});
if (deploy.status !== 0) throw new Error("Worker 部署失败");
const subdomain = await cf("/workers/subdomain");
const url = `https://${config.name}.${subdomain.subdomain}.workers.dev`;
await writeFile(
  "deployment.json",
  JSON.stringify(
    {
      url,
      environment: target,
      database_id: db?.uuid || null,
      database_provider: provider,
      bucket: prefix,
      queue: prefix,
    },
    null,
    2,
  ),
);
for (let attempt = 0; attempt < 12; attempt++) {
  const health = await fetch(url + "/api/health", {
    signal: AbortSignal.timeout(10000),
  });
  if (health.ok && (await health.json()).version === "workers-0.1") break;
  await health.body?.cancel();
  if (attempt === 11) throw new Error("Worker 路由尚未就绪，请稍后重试");
  await new Promise((resolve) => setTimeout(resolve, 5000));
}
if (process.env.ENABLE_FANQIE_SOURCE === "true" && !sourceToken && !hasSource) {
  try {
    instance = randomUUID().replaceAll("-", "");
    const registration = "fqr_" + randomBytes(32).toString("base64url");
    const headers = {
      "X-MangaDock-Instance-ID": instance,
      "X-MangaDock-Registration-Key": registration,
      "X-MangaDock-Version": "5.0",
      "X-MangaDock-System": "Cloudflare/Workers",
      Accept: "application/json",
      "User-Agent": "MangaDock/5.0 Fanqie-Client/1.1",
    };
    const response = await fetch("https://fanqie.dddinmx.cn/v1/register", {
      method: "POST",
      headers,
      signal: AbortSignal.timeout(25000),
    });
    if (response.ok) {
      const payload = await response.json();
      if (!payload.ok) throw new Error("来源注册响应无效");
      sourceToken = payload.data?.token;
    } else {
      // The GitHub runner and Cloudflare edge may take different origin routes. Keep the source server unchanged.
      await response.body?.cancel();
      console.log(
        `GitHub 来源注册 HTTP ${response.status}; 尝试从已部署 Worker 初始化。`,
      );
      const login = await fetch(url + "/api/v1/auth/login", {
        method: "POST",
        headers: { Origin: url, "Content-Type": "application/json" },
        body: JSON.stringify({ username: "admin", password }),
        signal: AbortSignal.timeout(30000),
      });
      if (!login.ok) throw new Error(`边缘初始化登录 HTTP ${login.status}`);
      const cookie = login.headers.get("set-cookie")?.split(";")[0],
        csrf = (await login.json()).data?.csrf_token;
      const edge = await fetch(url + "/api/source/register", {
        method: "POST",
        headers: {
          Origin: url,
          Cookie: cookie,
          "X-CSRFToken": csrf,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ instance, registration }),
        signal: AbortSignal.timeout(30000),
      });
      if (!edge.ok)
        throw new Error(`来源 Worker 边缘初始化 HTTP ${edge.status}`);
      sourceToken = (await edge.json()).data?.token;
    }
    if (!/^fqk_[A-Za-z0-9_-]{32,128}$/.test(sourceToken || ""))
      throw new Error("来源注册 Token 格式无效");
  } catch (error) {
    if (target !== "staging") throw error;
    sourceToken = undefined;
    console.log(
      "::warning::番茄来源注册不可用；staging 界面、漫画、R2 和云转换继续验证，小说来源验收仍会报告失败。",
    );
  }
}
// A token created by registration does not exist until after the Worker is up.
// Credentials already in the environment were uploaded before deploy.
if (sourceToken && !fanqieReady) {
  uploadSecrets(
    { FANQIE_API_TOKEN: sourceToken, FANQIE_INSTANCE_ID: instance },
    false,
  );
}
if (process.env.GITHUB_STEP_SUMMARY)
  await import("node:fs/promises").then((fs) =>
    fs.appendFile(
      process.env.GITHUB_STEP_SUMMARY,
      `部署地址：${url}\n\n管理员用户名：admin；密码来自 MANGADOCK_ADMIN_PASSWORD Secret。\n`,
    ),
  );
console.log(`部署地址：${url}`);
