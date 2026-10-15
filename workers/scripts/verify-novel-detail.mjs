import { chromium, expect } from "@playwright/test";
import { createClient } from "@libsql/client/web";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { createHash } from "node:crypto";
import { tursoUrl } from "../src/database.ts";
const { url } = JSON.parse(await readFile("deployment.json", "utf8"));
if (url !== "https://mangadock-turso-staging.dddinmx.workers.dev")
  throw new Error("Novel detail verification is restricted to Turso staging");
const id = process.env.NOVEL_DETAIL_ID,
  title = process.env.EXPECTED_NOVEL_TITLE;
if (!id || !title)
  throw new Error("An exact local novel ID and title are required");
const client = createClient({
  url: tursoUrl(process.env.TURSO_DATABASE_URL),
  authToken: process.env.TURSO_AUTH_TOKEN,
  intMode: "number",
});
let browser;
try {
  const book = (
    await client.execute({
      sql: "SELECT id,title,description,cover_key,index_key,chapter_count FROM books WHERE id=?",
      args: [id],
    })
  ).rows[0];
  if (
    !book ||
    book.title !== title ||
    !book.description ||
    !book.cover_key ||
    !book.index_key ||
    !book.chapter_count
  )
    throw new Error("Novel detail metadata or index is not ready");
  const owner = (
    await client.execute("SELECT id FROM users WHERE username='admin'")
  ).rows[0];
  const progress = async () =>
    JSON.stringify(
      (
        await client.execute({
          sql: "SELECT * FROM progress WHERE user_id=? AND book_id=?",
          args: [owner.id, id],
        })
      ).rows,
    );
  const before = await progress();
  browser = await chromium.launch({
    headless: true,
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
  });
  const results = [];
  for (const width of [1280, 390]) {
    const context = await browser.newContext({
        viewport: { width, height: 900 },
      }),
      page = await context.newPage();
    const response = await context.request.post(url + "/api/v1/auth/login", {
      data: {
        username: "admin",
        password: process.env.MANGADOCK_ADMIN_PASSWORD,
      },
      headers: { Origin: url },
    });
    if (!response.ok()) throw new Error("Novel detail verifier login failed");
    let writes = 0;
    await page.route("**/*", async (route) => {
      if (!["GET", "HEAD"].includes(route.request().method())) {
        writes++;
        await route.abort();
      } else await route.continue();
    });
    await page.addInitScript(() =>
      localStorage.setItem("mangadock-install-muted-until", "9999999999999"),
    );
    const detail = await page.goto(url + "/novel/" + encodeURIComponent(id));
    if (!detail?.ok()) throw new Error("Novel detail HTTP " + detail?.status());
    await expect(page.locator(".md-detail-title")).toHaveText(title);
    await expect(page.locator("#novel-detail-description")).toHaveText(
      book.description,
    );
    await expect(page.locator(".chapter-item")).toHaveCount(book.chapter_count);
    const image = page.locator(".md-detail-cover > img");
    await expect(image).toBeVisible();
    await expect
      .poll(() => image.evaluate((img) => img.complete && img.naturalWidth > 0))
      .toBe(true);
    await image.evaluate(async (img) => {
      await img.decode();
      await new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      );
    });
    const cover = await context.request.get(
      url + "/novel/" + encodeURIComponent(id) + "/cover",
    );
    if (!cover.ok() || !cover.headers()["content-type"]?.startsWith("image/"))
      throw new Error("Novel real cover is unavailable");
    const bytes = await cover.body(),
      hash = createHash("sha256").update(bytes).digest("hex");
    const fallback = createHash("sha256")
      .update(await readFile("../static/default-comic-cover.jpg"))
      .digest("hex");
    if (hash === fallback)
      throw new Error("Novel cover is still a placeholder");
    await mkdir("test-results", { recursive: true });
    await page.screenshot({ path: `test-results/novel-detail-${width}.png` });
    await page.locator(".md-detail-chapters").scrollIntoViewIfNeeded();
    await page.screenshot({
      path: `test-results/novel-detail-chapters-${width}.png`,
    });
    if (writes) throw new Error("Novel detail attempted a write request");
    results.push({
      width,
      chapters: book.chapter_count,
      description_length: book.description.length,
      real_cover_decoded: true,
      cover_sha256: hash,
      write_requests: 0,
    });
    await context.close();
  }
  if ((await progress()) !== before)
    throw new Error(
      "Original owner novel progress changed during detail inspection",
    );
  const report = {
    observed_at: new Date().toISOString(),
    novel_id: id,
    title,
    results,
    original_user_progress_unchanged: true,
    owner_reader_opened: false,
    new_download_created: false,
  };
  await writeFile(
    "novel-detail-browser-results.json",
    JSON.stringify(report, null, 2),
  );
  console.log(JSON.stringify(report));
} finally {
  await browser?.close();
  client.close();
}
