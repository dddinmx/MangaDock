import { test, expect } from "@playwright/test";
import { waitForVisibleComicImages } from "../../scripts/comic-reader-evidence.mjs";

async function login(page) {
  await page.goto("/login");
  await page.getByLabel("用户名").fill("admin");
  await page.getByLabel("密码").fill("local-e2e-password-2026");
  await page.locator("#loginButton").click();
  await expect(page.locator(".md-topnav")).toBeVisible();
}

for (const width of [1280, 390]) {
  test(`whole-book evidence waits for real images after reload at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await login(page);
    await page.goto("/reader/c_test");
    await waitForVisibleComicImages(page);
    let release;
    const gate = new Promise((resolve) => (release = resolve));
    await page.route("**/api/comic-pages/**", async (route) => {
      if (new URL(route.request().url()).searchParams.has("page")) await gate;
      await route.continue();
    });
    await page.reload();
    await expect(page.locator(".cbz-page-retry").first()).toBeVisible();
    let accepted = false;
    const evidence = waitForVisibleComicImages(page).then((count) => {
      accepted = true;
      return count;
    });
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(accepted).toBe(false);
    release();
    expect(await evidence).toBeGreaterThan(0);
    await expect(
      page.locator('.cbz-page[data-loading="loaded"] .cbz-page-retry'),
    ).toHaveCount(0);
  });
}

test("whole-book evidence rejects a reader containing only failed image placeholders", async ({
  page,
}) => {
  await login(page);
  await page.route("**/api/comic-pages/**", async (route) => {
    if (new URL(route.request().url()).searchParams.has("page"))
      await route.fulfill({ status: 503, body: "Unavailable" });
    else await route.continue();
  });
  await page.goto("/reader/c_test");
  await expect(page.locator(".cbz-page-retry").first()).toBeVisible();
  await expect(waitForVisibleComicImages(page, 1000)).rejects.toThrow(
    /Visible comic images must replace loading placeholders/,
  );
});
