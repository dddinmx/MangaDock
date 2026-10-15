import assert from "node:assert/strict";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { chromium, expect } from "@playwright/test";
import {
  createValidationReader,
  removeValidationReader,
  cleanFailedRunReader,
} from "./validation-reader.mjs";

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
let adminSession, validationUser;
const auditPath = `/api/admin/comics/${comicId}/audit`;
const report = {
  comic_id: comicId,
  task_id: taskId,
  new_download: false,
  started_at: new Date().toISOString(),
  complete: false,
};
async function request(path, method = "GET", body) {
  const session =
    path.startsWith("/api/admin/") && adminSession
      ? adminSession
      : { cookie, csrf };
  const response = await fetch(url + path, {
    method,
    headers: {
      Origin: url,
      Cookie: session.cookie,
      "Content-Type": "application/json",
      "X-CSRFToken": session.csrf,
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
const checkCommit =
  process.env.COMPLETE_COMIC_CHECK_SHA || process.env.GITHUB_SHA;
async function publishStatus(snapshot, conclusion) {
  // Update one CI check rather than polling the database from multiple tools.
  if (
    !process.env.GITHUB_TOKEN ||
    !process.env.GITHUB_REPOSITORY ||
    !/^[a-f0-9]{40}$/.test(checkCommit || "")
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
              head_sha: checkCommit,
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
  adminSession = { cookie, csrf };
  report.recovered_failed_run_readers_removed =
    await cleanFailedRunReader(request);
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
  // Progress checks must not overwrite the owner's reading while this long
  // verification runs overnight. Use a removable ordinary reader scoped to
  // the comic's existing group; administrative audits keep the admin session.
  const detail = (await (await request(`/api/comics/${comicId}`)).json()).item;
  const username = `md-verify-${crypto.randomUUID().slice(0, 12)}`;
  const password = crypto.randomUUID() + crypto.randomUUID();
  // Keep the exact generated username before POST so malformed responses and
  // failed identity assertions still reach verified cleanup in finally.
  validationUser = { username };
  validationUser = await createValidationReader(request, {
    username,
    password,
    role: "user",
    group_names: detail.group_name ? [detail.group_name] : [],
  });
  assert.equal(validationUser.role, "user");
  assert.match(String(validationUser.id), /^\d{1,20}$/);
  const readerLogin = await request("/api/v1/auth/login", "POST", {
    username,
    password,
  });
  cookie = readerLogin.headers.get("set-cookie")?.split(";")[0] || "";
  const readerIdentity = (await readerLogin.json()).data;
  csrf = readerIdentity.csrf_token;
  assert.equal(readerIdentity.user.role, "user");
  assert.equal(String(readerIdentity.user.id), String(validationUser.id));
  const me = (await (await request("/api/v1/auth/me")).json()).data.user;
  assert.equal(me.role, "user");
  assert.equal(String(me.id), String(validationUser.id));
  report.original_user_progress_untouched = true;
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
    verifiedChapters = [],
    manifests = [],
    invalidImages = [];
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
      allKeys.add(key);
      images++;
      if (!object || object.size <= 0 || !object.type?.startsWith("image/"))
        invalidImages.push({
          chapter_id: chapter.id,
          key,
          present: !!object,
          size: object?.size ?? null,
          type: object?.type ?? null,
        });
      else bytes += object.size;
    }
    manifests.push({ chapter, keys: manifest.keys });
  }
  report.r2_inventory = {
    listed_objects: objects.size,
    manifest_images: images,
    invalid_images: invalidImages.length,
    invalid_samples: invalidImages.slice(0, 25),
    invalid_chapters: [
      ...new Set(invalidImages.map((image) => image.chapter_id)),
    ],
  };
  assert.equal(
    invalidImages.length,
    0,
    "R2 inventory has missing, empty or non-image objects",
  );
  for (const { chapter, keys } of manifests) {
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
      assert.equal(content.byteLength, objects.get(keys[index]).size);
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
  if (validationUser && adminSession) {
    cookie = adminSession.cookie;
    csrf = adminSession.csrf;
    await removeValidationReader(request, validationUser)
      .then(() => {
        report.validation_user_removed = true;
      })
      .catch(() => {
        report.validation_user_removed = false;
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
