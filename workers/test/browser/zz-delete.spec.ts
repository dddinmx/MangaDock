import { test, expect } from "@playwright/test";

// Runs after the reader tests; delete only the disposable local seed fixture.
test("original comic deletion form works with title-only chapters and neutral dark surfaces", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await page.addInitScript(() =>
    localStorage.setItem("mangadock-theme", "dark"),
  );
  await page.goto("/login");
  await page.getByLabel("用户名").fill("admin");
  await page.getByLabel("密码").fill("local-e2e-password-2026");
  await page.locator("#loginButton").click();
  await expect(page.locator(".md-topnav")).toBeVisible();
  await page.goto("/comic/c_test");
  await expect(page.locator("#chapter-list .chapter-title")).toHaveText(
    "第一话",
  );
  await expect(page.locator("#chapter-list .chapter-size")).toHaveCount(0);
  expect(
    await page.evaluate(() => getComputedStyle(document.body).backgroundColor),
  ).toBe("rgb(18, 18, 18)");
  await page.getByRole("button", { name: "更多操作" }).click();
  page.once("dialog", (dialog) => dialog.accept());
  const submitted = page.waitForResponse(
    (r) =>
      new URL(r.url()).pathname === "/comic/delete" &&
      r.request().method() === "POST",
  );
  await page.getByRole("button", { name: "删除漫画及源文件" }).click();
  expect((await submitted).status()).toBe(303);
  await page.waitForURL("**/comics?*");
  expect((await page.request.get("/comic/c_test")).status()).toBe(404);
});
