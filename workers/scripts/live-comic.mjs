import { readFile, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
const { url } = JSON.parse(await readFile("deployment.json", "utf8"));
const source = "https://baozimh.org/manga/dafengdagengren-chuyingshe";
let cookie = "",
  csrf = "";
async function request(path, method = "GET", body) {
  const response = await fetch(url + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      Origin: url,
      Cookie: cookie,
      "X-CSRFToken": csrf,
    },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(60000),
  });
  if (!response.ok) throw new Error(`${path} HTTP ${response.status}`);
  return response;
}
try {
  const response = await request("/api/v1/auth/login", "POST", {
    username: "admin",
    password: process.env.MANGADOCK_ADMIN_PASSWORD,
  });
  cookie = response.headers.get("set-cookie").split(";")[0];
  csrf = (await response.json()).data.csrf_token;
  const library = () => request("/api/comics").then((r) => r.json());
  let items = (await library()).items;
  let comic = items.find(
    (c) =>
      c.source_url === source ||
      c.name === "大奉打更人" ||
      c.title === "大奉打更人",
  );
  if (!comic)
    await request("/api/v1/downloads", "POST", {
      url: source,
      format: 2,
      max_chapters: 1,
    });
  for (let attempt = 0; attempt < 8; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 10000));
    const run = spawnSync(
      "python",
      ["scripts/cloud-compute.py", "--limit", "10"],
      { stdio: "inherit", env: { ...process.env, MANGADOCK_URL: url } },
    );
    if (run.status !== 0) throw new Error("Cloud comic conversion failed");
    items = (await library()).items;
    comic = items.find(
      (c) => c.name === "大奉打更人" || c.title === "大奉打更人",
    );
    if (comic?.chapter_count > 0) break;
  }
  if (!comic?.chapter_count)
    throw new Error("Real comic download did not become readable");
  const detail = await (await request(`/api/comics/${comic.id}`)).json();
  const chapters = (
    await (await request(`/api/comics/${comic.id}/chapters`)).json()
  ).chapters;
  if (!chapters?.length) throw new Error("Real comic has no chapters");
  const chapter = chapters[0],
    chapterId = chapter.chapter_id || chapter.id;
  const pages = await (
    await request(`/api/comics/${comic.id}/chapters/${chapterId}/pages`)
  ).json();
  const file = await request(
    `/api/comics/${comic.id}/chapters/${chapterId}/pages/0`,
  );
  const bytes = (await file.arrayBuffer()).byteLength;
  if (!file.headers.get("content-type")?.startsWith("image/") || bytes < 100)
    throw new Error("Real comic page is missing");
  await request(`/api/comics/${comic.id}/progress`, "POST", {
    chapter_index: 0,
    page_index: 0,
    client_progress_ms: Date.now(),
  });
  await writeFile(
    "live-comic-results.json",
    JSON.stringify(
      {
        url,
        source,
        comic_id: comic.id,
        chapter_id: chapterId,
        pages,
        first_page_bytes: bytes,
        progress_saved: true,
      },
      null,
      2,
    ),
  );
  console.log(
    "::notice::Real baozimh comic download, private R2 image, and saved progress passed.",
  );
} catch (error) {
  console.error("::error::" + error.message);
  process.exitCode = 1;
}
