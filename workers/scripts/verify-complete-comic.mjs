import assert from "node:assert/strict";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { chromium, expect } from "@playwright/test";

const { url } = JSON.parse(await readFile("deployment.json", "utf8"));
const comicId = process.env.COMPLETE_COMIC_ID || "";
const taskId = process.env.DIAGNOSTIC_TASK_ID || "";
if (
  new URL(url).hostname !== "mangadock-turso-staging.dddinmx.workers.dev" ||
  !/^c_[a-f0-9]{12}$/.test(comicId) ||
  !/^[a-f0-9-]{36}$/.test(taskId)
)
  throw new Error(
    "An exact staging comic ID and existing task UUID are required",
  );
let cookie = "",
  csrf = "";
const auditPath = `/api/admin/comics/${comicId}/audit`;
const report = {
  comic_id: comicId,
  task_id: taskId,
  new_download: false,
  started_at: new Date().toISOString(),
  complete: false,
};
async function request(path, method = "GET", body) {
  const response = await fetch(url + path, {
    method,
    headers: {
      Origin: url,
      Cookie: cookie,
      "Content-Type": "application/json",
      "X-CSRFToken": csrf,
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
    signal: AbortSignal.timeout(60000),
    redirect: "error",
  });
  if (!response.ok)
    throw new Error(
      `Comic verification HTTP ${response.status} at ${new URL(url + path).pathname}`,
    );
  return response;
}
const audit = (params) =>
  request(auditPath + "?" + new URLSearchParams(params))
    .then((r) => r.json())
    .then((r) => r.data);
let originalProgress, browser;
let checkId;
async function publishStatus(snapshot, conclusion) {
  // Update one CI check rather than polling the database from multiple tools.
  if (
    !process.env.GITHUB_TOKEN ||
    !process.env.GITHUB_REPOSITORY ||
    !process.env.GITHUB_SHA
  )
    return;
  const summary = snapshot
    ? {
        observed_at: new Date().toISOString(),
        task: snapshot.task,
        ready_chapters: snapshot.chapters.filter((c) => c.state === "ready")
          .length,
        ready_images: snapshot.chapters
          .filter((c) => c.state === "ready")
          .reduce((sum, c) => sum + c.page_count, 0),
      }
    : report;
  const body = {
    status: conclusion ? "completed" : "in_progress",
    ...(conclusion
      ? { conclusion, completed_at: new Date().toISOString() }
      : {}),
    output: {
      title: conclusion
        ? `Whole-book validation: ${conclusion}`
        : `Downloaded ${summary.task?.completed_chapters || 0}/${summary.task?.source_total || "?"} update chapters`,
      summary: "```json\n" + JSON.stringify(summary, null, 2) + "\n```",
    },
  };
  const response = await fetch(
    `https://api.github.com/repos/${process.env.GITHUB_REPOSITORY}/check-runs${checkId ? "/" + checkId : ""}`,
    {
      method: checkId ? "PATCH" : "POST",
      headers: {
        Authorization: `Bearer ${process.env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        ...(!checkId
          ? {
              name: "Complete comic verification",
              head_sha: process.env.GITHUB_SHA,
              external_id: taskId,
            }
          : {}),
        ...body,
      }),
      signal: AbortSignal.timeout(15000),
    },
  );
  if (!response.ok)
    throw new Error("CI progress status HTTP " + response.status);
  checkId = (await response.json()).id;
}
try {
  const login = await request("/api/v1/auth/login", "POST", {
    username: "admin",
    password: process.env.MANGADOCK_ADMIN_PASSWORD,
  });
  cookie = login.headers.get("set-cookie")?.split(";")[0] || "";
  csrf = (await login.json()).data.csrf_token;
  // Full books take longer than a smoke test; keep the check finite without
  // treating a healthy multi-hour native download as a 45-minute failure.
  const deadline = Date.now() + 240 * 60000;
  let snapshot;
  for (;;) {
    snapshot = await audit({ task_id: taskId });
    if (!snapshot.task)
      throw new Error(
        "The specified task disappeared; verification cannot create a replacement",
      );
    const ready = snapshot.chapters.filter((c) => c.state === "ready").length;
    console.log(
      JSON.stringify({
        observed_at: new Date().toISOString(),
        task: snapshot.task,
        ready_chapters: ready,
      }),
    );
    report.latest = { task: snapshot.task, ready_chapters: ready };
    await publishStatus(snapshot).catch((error) =>
      console.log("::notice::" + error.message),
    );
    if (snapshot.task.state === "completed") break;
    if (["error", "cancelled"].includes(snapshot.task.state))
      throw new Error("The existing book download did not finish successfully");
    if (Date.now() >= deadline)
      throw new Error(
        "Whole-book completion was not reached within the bounded verification window",
      );
    await new Promise((resolve) => setTimeout(resolve, 60000));
  }
  snapshot = await audit({ task_id: taskId, catalogue: "1" });
  assert.equal(snapshot.task.completed_chapters, snapshot.task.source_total);
  assert.equal(snapshot.task.source_cursor, snapshot.task.source_total);
  assert.equal(
    snapshot.chapters.every(
      (c) => c.state === "ready" && c.format === "images" && c.page_count > 0,
    ),
    true,
  );
  const expectedIds = new Set(snapshot.source_chapter_ids);
  assert.equal(expectedIds.size, snapshot.source_chapter_ids.length);
  assert.equal(snapshot.chapters.length, expectedIds.size);
  assert.deepEqual(
    new Set(snapshot.chapters.map((c) => String(c.source_id))),
    expectedIds,
  );
  const objects = new Map();
  let cursor = "";
  for (let page = 0; page < 100; page++) {
    const batch = await audit({ objects: "1", ...(cursor ? { cursor } : {}) });
    for (const object of batch.objects) objects.set(object.key, object);
    if (!batch.cursor) {
      cursor = "";
      break;
    }
    assert.notEqual(batch.cursor, cursor);
    cursor = batch.cursor;
  }
  assert.equal(cursor, "", "R2 listing exceeded its bounded page limit");
  let images = 0,
    bytes = 0;
  const allKeys = new Set(),
    verifiedChapters = [];
  for (const chapter of snapshot.chapters) {
    const manifest = await audit({ chapter_id: chapter.id });
    assert.equal(manifest.state, "ready");
    assert.equal(manifest.keys.length, chapter.page_count);
    assert.equal(new Set(manifest.keys).size, chapter.page_count);
    const prefix = `comics/${comicId}/chapters/${chapter.id}/`;
    for (const key of manifest.keys) {
      assert.equal(typeof key, "string");
      assert.equal(
        key.startsWith(prefix),
        true,
        "Manifest references an unexpected storage prefix",
      );
      assert.equal(allKeys.has(key), false, "Two chapters reuse an image key");
      const object = objects.get(key);
      assert.ok(
        object && object.size > 0 && object.type?.startsWith("image/"),
        "A chapter image is missing or empty in R2",
      );
      allKeys.add(key);
      images++;
      bytes += object.size;
    }
    const pages = await (
      await request(`/api/comics/${comicId}/chapters/${chapter.id}/pages`)
    ).json();
    assert.equal(pages.total_pages, chapter.page_count);
    // Read both ends of every chapter through the real authenticated reader.
    for (const index of new Set([0, chapter.page_count - 1])) {
      const response = await request(
        `/api/comics/${comicId}/chapters/${chapter.id}/pages/${index}`,
      );
      const content = new Uint8Array(await response.arrayBuffer());
      assert.equal(
        response.headers.get("content-type")?.startsWith("image/"),
        true,
      );
      assert.equal(content.byteLength, objects.get(manifest.keys[index]).size);
    }
    verifiedChapters.push({ id: chapter.id, pages: chapter.page_count });
  }
  originalProgress = (
    await (await request(`/api/comics/${comicId}/progress`)).json()
  ).progress;
  const lastIndex = snapshot.chapters.length - 1,
    lastChapter = snapshot.chapters[lastIndex];
  await request(`/api/comics/${comicId}/progress`, "POST", {
    chapter_index: lastIndex,
    page_index: 0,
    scroll_position: 0,
    total_chapters: snapshot.chapters.length,
    total_pages: lastChapter.page_count,
    client_progress_ms: Date.now(),
  });
  const saved = (
    await (await request(`/api/comics/${comicId}/progress`)).json()
  ).progress;
  assert.equal(saved.chapter_index, lastIndex);
  assert.equal(saved.page_index, 0);
  browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
  });
  const context = await browser.newContext();
  await context.addCookies([
    {
      name: cookie.split("=")[0],
      value: cookie.slice(cookie.indexOf("=") + 1),
      url,
    },
  ]);
  const browserResults = [];
  await mkdir("test-results", { recursive: true });
  for (const width of [1280, 390]) {
    const page = await context.newPage({ viewport: { width, height: 900 } });
    await page.setViewportSize({ width, height: 900 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.name));
    await page.addInitScript(() =>
      localStorage.setItem("mangadock-install-muted-until", "9999999999999"),
    );
    await page.goto(url + `/reader/${comicId}?start_chapter=${lastIndex}`);
    await expect
      .poll(
        () =>
          page
            .locator(".comic-page")
            .first()
            .evaluate((image) => image.naturalWidth),
        { timeout: 60000 },
      )
      .toBeGreaterThan(0);
    await page.evaluate(() => window.scrollTo(0, 600));
    await expect
      .poll(
        async () =>
          (await (await request(`/api/comics/${comicId}/progress`)).json())
            .progress.scroll_position,
        { timeout: 30000 },
      )
      .toBeGreaterThan(0);
    await page.reload();
    await expect
      .poll(() => page.evaluate(() => window.scrollY), { timeout: 30000 })
      .toBeGreaterThan(0);
    assert.deepEqual(errors, []);
    await page.screenshot({ path: `test-results/complete-comic-${width}.png` });
    browserResults.push({
      width,
      last_chapter_readable: true,
      scroll_restored: true,
    });
    await page.close();
  }
  await browser.close();
  browser = undefined;
  Object.assign(report, {
    complete: true,
    finished_at: new Date().toISOString(),
    source_chapters: expectedIds.size,
    ready_chapters: snapshot.chapters.length,
    verified_images: images,
    image_bytes: bytes,
    every_image_present_in_r2: true,
    every_chapter_first_and_last_image_read: true,
    verified_chapters: verifiedChapters,
    progress_saved_and_read_back: true,
    browser: browserResults,
    task: snapshot.task,
  });
} catch (error) {
  report.failure = error.message;
  console.error("::error::" + error.message);
  process.exitCode = 1;
} finally {
  await browser?.close();
  if (originalProgress) {
    await request(`/api/comics/${comicId}/progress`, "POST", {
      ...originalProgress,
      client_progress_ms: Date.now() + 1000,
    })
      .then(() => {
        report.original_progress_restored = true;
      })
      .catch(() => {
        report.original_progress_restored = false;
        process.exitCode = 1;
      });
  }
  await writeFile(
    "complete-comic-results.json",
    JSON.stringify(report, null, 2),
  );
  console.log(JSON.stringify(report));
  await publishStatus(
    null,
    report.complete && process.exitCode !== 1 ? "success" : "failure",
  ).catch((error) => console.log("::notice::" + error.message));
}
