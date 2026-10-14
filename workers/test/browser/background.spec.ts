import { test, expect } from "@playwright/test";
async function login(page: any) {
  await page.goto("/login");
  await page.getByLabel("用户名").fill("admin");
  await page.getByLabel("密码").fill("local-e2e-password-2026");
  await page.locator("#loginButton").click();
  await expect(page.locator(".md-topnav")).toBeVisible();
}
for (const width of [1280, 390]) {
  test(`manual comic update uses the actual API and opens its progress page at ${width}px`, async ({
    page,
  }) => {
    await page.setViewportSize({ width, height: 900 });
    await login(page);
    await page.goto("/update");
    await page.locator("#comic_name").selectOption({ label: "本地测试漫画" });
    const csrf = await page
      .locator('input[name="csrf_token"]')
      .first()
      .inputValue();
    let capture: (value: any) => void;
    const created = new Promise<any>((resolve) => {
      capture = resolve;
    });
    // Forward to the real API and capture its body before the original UI
    // navigates away; DevTools can discard a response after navigation.
    await page.route("**/api/v1/updates", async (route) => {
      const response = await route.fetch();
      capture({ status: response.status(), result: await response.json() });
      await route.fulfill({ response });
    });
    await page.locator("#updateButton").click();
    const { status, result } = await created;
    expect(status).toBe(201);
    expect(result.ok).toBe(true);
    expect(result.data.task_id).toBeTruthy();
    expect(result.data.task_ids).toEqual([result.data.task_id]);
    await page.waitForURL(`**/progress/${result.data.task_id}`);
    await expect(page.locator("#update-error:visible")).toHaveCount(0);
    const options = {
      headers: { "X-CSRFToken": csrf, Origin: "http://localhost:8787" },
      data: {},
    };
    expect(
      (
        await page.request.post(
          `/api/v1/tasks/${result.data.task_id}/cancel`,
          options,
        )
      ).ok(),
    ).toBeTruthy();
    expect(
      (
        await page.request.delete(
          `/api/v1/tasks/${result.data.task_id}`,
          options,
        )
      ).ok(),
    ).toBeTruthy();
  });
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
  // The route counter advances before the response is parsed and the next
  // timer is installed. Await that timer before advancing mocked time.
  const pollSettled = (scheduled: boolean) =>
    expect
      .poll(() =>
        page.evaluate(() => ({
          pending: eval("refreshPending"),
          scheduled: eval("autoRefreshTimer") !== null,
        })),
      )
      .toEqual({ pending: false, scheduled });
  await pollSettled(true);
  await page.clock.fastForward(10 * 60000);
  await expect.poll(() => requests).toBe(3);
  await pollSettled(true);
  await page.clock.fastForward(5 * 60000);
  await expect.poll(() => requests).toBe(4);
  await pollSettled(false);
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
