import { HttpError, now, type Env } from "./index";
import { all, one, type Row } from "./library";
import { PAGE_MESSAGE_SIZE } from "./downloads";
import { deferDownloads } from "./download-recovery";
import { QueuePausedError } from "./queue-safety";

// Explicit operator repair of the old fatal reservation-conflict bug. This is
// not automatic retry-budget reset, and never revives a user cancellation.
export async function repairNativeStorage(env: Env, id: string) {
  const root = await one(
    env,
    "SELECT j.*,c.id AS existing_comic FROM comic_jobs j JOIN comics c ON c.id=j.comic_id WHERE j.id=?",
    id,
  );
  if (!root) throw new HttpError(404, "原任务或漫画不存在");
  const data = JSON.parse(root.payload);
  if (data.native_storage_repaired)
    return { repaired: false, already_repaired: true };
  if (
    root.kind !== "source" ||
    root.state !== "error" ||
    !data.source_planned ||
    data.provider === "fanqie"
  )
    throw new HttpError(409, "只能修复存储占位冲突中断的原生漫画任务");
  if (
    await one(
      env,
      "SELECT 1 FROM comic_jobs WHERE comic_id=? AND kind='source' AND id!=? AND state IN ('queued','running','waiting') LIMIT 1",
      root.comic_id,
      id,
    )
  )
    throw new HttpError(409, "漫画已有进行中的任务");
  const children = await all(
    env,
    "SELECT j.*,c.page_count,c.format AS chapter_format,c.manifest_key FROM comic_jobs j JOIN chapters c ON c.id=json_extract(j.payload,'$.chapter_id') WHERE j.parent_id=? AND j.kind='chapter' AND j.state!='completed' LIMIT 5",
    id,
  );
  if (!children.length || children.length > 4)
    throw new HttpError(409, "未完成章节超出修复范围");
  const recoverable = new Set([
    "文件正在上传，请稍后重试",
    "主任务已停止",
    "任务已停止或执行已过期",
  ]);
  const pages: Row[] = [];
  let conflict = false;
  for (const child of children) {
    const plan = await all(
      env,
      "SELECT id,state,message,attempts FROM comic_jobs WHERE parent_id=? AND kind='page' LIMIT 401",
      child.id,
    );
    const manifest = await env.LIBRARY.get(child.manifest_key);
    const keys = manifest ? await manifest.json<unknown[]>() : [];
    if (
      child.state !== "error" ||
      child.chapter_format !== "images" ||
      !plan.length ||
      plan.length !== child.page_count ||
      keys.length !== child.page_count ||
      plan.some(
        (p) =>
          p.state !== "completed" &&
          (p.state !== "error" ||
            p.attempts > 1 ||
            !recoverable.has(p.message)),
      )
    )
      throw new HttpError(409, "图片计划或错误原因不符合存储修复条件");
    if (
      await one(
        env,
        "SELECT 1 FROM compute_jobs WHERE state IN ('queued','running') AND json_extract(payload,'$.parent_job') IN (SELECT id FROM comic_jobs WHERE id=? OR parent_id=?) LIMIT 1",
        child.id,
        child.id,
      )
    )
      throw new HttpError(409, "章节仍有云端作业，未应用修复");
    conflict ||= plan.some((p) => p.message === "文件正在上传，请稍后重试");
    pages.push(...plan.filter((p) => p.state === "error"));
  }
  if (!conflict || !pages.length || pages.length > 800)
    throw new HttpError(409, "不属于旧版上传占位冲突");
  const marker = crypto.randomUUID(),
    guard =
      "EXISTS(SELECT 1 FROM comic_jobs r WHERE r.id=? AND r.state='waiting' AND json_extract(r.payload,'$.native_storage_repaired')=?)";
  const statements = [
    env.DB.prepare(
      "UPDATE comic_jobs SET state='waiting',claim_token=NULL,lease_until=0,message='保留已完成内容，恢复上传占位冲突中断的任务',payload=json_set(payload,'$.native_storage_repaired',?),updated_at=? WHERE id=? AND state='error' AND json_extract(payload,'$.native_storage_repaired') IS NULL",
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
        `UPDATE comic_jobs SET state='queued',attempts=0,claim_token=NULL,lease_until=0,message='恢复存储占位冲突中断的图片',updated_at=0 WHERE parent_id=? AND kind='page' AND state='error' AND attempts<=1 AND message IN ('文件正在上传，请稍后重试','主任务已停止','任务已停止或执行已过期') AND ${guard}`,
      ).bind(child.id, id, marker),
    );
  }
  const result = await env.DB.batch(statements);
  if (!result[0].meta.changes)
    throw new HttpError(409, "任务状态已改变，未应用修复");
  const messages = [];
  for (let i = 0; i < pages.length; i += PAGE_MESSAGE_SIZE)
    messages.push({
      body: {
        kind: "comic-pages" as const,
        ids: pages.slice(i, i + PAGE_MESSAGE_SIZE).map((p) => p.id),
      },
    });
  try {
    await env.DOWNLOADS.sendBatch(messages);
  } catch (error) {
    for (let i = 0; i < pages.length; i += 100)
      await deferDownloads(
        env,
        pages.slice(i, i + 100).map((p) => p.id),
        error instanceof QueuePausedError ? error.retryAfter : 60,
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
