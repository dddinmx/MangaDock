import { randomBytes } from "node:crypto";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const worker = "mangadock-turso-staging";
const url = "https://mangadock-turso-staging.dddinmx.workers.dev";
async function uploadSecrets(values) {
  const folder = await mkdtemp(join(tmpdir(), "md-source-configuration-"));
  try {
    const config = join(folder, "wrangler.json");
    await writeFile(
      config,
      JSON.stringify({ name: worker, compatibility_date: "2026-10-12" }),
    );
    for (const value of Object.values(values))
      console.log("::add-mask::" + value);
    const result = spawnSync(
      "npx",
      ["wrangler", "secret", "bulk", "--name", worker, "--config", config],
      {
        input: JSON.stringify(values),
        encoding: "utf8",
        env: { ...process.env, CI: "true" },
        timeout: 120000,
      },
    );
    if (result.error || result.status !== 0)
      throw new Error(
        "Existing staging source Secrets could not be updated; no registration retry was attempted",
      );
  } finally {
    await rm(folder, { recursive: true, force: true });
  }
}

export async function initializeNovelSource(
  environ,
  request = fetch,
  writeSecrets = uploadSecrets,
) {
  const account = environ.CLOUDFLARE_ACCOUNT_ID;
  if (
    !/^[a-f0-9]{32}$/i.test(account || "") ||
    !environ.CLOUDFLARE_API_TOKEN ||
    !environ.MANGADOCK_ADMIN_PASSWORD
  )
    throw new Error(
      "Staging source initialization requires configured CI credentials",
    );
  const settingsUrl = `https://api.cloudflare.com/client/v4/accounts/${account}/workers/scripts/${worker}/settings`;
  async function settings() {
    const response = await request(settingsUrl, {
      redirect: "error",
      signal: AbortSignal.timeout(30000),
      headers: { Authorization: "Bearer " + environ.CLOUDFLARE_API_TOKEN },
    });
    if (!response.ok)
      throw new Error(
        `Cannot inspect existing staging Worker settings: HTTP ${response.status}`,
      );
    const data = await response.json();
    if (data.success !== true || !Array.isArray(data.result?.bindings))
      throw new Error("Existing staging Worker settings are unavailable");
    return data.result.bindings;
  }
  const before = await settings();
  const sourceBindings = before.filter((binding) =>
    ["FANQIE_API_TOKEN", "FANQIE_INSTANCE_ID"].includes(binding.name),
  );
  if (sourceBindings.length === 2)
    return {
      configured: true,
      new_registration: false,
      existing_credentials_preserved: true,
      application_code_changed: false,
    };
  if (sourceBindings.length)
    throw new Error(
      "Source credentials are partially configured; refusing to replace an existing secret",
    );
  const ref = before.find((binding) => binding.name === "COMPUTE_REF")?.text;
  if (!/^[a-f0-9]{40}$/.test(ref || ""))
    throw new Error("Cannot preserve the exact deployed compute revision");
  const login = await request(url + "/api/v1/auth/login", {
    method: "POST",
    redirect: "error",
    signal: AbortSignal.timeout(30000),
    headers: { Origin: url, "Content-Type": "application/json" },
    body: JSON.stringify({
      username: "admin",
      password: environ.MANGADOCK_ADMIN_PASSWORD,
    }),
  });
  if (!login.ok)
    throw new Error(`Source initialization login HTTP ${login.status}`);
  const cookie = login.headers.get("set-cookie")?.split(";")[0],
    csrf = (await login.json()).data?.csrf_token;
  if (!cookie || !csrf)
    throw new Error("Source initialization session is incomplete");
  const instance = randomBytes(16).toString("hex"),
    registration = "fqr_" + randomBytes(32).toString("base64url");
  const registered = await request(url + "/api/source/register", {
    method: "POST",
    redirect: "error",
    signal: AbortSignal.timeout(35000),
    headers: {
      Origin: url,
      Cookie: cookie,
      "X-CSRFToken": csrf,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ instance, registration }),
  });
  if (!registered.ok)
    throw new Error(
      `Existing source registration HTTP ${registered.status}; no automatic retry`,
    );
  const payload = await registered.json(),
    token = payload.data?.token;
  if (payload.ok !== true || !/^fqk_[A-Za-z0-9_-]{32,128}$/.test(token || ""))
    throw new Error("Source registration returned an invalid credential");
  await writeSecrets({ FANQIE_API_TOKEN: token, FANQIE_INSTANCE_ID: instance });
  const after = await settings();
  for (const old of before) {
    const current = after.find((binding) => binding.name === old.name);
    if (
      !current ||
      old.type !== current.type ||
      [
        "text",
        "namespace_id",
        "bucket_name",
        "database_id",
        "queue_name",
        "service",
        "environment",
      ].some((key) => old[key] !== current[key])
    )
      throw new Error(
        "Staging settings changed unexpectedly during source initialization",
      );
  }
  if (
    !["FANQIE_API_TOKEN", "FANQIE_INSTANCE_ID"].every((name) =>
      after.some(
        (binding) => binding.name === name && binding.type === "secret_text",
      ),
    )
  )
    throw new Error(
      "New source credentials were not confirmed in Worker Secrets",
    );
  return {
    configured: true,
    new_registration: true,
    stored_in_cloudflare_secrets: true,
    compute_ref_unchanged: true,
    application_code_changed: false,
  };
}
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try {
    const report = await initializeNovelSource(process.env);
    await writeFile(
      "novel-source-initialization.json",
      JSON.stringify(report, null, 2),
    );
    console.log(JSON.stringify(report));
  } catch (error) {
    console.error("::error::" + error.message);
    process.exitCode = 1;
  }
}
