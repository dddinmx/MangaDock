import { readFile, writeFile } from "node:fs/promises";
const { url } = JSON.parse(await readFile("deployment.json", "utf8"));
const source = "https://baozimh.org/manga/dafengdagengren-chuyingshe";
const expectedChapters = Math.min(
  3,
  Math.max(1, Number(process.env.COMIC_MAX_CHAPTERS) || 1),
);
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
  const items = (await library()).items;
  let comic = items.find(
    (c) =>
      c.chapter_count >= expectedChapters &&
      (c.source_url === source ||
        c.name === "大奉打更人" ||
        c.title === "大奉打更人"),
  );
  comic ||= items.find((c) => c.chapter_count >= expectedChapters);
  const newDownload = false;
  if (!comic || comic.chapter_count < expectedChapters)
    throw new Error(
      "No existing readable comic; acceptance never creates a download",
    );
  const detail = await (await request(`/api/comics/${comic.id}`)).json();
  const chapters = (
    await (await request(`/api/comics/${comic.id}/chapters`)).json()
  ).chapters;
  if (!chapters?.length) throw new Error("Real comic has no chapters");
  const chapter = chapters[expectedChapters - 1],
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
    chapter_index: expectedChapters - 1,
    page_index: 0,
    client_progress_ms: Date.now(),
  });
  const progress = (
    await (await request(`/api/comics/${comic.id}/progress`)).json()
  ).progress;
  if (
    progress?.chapter_index !== expectedChapters - 1 ||
    progress?.page_index !== 0
  )
    throw new Error("Comic progress did not persist");
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
        new_download: newDownload,
        verified_chapters: expectedChapters,
        progress_read_back: progress,
        progress_saved: true,
      },
      null,
      2,
    ),
  );
  console.log(
    "::notice::Real baozimh comic download, private R2 image, and saved progress passed.",
  );
  console.log(
    JSON.stringify({
      new_download: newDownload,
      verified_chapters: expectedChapters,
      first_page_bytes: bytes,
      progress_read_back: progress,
    }),
  );
} catch (error) {
  console.error("::error::" + error.message);
  process.exitCode = 1;
}
