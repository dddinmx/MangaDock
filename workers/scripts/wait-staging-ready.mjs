import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

export async function waitForStaging(url, password, options = {}) {
  if (url !== "https://mangadock-turso-staging.dddinmx.workers.dev")
    throw new Error("Readiness is restricted to the exact Turso staging site");
  if (!password) throw new Error("Staging administrator secret is missing");
  const request = options.fetch || fetch;
  const clock = options.clock || Date.now;
  const sleep =
    options.sleep ||
    ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  const until = clock() + 1800000;
  for (let attempt = 1; attempt <= 30 && clock() < until; attempt++) {
    const login = await request(url + "/api/v1/auth/login", {
      method: "POST",
      redirect: "error",
      signal: AbortSignal.timeout(30000),
      headers: { Origin: url, "Content-Type": "application/json" },
      body: JSON.stringify({ username: "admin", password }),
    });
    let result = login;
    if (login.ok) {
      const cookie = login.headers.get("set-cookie")?.split(";")[0];
      const data = await login.json();
      const csrf = data.data?.csrf_token;
      if (!cookie || !csrf)
        throw new Error(
          "Readiness login did not return a session and CSRF token",
        );
      result = await request(url + "/api/admin/maintenance", {
        method: "POST",
        redirect: "error",
        signal: AbortSignal.timeout(60000),
        headers: { Origin: url, Cookie: cookie, "X-CSRFToken": csrf },
      });
      if (result.ok) {
        await result.body?.cancel();
        return {
          ready: true,
          maintenance: true,
          attempts: attempt,
          observed_at: new Date(clock()).toISOString(),
          new_download: false,
        };
      }
    }
    const retryAfter = Number(result.headers.get("Retry-After")) || 60;
    await result.body?.cancel();
    if (![429, 503].includes(result.status))
      throw new Error(
        `Staging readiness HTTP ${result.status}; no automatic credential retries`,
      );
    const delay = Math.max(60, retryAfter) * 1000;
    if (clock() + delay >= until || attempt === 30)
      throw new Error(
        "Staging readiness is still paused beyond its thirty-minute window",
      );
    await sleep(delay);
  }
  throw new Error("Staging readiness deadline exceeded");
}
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try {
    const { url } = JSON.parse(await readFile("deployment.json", "utf8"));
    const report = await waitForStaging(
      url,
      process.env.MANGADOCK_ADMIN_PASSWORD,
    );
    await writeFile("readiness-results.json", JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } catch (error) {
    console.error("::error::" + error.message);
    process.exitCode = 1;
  }
}
