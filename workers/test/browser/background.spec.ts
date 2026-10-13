import { test, expect } from "@playwright/test";
async function login(page: any) {
  await page.goto("/login");
  await page.getByLabel("用户名").fill("admin");
  await page.getByLabel("密码").fill("local-e2e-password-2026");
  await page.locator("#loginButton").click();
  await expect(page.locator(".md-topnav")).toBeVisible();
}
test("active tasks keep polling across a fifteen-minute compute wait and stop after completion", async ({
  page,
}) => {
  await login(page);
  const response = await page.request.get("/tasks");
  const original = await response.text();
  const active = original
    .replace("let hasActiveTasks = false;", "let hasActiveTasks = true;")
    .replace('data-active="false"', 'data-active="true"');
  let requests = 0;
  await page.route("**/tasks", async (route: any) => {
    requests++;
    await route.fulfill({
      status: 200,
      contentType: "text/html",
      body: requests < 4 ? active : original,
    });
  });
  await page.clock.install();
  await page.goto("/tasks");
  await page.clock.runFor(10000);
  await expect.poll(() => requests).toBe(2);
  await page.clock.fastForward(10 * 60000);
  await expect.poll(() => requests).toBe(3);
  await page.clock.fastForward(5 * 60000);
  await expect.poll(() => requests).toBe(4);
  await page.clock.fastForward(2 * 60000);
  expect(requests).toBe(4);
});
test("an asynchronous update check keeps waiting and refreshes after the result arrives", async ({
  page,
}) => {
  await login(page);
  let checks = 0,
    statusRequests = 0;
  await page.route("**/api/v1/updates/check", async (route: any) => {
    checks++;
    await route.fulfill({
      json: { ok: true, data: { checking: true, message: "更新检查已排队" } },
    });
  });
  await page.route("**/api/v1/updates?status_only=1", async (route: any) => {
    statusRequests++;
    await route.fulfill({
      json: {
        ok: true,
        data: {
          checking: statusRequests < 2,
          total: 1,
          completed: statusRequests < 2 ? 0 : 1,
          candidates: [],
        },
      },
    });
  });
  await page.clock.install();
  await page.goto("/update");
  await page.locator("#checkUpdatesButton").click();
  await expect(page.locator("#update-success")).toContainText(
    "完成后将显示结果",
  );
  await expect(page.locator("#checkUpdatesLabel")).toHaveText("检查中...");
  await page.clock.runFor(15000);
  await expect.poll(() => statusRequests).toBe(1);
  await expect(page.locator("#checkUpdatesButton")).toBeDisabled();
  await page.clock.fastForward(16 * 60000);
  await expect.poll(() => statusRequests).toBe(2);
  await expect(page.locator("#checkUpdatesButton")).toBeEnabled();
  await page.clock.fastForward(60000);
  expect(statusRequests).toBe(2);
  expect(checks).toBe(1);
});
