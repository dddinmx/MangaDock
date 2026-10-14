import { HttpError, now, type Env } from "./index";
import { one, all, type Row } from "./library";
import { notifyCompute } from "./compute-scheduler";
import { QueuePausedError } from "./queue-safety";
import { deferDownloads } from "./download-recovery";

// One-time repair of the old whole-chapter fallback. Never reset attempts,
// revive user cancellations, touch completed pages, or create a new source task.
export async function repairNativeFallback(env: Env, id: string) {
  const root = await one(
    env,
    "SELECT j.*,c.id AS existing_comic FROM comic_jobs j LEFT JOIN comics c ON c.id=j.comic_id WHERE j.id=?",
    id,
  );
  if (!root || !root.existing_comic)
    throw new HttpError(404, "原任务或漫画不存在");
  const data = JSON.parse(root.payload);
  if (data.native_fallback_repaired)
    return { repaired: false, already_repaired: true };
  if (root.kind !== "source" || root.state !== "error" || !data.source_planned)
    throw new HttpError(409, "只能修复旧版兼容下载中断的来源任务");
  const children = await all(
    env,
    "SELECT j.*,c.page_count,c.format AS chapter_format FROM comic_jobs j JOIN chapters c ON c.id=json_extract(j.payload,'$.chapter_id') WHERE j.parent_id=? AND j.kind='chapter' AND j.state!='completed' LIMIT 5",
    id,
  );
  if (!children.length || children.length > 4)
    throw new HttpError(409, "未完成章节超出修复范围");
  const pages: Row[] = [];
  for (const child of children) {
    if (
      child.state !== "error" ||
      child.chapter_format !== "images" ||
      JSON.parse(child.payload).provider === "fanqie"
    )
      throw new HttpError(409, "任务不属于旧版原生图片兼容回退");
    const plan = await all(
      env,
      "SELECT id,state,attempts,message FROM comic_jobs WHERE parent_id=? AND kind='page' LIMIT 401",
      child.id,
    );
    const compatible = await one(
      env,
      "SELECT 1 FROM compute_jobs WHERE kind='legacy-chapter' AND json_extract(payload,'$.parent_job')=? AND state='error' LIMIT 1",
      child.id,
    );
    const active = await one(
      env,
      "SELECT 1 FROM compute_jobs WHERE json_extract(payload,'$.parent_job')=? AND state IN ('queued','running') LIMIT 1",
      child.id,
    );
    if (
      !compatible ||
      active ||
      plan.length !== child.page_count ||
      !plan.length ||
      plan.some(
        (p) =>
          p.state !== "completed" &&
          (p.state !== "cancelled" ||
            p.message !== "改由云端兼容下载" ||
            p.attempts >= 5),
      )
    )
      throw new HttpError(409, "图片计划不完整或存在用户取消，未应用修复");
    pages.push(...plan.filter((p) => p.state === "cancelled"));
  }
  if (!pages.length || pages.length > 400)
    throw new HttpError(409, "补下载图片超出单次修复范围");
  const marker = crypto.randomUUID();
  const guard =
    "EXISTS(SELECT 1 FROM comic_jobs r WHERE r.id=? AND r.state='waiting' AND json_extract(r.payload,'$.native_fallback_repaired')=?)";
  const statements = [
    env.DB.prepare(
      "UPDATE comic_jobs SET state='waiting',message='保留已完成图片，补下载旧版回退取消的图片',payload=json_set(payload,'$.native_fallback_repaired',?),updated_at=? WHERE id=? AND state='error' AND json_extract(payload,'$.native_fallback_repaired') IS NULL",
    ).bind(marker, now(), id),
  ];
  for (const child of children) {
    statements.push(
      env.DB.prepare(
        `UPDATE comic_jobs SET state='waiting',claim_token=NULL,lease_until=0,updated_at=? WHERE id=? AND state='error' AND ${guard}`,
      ).bind(now(), child.id, id, marker),
    );
    statements.push(
      env.DB.prepare(
        `UPDATE comic_jobs SET state='queued',claim_token=NULL,lease_until=0,updated_at=0,message='补下载旧版回退取消的图片' WHERE parent_id=? AND kind='page' AND state='cancelled' AND message='改由云端兼容下载' AND attempts<5 AND ${guard}`,
      ).bind(child.id, id, marker),
    );
  }
  const result = await env.DB.batch(statements);
  if (!result[0].meta.changes)
    throw new HttpError(409, "任务状态已改变，未应用修复");
  await notifyCompute(env);
  const messages = [];
  for (let i = 0; i < pages.length; i += 10)
    messages.push({
      body: {
        kind: "comic-pages" as const,
        ids: pages.slice(i, i + 10).map((p) => p.id),
      },
    });
  try {
    await env.DOWNLOADS.sendBatch(messages);
  } catch (error) {
    if (!(error instanceof QueuePausedError)) throw error;
    for (let i = 0; i < pages.length; i += 100)
      await deferDownloads(
        env,
        pages.slice(i, i + 100).map((p) => p.id),
        error.retryAfter,
      );
  }
  return {
    repaired: true,
    task_id: id,
    chapters: children.length,
    pages: pages.length,
    preserved_chapters: root.completed_chapters,
  };
}
