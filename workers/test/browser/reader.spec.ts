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

for (const width of [1280, 390])
  test(`original comic reader loads private pages and restores scroll at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.addInitScript(() =>
      localStorage.setItem("mangadock-install-muted-until", "9999999999999"),
    );
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("/login");
    await page.getByLabel("用户名").fill("admin");
    await page.getByLabel("密码").fill("local-e2e-password-2026");
    await page.locator("#loginButton").click();
    await expect(page.locator(".md-topnav")).toBeVisible();
    await page.goto("/reader/c_test");
    await expect(page.locator(".comic-page")).toHaveCount(2);
    await expect
      .poll(() =>
        page
          .locator(".comic-page")
          .first()
          .evaluate((image: any) => image.naturalWidth),
      )
      .toBeGreaterThan(0);
    await page.locator(".comic-page").nth(1).scrollIntoViewIfNeeded();
    await expect
      .poll(() =>
        page
          .locator(".comic-page")
          .nth(1)
          .evaluate((image: any) => image.naturalWidth),
      )
      .toBeGreaterThan(0);
    await page.evaluate(() =>
      window.scrollTo(0, document.documentElement.scrollHeight - 900),
    );
    await expect
      .poll(async () => {
        const response = await page.request.get(
          "/get_progress/" + encodeURIComponent("本地测试漫画"),
        );
        return (await response.json()).scroll_position || 0;
      })
      .toBeGreaterThan(0);
    const progress = await (
      await page.request.get(
        "/get_progress/" + encodeURIComponent("本地测试漫画"),
      )
    ).json();
    await page.reload();
    await expect
      .poll(() => page.evaluate(() => window.scrollY))
      .toBeGreaterThan(0);
    const after = await (
      await page.request.get(
        "/get_progress/" + encodeURIComponent("本地测试漫画"),
      )
    ).json();
    expect(after.last_chapter).toBe(progress.last_chapter);
    expect(errors).toEqual([]);
    await page.screenshot({ path: `test-results/comic-reader-${width}.png` });
  });
