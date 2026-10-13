import { test, expect } from "@playwright/test";
test("original login and reader templates: read, paginate, save progress and resume", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByText("欢迎回来")).toBeVisible();
  await page.getByLabel("用户名").fill("admin");
  await page.getByLabel("密码").fill("local-e2e-password-2026");
  await page.locator("#loginButton").click();
  await expect(page.locator(".md-topnav")).toBeVisible();
  await page.goto("/novels");
  await page.locator('a[href^="/novel/123456789/read"]').first().click();
  await expect(page.locator("#reader-flow .novel-reader-title")).toHaveText(
    "第一章",
  );
  expect(
    await page.evaluate(() => Boolean((window as any).EPUB_INJECTED)),
  ).toBe(false);
  await page.keyboard.press("ArrowRight");
  await expect
    .poll(async () => {
      const result = await page.request.get("/api/books/123456789/progress");
      return (await result.json()).data?.page || 0;
    })
    .toBeGreaterThan(0);
  const before = (
    await (await page.request.get("/api/books/123456789/progress")).json()
  ).data;
  await page.reload();
  await expect(page.locator("#reader-flow .novel-reader-title")).toHaveText(
    "第一章",
  );
  await expect(page.locator("#reader-page-status")).toContainText(
    `${before.page + 1} /`,
  );
  await page
    .locator("#reader-viewport")
    .click({ position: { x: 400, y: 220 } });
  await page.locator("#next-chapter").click();
  await expect(page.locator("#reader-flow .novel-reader-title")).toHaveText(
    "第二章",
  );
  await expect
    .poll(async () => {
      const result = await page.request.get("/api/books/123456789/progress");
      return (await result.json()).data?.chapter;
    })
    .toBe(1);
  await page.reload();
  await expect(page.locator("#reader-flow .novel-reader-title")).toHaveText(
    "第二章",
  );
  await page.screenshot({ path: "test-results/reader-desktop.png" });
  expect(errors).toEqual([]);
});
test("mobile login keeps original layout", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/login");
  await expect(page.getByLabel("用户名")).toBeVisible();
  await expect(page.locator("#loginButton")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({ path: "test-results/login-mobile.png" });
});
