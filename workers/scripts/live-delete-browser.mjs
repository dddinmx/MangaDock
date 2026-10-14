import { chromium, expect } from "@playwright/test";
import { createClient } from "@libsql/client/web";
import { randomUUID } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { tursoUrl } from "../src/database.ts";

const { url } = JSON.parse(await readFile("deployment.json", "utf8"));
if (new URL(url).hostname !== "mangadock-turso-staging.dddinmx.workers.dev")
  throw new Error("Deletion acceptance is restricted to staging");
const client = createClient({
  url: tursoUrl(process.env.TURSO_DATABASE_URL),
  authToken: process.env.TURSO_AUTH_TOKEN,
});
const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
});
const results = [];
try {
  const context = await browser.newContext();
  const login = await context.request.post(url + "/api/v1/auth/login", {
    headers: { Origin: url },
    data: { username: "admin", password: process.env.MANGADOCK_ADMIN_PASSWORD },
  });
  if (!login.ok()) throw new Error(`Deletion login HTTP ${login.status()}`);
  for (const width of [1280, 390]) {
    // Only these freshly created, random-ID acceptance fixtures are deleted.
    const id = "delete-probe-" + randomUUID();
    const chapterId = "ch_" + id;
    const page = await context.newPage();
    try {
      await client.batch(
        [
          {
            sql: "INSERT INTO comics(id,name,provider,source_url,created_at,updated_at) VALUES (?,?,'baozimh_org','https://example.com/deletion-probe',?,?)",
            args: [
              id,
              "删除回归验证-" + id,
              Math.floor(Date.now() / 1000),
              Math.floor(Date.now() / 1000),
            ],
          },
          {
            sql: "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,state) VALUES (?,?,0,'回归测试章节','deletion-probe','ready')",
            args: [chapterId, id],
          },
        ],
        "write",
      );
      await page.setViewportSize({ width, height: 900 });
      await page.addInitScript(() =>
        localStorage.setItem("mangadock-theme", "dark"),
      );
      await page.goto(url + "/comic/" + encodeURIComponent(id));
      await expect(page.locator("#chapter-list .chapter-title")).toHaveText(
        "回归测试章节",
      );
      await expect(page.locator("#chapter-list .chapter-size")).toHaveCount(0);
      const background = await page.evaluate(
        () => getComputedStyle(document.body).backgroundColor,
      );
      const channels = background.match(/\d+/g)?.slice(0, 3).map(Number);
      if (!channels || channels.some((value) => value !== channels[0]))
        throw new Error("Dark detail background is not neutral grey");
      const form = page.locator('form[action="/comic/delete"]');
      await expect(form).toHaveCount(1);
      await page.locator("[data-detail-menu-toggle]").click();
      page.once("dialog", (dialog) => dialog.accept());
      const submitted = page.waitForResponse(
        (r) =>
          new URL(r.url()).pathname === "/comic/delete" &&
          r.request().method() === "POST",
      );
      await form.getByRole("button", { name: "删除漫画及源文件" }).click();
      const response = await submitted;
      if (response.status() !== 303)
        throw new Error(`Deletion HTTP ${response.status()}`);
      await page.waitForURL("**/comics?*");
      const remaining = await client.execute({
        sql: "SELECT id FROM comics WHERE id=?",
        args: [id],
      });
      if (remaining.rows.length)
        throw new Error("Deleted fixture remains in database");
      results.push({
        width,
        status: response.status(),
        metadata_deleted: true,
        chapter_titles_only: true,
        dark_background: background,
      });
    } finally {
      await page.close();
      await client.batch(
        [
          { sql: "DELETE FROM chapters WHERE comic_id=?", args: [id] },
          { sql: "DELETE FROM comics WHERE id=?", args: [id] },
        ],
        "write",
      );
    }
  }
  await writeFile(
    "live-delete-results.json",
    JSON.stringify({ results }, null, 2),
  );
  console.log(JSON.stringify({ deletion_acceptance: results }));
} finally {
  await browser.close();
  client.close();
}
