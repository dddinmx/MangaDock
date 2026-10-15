import { HttpError, hash, now, type Env } from "./index";
import { all, one, putBytes, type Row } from "./library";
import { loadComic } from "./providers";
import { inspectChapterImages } from "./comic-integrity";
import { deferDownloads } from "./download-recovery";
import { QueuePausedError } from "./queue-safety";

// Explicit, bounded repair of legacy ready metadata. Reuse the source task,
// preserve its old plan and every existing object, and never revive old roots.
export async function repairComicIntegrity(env: Env, id: string, input: Row) {
  const ids = input.chapter_ids;
  if (
    !Array.isArray(ids) ||
    !ids.length ||
    ids.length > 8 ||
    ids.some(
      (value) => typeof value !== "string" || !/^ch_[a-f0-9]{24}$/.test(value),
    ) ||
    new Set(ids).size !== ids.length
  )
    throw new HttpError(400, "需要一至八个不重复的现有章节编号");
  const root = await one(
    env,
    "SELECT * FROM comic_jobs WHERE id=? AND kind='source'",
    id,
  );
  if (!root || root.comic_id !== input.comic_id)
    throw new HttpError(404, "原任务与漫画不匹配");
  const data = JSON.parse(root.payload);
  if (data.integrity_repair_v1) {
    if (
      JSON.stringify([...ids].sort()) !==
      JSON.stringify([...data.integrity_repair_v1.chapter_ids].sort())
    )
      throw new HttpError(409, "原任务已应用不同的完整性修复");
    return { repaired: false, already_repaired: true, task_id: id };
  }
  const comic = await one(
    env,
    "SELECT * FROM comics WHERE id=?",
    root.comic_id,
  );
  if (
    !comic ||
    comic.provider === "fanqie" ||
    root.state !== "completed" ||
    !data.source_planned ||
    data.source_cursor !== data.source_total ||
    root.completed_chapters !== data.source_total
  )
    throw new HttpError(409, "只能补齐已完成原生任务跳过的历史缺图章节");
  if (
    await one(
      env,
      "SELECT 1 FROM comic_jobs WHERE comic_id=? AND kind='source' AND state IN ('queued','running','waiting') LIMIT 1",
      comic.id,
    )
  )
    throw new HttpError(409, "漫画已有进行中的下载");
  const chapters = await all(
    env,
    `SELECT id,state,source_id,manifest_key,page_count,format FROM chapters WHERE comic_id=? AND id IN (${ids.map(() => "?").join(",")})`,
    comic.id,
    ...ids,
  );
  if (
    chapters.length !== ids.length ||
    chapters.some(
      (chapter) => chapter.state !== "ready" || chapter.format !== "images",
    )
  )
    throw new HttpError(409, "章节不属于本漫画或不是历史就绪图片章节");
  let missingImages = 0;
  for (const chapter of chapters) {
    if (
      await one(
        env,
        "SELECT 1 FROM comic_jobs WHERE id=?",
        `${id}:${chapter.id}`,
      )
    )
      throw new HttpError(409, "章节已在原任务计划内，未重复创建");
    const inventory = await inspectChapterImages(env, chapter);
    if (!inventory.missing.length || inventory.invalid.length)
      throw new HttpError(409, "章节不属于仅缺失图片的修复范围");
    missingImages += inventory.missing.length;
  }
  const oldObject = data.source_key
    ? await env.LIBRARY.get(data.source_key)
    : null;
  const oldPlan = oldObject ? await oldObject.json<any>() : null;
  if (
    !oldPlan ||
    !Array.isArray(oldPlan.chapters) ||
    oldPlan.chapters.length !== data.source_total
  )
    throw new HttpError(409, "原目录计划不完整，未应用修复");
  const source = await loadComic(comic.source_url, env);
  if (source.provider !== comic.provider)
    throw new HttpError(409, "漫画来源已改变");
  const selected = chapters.map((chapter) =>
    source.chapters.find(
      (item: any) => String(item.id) === String(chapter.source_id),
    ),
  );
  if (
    selected.some((chapter) => !chapter) ||
    selected.some((chapter) =>
      oldPlan.chapters.some(
        (old: any) => String(old.id) === String(chapter!.id),
      ),
    )
  )
    throw new HttpError(409, "缺图章节不在当前来源目录或已在原计划中");
  const marker = (await hash(JSON.stringify([...ids].sort()))).slice(0, 24);
  const key = `comics/${comic.id}/sources/${id}-integrity-${marker}.json`;
  const plan = { ...oldPlan, chapters: [...oldPlan.chapters, ...selected] };
  await putBytes(
    env,
    key,
    new TextEncoder().encode(JSON.stringify(plan)),
    "application/json",
  );
  const payload = {
    ...data,
    source_key: key,
    source_total: plan.chapters.length,
    integrity_repair_v1: {
      marker,
      chapter_ids: ids,
      missing_images: missingImages,
      original_source_key: data.source_key,
    },
  };
  const guard =
    "EXISTS(SELECT 1 FROM comic_jobs r WHERE r.id=? AND r.state='queued' AND json_extract(r.payload,'$.integrity_repair_v1.marker')=?)";
  const unchanged = chapters
    .map(
      () =>
        "EXISTS(SELECT 1 FROM chapters WHERE id=? AND comic_id=? AND state='ready' AND manifest_key=?)",
    )
    .join(" AND ");
  const result = await env.DB.batch([
    env.DB.prepare(
      `UPDATE comic_jobs SET state='queued',claim_token=NULL,lease_until=0,payload=?,message='保留已下载图片，补齐历史缺图章节',updated_at=? WHERE id=? AND state='completed' AND payload=? AND EXISTS(SELECT 1 FROM comics WHERE id=?) AND NOT EXISTS(SELECT 1 FROM comic_jobs other WHERE other.comic_id=? AND other.id!=? AND other.kind='source' AND other.state IN ('queued','running','waiting')) AND ${unchanged}`,
    ).bind(
      JSON.stringify(payload),
      now(),
      id,
      root.payload,
      comic.id,
      comic.id,
      id,
      ...chapters.flatMap((chapter) => [
        chapter.id,
        comic.id,
        chapter.manifest_key,
      ]),
    ),
    ...chapters.map((chapter) =>
      env.DB.prepare(
        `UPDATE chapters SET state='downloading' WHERE id=? AND comic_id=? AND manifest_key=? AND ${guard}`,
      ).bind(chapter.id, comic.id, chapter.manifest_key, id, marker),
    ),
  ]);
  if (!result[0].meta.changes)
    throw new HttpError(409, "任务已改变，未恢复下载");
  try {
    await env.DOWNLOADS.send({ id, kind: "comic" });
  } catch (error) {
    await deferDownloads(
      env,
      [id],
      error instanceof QueuePausedError ? error.retryAfter : 60,
    );
  }
  return {
    repaired: true,
    task_id: id,
    chapters: chapters.length,
    missing_images: missingImages,
    preserved_chapters: root.completed_chapters,
  };
}
