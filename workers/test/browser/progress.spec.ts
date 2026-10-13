import { test, expect } from "@playwright/test";
import { spawnSync } from "node:child_process";

for (const viewport of [
  { width: 1280, height: 900 },
  { width: 390, height: 844 },
]) {
  test(`task details display waiting, log strings and execution times at ${viewport.width}px`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    const reference = spawnSync(
      process.env.MANGADOCK_REFERENCE_PYTHON || "python3",
      ["scripts/render-reference.py"],
      {
        input: JSON.stringify({
          template: "progress.html",
          endpoint: "progress",
          values: { task_id: "timing-root", is_novel_task: false },
        }),
        encoding: "utf8",
      },
    );
    if (reference.status !== 0) throw new Error(reference.stderr);
    await page.route("**/progress/timing-root", (route) =>
      route.fulfill({ contentType: "text/html", body: reference.stdout }),
    );
    const failures: string[] = [];
    page.on("pageerror", (error) => failures.push(error.message));
    let requests = 0;
    let data: any = {
      status: "waiting",
      status_label: "等待云执行器",
      total_chapters: 125,
      completed_chapters: 0,
      progress_percent: 0,
      start_time: null,
      end_time: null,
      log: ["任务已排队，云执行器尚未开始；定时调度可能延迟，请勿重复提交。"],
    };
    await page.route("**/api/v1/tasks/timing-root", (route) => {
      requests++;
      return route.fulfill({ json: { ok: true, data } });
    });
    await page.clock.install();
    await page.goto("/progress/timing-root");
    await expect(page.locator("#status")).toHaveText("等待云执行器");
    await expect(page.locator("#log-container")).toContainText(
      "定时调度可能延迟",
    );
    await expect(page.locator("#start-time")).toHaveText("--:--:--");
    await expect(page.locator("#error-message")).toBeHidden();
    await page.screenshot({
      path: `test-results/task-waiting-${viewport.width}.png`,
      fullPage: true,
    });
    await page.clock.runFor(1000);
    await page.clock.fastForward(17 * 60000);
    await expect.poll(() => requests).toBeGreaterThan(1);
    data = {
      ...data,
      status: "running",
      status_label: "云端处理中",
      start_time: "2026-10-13T23:01:00Z",
      log: "兼容旧接口的日志字符串 <script>bad()</script>",
    };
    await page.clock.runFor(15000);
    await expect(page.locator("#status")).toHaveText("云端处理中");
    await expect(page.locator("#start-time")).not.toHaveText("--:--:--");
    await expect(page.locator("#log-container")).toContainText(
      "<script>bad()</script>",
    );
    expect(await page.locator("#log-container script").count()).toBe(0);
    await expect(page.locator("#error-message")).toBeHidden();
    data = {
      ...data,
      status: "completed",
      status_label: "已完成",
      completed_chapters: 125,
      progress_percent: 100,
      end_time: "2026-10-13T23:02:00Z",
      log: ["下载完成"],
    };
    await page.clock.runFor(15000);
    await expect(page.locator("#status")).toHaveText("已完成");
    await expect(page.locator("#progress-percent")).toHaveText("100%");
    await expect(page.locator("#end-time")).not.toHaveText("--:--:--");
    const completedRequests = requests;
    await page.clock.runFor(60000);
    expect(requests).toBe(completedRequests);
    expect(failures).toEqual([]);
  });
}
