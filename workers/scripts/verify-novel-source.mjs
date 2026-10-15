import { readFile, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

export async function verifyNovelSource(url, password, request = fetch) {
  if (url !== "https://mangadock-turso-staging.dddinmx.workers.dev")
    throw new Error(
      "Novel availability checks are restricted to Turso staging",
    );
  if (!password) throw new Error("Staging administrator secret is missing");
  const login = await request(url + "/api/v1/auth/login", {
    method: "POST",
    redirect: "error",
    signal: AbortSignal.timeout(30000),
    headers: { Origin: url, "Content-Type": "application/json" },
    body: JSON.stringify({ username: "admin", password }),
  });
  if (!login.ok)
    throw new Error(`Novel availability login HTTP ${login.status}`);
  const cookie = login.headers.get("set-cookie")?.split(";")[0];
  if (!cookie || !(await login.json()).data?.csrf_token)
    throw new Error("Novel availability login did not establish a session");
  const headers = { Origin: url, Cookie: cookie };
  const search = await request(
    url + "/api/search?q=" + encodeURIComponent("三国演义"),
    {
      headers,
      redirect: "error",
      signal: AbortSignal.timeout(30000),
    },
  );
  if (!search.ok)
    throw new Error(`Deployed novel source search HTTP ${search.status}`);
  const payload = await search.json(),
    data = payload.data;
  const items = data?.items || data?.books || data?.results;
  if (payload.ok !== true || !Array.isArray(items) || !items.length)
    throw new Error("Deployed novel source did not return real search results");
  const page = await request(url + "/novels/fanqie", {
    headers,
    redirect: "error",
    signal: AbortSignal.timeout(30000),
  });
  if (!page.ok) throw new Error(`Novel download page HTTP ${page.status}`);
  const html = await page.text();
  if (
    !/action=["']\/novels\/fanqie\/download["']/.test(html) ||
    !html.includes("直接下载")
  )
    throw new Error("Novel page did not expose its existing download form");
  return {
    observed_at: new Date().toISOString(),
    url,
    source_search_available: true,
    search_results: items.length,
    download_entry_available: true,
    download_exercised: false,
    original_user_progress_untouched: true,
    new_download_created: false,
  };
}
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try {
    const { url } = JSON.parse(await readFile("deployment.json", "utf8"));
    const report = await verifyNovelSource(
      url,
      process.env.MANGADOCK_ADMIN_PASSWORD,
    );
    await writeFile(
      "novel-source-results.json",
      JSON.stringify(report, null, 2),
    );
    console.log(JSON.stringify(report));
  } catch (error) {
    console.error("::error::" + error.message);
    process.exitCode = 1;
  }
}
