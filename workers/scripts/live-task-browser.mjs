import { chromium, expect } from "@playwright/test";
import { readFile, writeFile, mkdir } from "node:fs/promises";
const { url } = JSON.parse(await readFile("deployment.json", "utf8"));
const browser = await chromium.launch({
  headless: true,
  executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined,
});
const results = [];
try {
  const context = await browser.newContext();
  const login = await context.request.post(url + "/api/v1/auth/login", {
    headers: { Origin: url },
    data: { username: "admin", password: process.env.MANGADOCK_ADMIN_PASSWORD },
  });
  if (!login.ok()) throw new Error(`Task login HTTP ${login.status()}`);
  const list = await context.request.get(url + "/api/v1/tasks");
  if (!list.ok()) throw new Error(`Tasks HTTP ${list.status()}`);
  const items = (await list.json()).data.items;
  const task =
    items.find((t) => t.comic_name?.includes("财富自由后")) ||
    items.find(
      (t) => !["completed", "cancelled", "error"].includes(t.status),
    ) ||
    items[0];
  if (!task) throw new Error("No real task available for task-page validation");
  for (const width of [1280, 390]) {
    const page = await context.newPage();
    await page.setViewportSize({ width, height: 900 });
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript(() =>
      localStorage.setItem("mangadock-install-muted-until", "9999999999999"),
    );
    const detail = page.waitForResponse(
      (r) =>
        new URL(r.url()).pathname === `/api/v1/tasks/${task.id}` &&
        r.request().method() === "GET",
      { timeout: 30000 },
    );
    await page.goto(url + "/progress/" + encodeURIComponent(task.id));
    const response = await detail;
    if (!response.ok())
      throw new Error(`Task detail HTTP ${response.status()}`);
    const data = (await response.json()).data;
    if (!Array.isArray(data.log) || !data.log.length)
      throw new Error("Task log contract is missing");
    for (const key of ["start_time", "end_time"])
      if (data[key] !== null && !Number.isFinite(Date.parse(data[key])))
        throw new Error(`Invalid ${key}`);
    await expect(page.locator("#status")).toHaveText(data.status_label);
    await expect(
      page.locator("#log-container .md-log-line").first(),
    ).toHaveText(data.log[0]);
    if (data.status === "error")
      await expect(page.locator("#error-message")).toBeVisible();
    else await expect(page.locator("#error-message")).toBeHidden();
    for (const [key, id] of [
      ["start_time", "#start-time"],
      ["end_time", "#end-time"],
    ]) {
      if (data[key] === null)
        await expect(page.locator(id)).toHaveText("--:--:--");
      else await expect(page.locator(id)).not.toHaveText("--:--:--");
    }
    if (errors.length) throw new Error("Task-page JavaScript error");
    await mkdir("test-results", { recursive: true });
    await page.screenshot({
      path: `test-results/live-task-${width}.png`,
      fullPage: true,
    });
    results.push({
      width,
      task_id: task.id,
      comic_name: data.comic_name,
      state: data.state,
      status: data.status,
      status_label: data.status_label,
      total_chapters: data.total_chapters,
      completed_chapters: data.completed_chapters,
      progress_percent: data.progress_percent,
      has_start_time: !!data.start_time,
      has_end_time: !!data.end_time,
      log_lines: data.log.length,
      javascript_errors: errors.length,
    });
    await page.close();
  }
  await writeFile(
    "live-task-results.json",
    JSON.stringify({ url, results }, null, 2),
  );
  console.log(JSON.stringify({ real_task_pages: results }));
} finally {
  await browser.close();
}
