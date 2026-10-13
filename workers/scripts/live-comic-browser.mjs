import { chromium, expect } from "@playwright/test";
import { readFile, writeFile, mkdir } from "node:fs/promises";
const { url } = JSON.parse(await readFile("deployment.json", "utf8"));
const { comic_id } = JSON.parse(
  await readFile("live-comic-results.json", "utf8"),
);
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
});
const results = [];
try {
  for (const width of [1280, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript(() =>
      localStorage.setItem("mangadock-install-muted-until", "9999999999999"),
    );
    await page.goto(url + "/login");
    await page.getByLabel("用户名").fill("admin");
    await page.getByLabel("密码").fill(process.env.MANGADOCK_ADMIN_PASSWORD);
    await page.locator("#loginButton").click();
    await page.waitForURL(url+'/',{timeout:30000});
    await expect(page.locator(".md-topnav")).toBeVisible({timeout:30000});
    await page.goto(url + "/reader/" + comic_id);
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
    const progressUrl = url + "/get_progress/" + encodeURIComponent(comic_id);
    await expect
      .poll(
        async () => {
          const response = await page.request.get(progressUrl);
          return (await response.json()).scroll_position || 0;
        },
        { timeout: 30000 },
      )
      .toBeGreaterThan(0);
    const progress = await (await page.request.get(progressUrl)).json();
    await page.reload();
    await expect
      .poll(() => page.evaluate(() => window.scrollY), { timeout: 30000 })
      .toBeGreaterThan(0);
    if (errors.length) throw new Error("Comic reader JavaScript error");
    await mkdir("test-results", { recursive: true });
    await page.screenshot({ path: `test-results/live-comic-${width}.png` });
    results.push({
      width,
      comic_id,
      real_image_loaded: true,
      scroll_restored: true,
      progress,
    });
    await page.close();
  }
  await writeFile(
    "live-comic-browser-results.json",
    JSON.stringify({ url, results }, null, 2),
  );
  console.log(
    "Real comic browser: private pages and progress restoration passed at 1280px and 390px.",
  );
} finally {
  await browser.close();
}
