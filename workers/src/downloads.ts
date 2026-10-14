import {
  holdComic,
  QueuePausedError,
  QUEUE_PAUSED_MESSAGE,
} from "./queue-safety";
import { notifyCompute } from "./compute-scheduler";
import { wakeRemotePolls } from "./remote-polls";
import {
  fencedEnv,
  assertComicActive,
  cancelComputeForTask,
} from "./job-state";
import { putStream } from "./storage";
import { checkUpdates } from "./updates";
import { runR2Scan } from "./scans";
import { imageResponse } from "./image-stream";
import { runWebdavScan } from "./integrations";
import { queueCompute } from "./compute";
import {
  hash,
  HttpError,
  now,
  source,
  sourceJson,
  type Env,
  type Identity,
} from "./index";
import {
  admin,
  downloader,
  all,
  one,
  run,
  canAccess,
  setting,
  reserve,
  putBytes,
  type Row,
} from "./library";
import {
  providerFor,
  providerHosts,
  loadComic,
  chapterImages,
  UA,
  type SourceChapter,
} from "./providers";
import { safeFetch } from "./security";
/** Hard cap on attempts per comic job; after this the job is dead ('error'). */
export const MAX_JOB_ATTEMPTS = 5;
export const MAX_REMOTE_POLLS = 30;
/** Chapters admitted by one source execution, not a whole-book limit. */
export const DEFAULT_MAX_CHAPTERS = 20;
// A Turso query is an external fetch. Keep directory admission well below the
// free plan's 50 external subrequests per invocation, including checkpoints.
export const SOURCE_BATCH_SIZE = 4;
/** Existing source parsers cap directories at 10,000 entries. 0 means the whole book. */
export const HARD_MAX_CHAPTERS = 10000;
export function clampMaxChapters(value: unknown) {
  const n = Math.trunc(Number(value));
  if (!Number.isFinite(n) || n < 1) return 0;
  return Math.min(HARD_MAX_CHAPTERS, n);
}
export function selectSourceChapters(
  chapters: SourceChapter[],
  maxChapters: unknown,
  isUpdate = false,
  readyIds: Iterable<string> = [],
) {
  const cap = clampMaxChapters(maxChapters),
    ready = new Set(readyIds);
  const selected = isUpdate
    ? chapters.filter((chapter) => !ready.has(chapter.id))
    : cap
      ? chapters.slice(0, cap)
      : chapters;
  return (isUpdate && cap ? selected.slice(0, cap) : selected).filter(
    (chapter) => !ready.has(chapter.id),
  );
}
/** Exponential backoff in seconds: 60, 120, 240, ... capped at 6 h. */
export function retryDelay(attempts: number) {
  return Math.min(60 * 2 ** Math.max(0, attempts - 1), 21600);
}
export async function enqueue(
  env: Env,
  userId: string,
  kind: string,
  payload: Row,
  parent: string | null = null,
  comic: string | null = null,
  deterministicId?: string,
  deliver = true,
) {
  if (kind === "webdav") {
    const known = await one(
      env,
      "SELECT id FROM comic_jobs WHERE kind='webdav' AND json_extract(payload,'$.mount_id')=? AND json_extract(payload,'$.url')=? AND state IN ('queued','running','waiting') LIMIT 1",
      payload.mount_id,
      payload.url,
    );
    if (known) return known.id;
  }
  const id = deterministicId || crypto.randomUUID();
  if (
    deterministicId &&
    (await one(env, "SELECT id FROM comic_jobs WHERE id=?", id))
  )
    return id;
  const inserted = await run(
    env,
    "INSERT INTO comic_jobs(id,user_id,comic_id,parent_id,kind,payload,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
    id,
    userId,
    comic,
    parent,
    kind,
    JSON.stringify(payload),
    now(),
    now(),
  );
  if (!inserted.meta.changes) throw new HttpError(409, "任务已停止");
  if (!parent) await notifyCompute(env);
  if (deliver)
    try {
      await env.DOWNLOADS.send({ id, kind: "comic" });
    } catch (error) {
      await holdComic(env, id, error);
    }
  return id;
}
export async function createDownload(env: Env, user: Identity, data: Row) {
  downloader(user);
  let url = String(data.url || "").trim();
  const found = url.match(/https?:\/\/[^\s]+|fanqie-comic:\/\/\d{8,24}/);
  if (found) url = found[0];
  if (url.startsWith("http://")) url = url.replace(/^http:/, "https:");
  const provider = providerFor(url);
  if (
    ["mxs", "kalamanhua"].includes(provider) &&
    !user.can_view_adult &&
    (await setting(env, "adult_content_enabled", "0")) !== "1"
  )
    throw new HttpError(403, "18+ 来源未开启");
  const group = String(data.group || data.group_name || "默认分组");
  if (
    !(await one(
      env,
      "SELECT name FROM library_groups WHERE kind='comic' AND name=?",
      group,
    ))
  )
    throw new HttpError(400, "分组不存在");
  if (
    user.role !== "admin" &&
    !(await one(
      env,
      "SELECT 1 FROM group_permissions WHERE user_id=? AND group_name=?",
      user.user_id,
      group,
    ))
  )
    throw new HttpError(403, "当前账号无权下载到此分组");
  const known = await one(env, "SELECT * FROM comics WHERE source_url=?", url);
  if (known && !(await canAccess(env, user, known)))
    throw new HttpError(403, "当前账号未被授权访问此作品");
  if (
    await one(
      env,
      "SELECT id FROM comic_jobs WHERE kind='source' AND json_extract(payload,'$.url')=? AND state IN ('queued','running','waiting')",
      url,
    )
  )
    throw new HttpError(409, "来源已有进行中的任务");
  const format = Number(data.format || data.comic_format || 2);
  if (![1, 2].includes(format)) throw new HttpError(400, "漫画格式无效");
  return enqueue(env, user.user_id, "source", {
    url,
    provider,
    group,
    format,
    is_update: !!data.is_update,
    max_chapters: clampMaxChapters(data.max_chapters),
  });
}
async function currentCreator(env: Env, job: Row) {
  const row = await one(env, "SELECT * FROM users WHERE id=?", job.user_id);
  if (!row) throw new HttpError(403, "任务创建者已移除");
  const user = { ...row, user_id: row.id } as Identity;
  downloader(user);
  if (job.parent_id) {
    const parent = await one(
      env,
      "SELECT state FROM comic_jobs WHERE id=?",
      job.parent_id,
    );
    if (!parent || ["error", "cancelled"].includes(parent.state))
      throw new HttpError(409, "主任务已停止");
  }
  return user;
}
export async function completeParent(env: Env, rootId: string) {
  const pending = await one(
    env,
    "SELECT 1 FROM comic_jobs WHERE parent_id=? AND state IN ('queued','submitting','running','waiting') LIMIT 1",
    rootId,
  );
  if (pending) return "pending";
  const failed = await one(
    env,
    "SELECT 1 FROM comic_jobs WHERE parent_id=? AND state IN ('error','cancelled') LIMIT 1",
    rootId,
  );
  const root = await one(
    env,
    "SELECT kind,state,payload FROM comic_jobs WHERE id=?",
    rootId,
  );
  if (!root || root.state !== "waiting") return "stopped";
  if (
    root.kind === "source" &&
    (await one(
      env,
      "SELECT 1 FROM compute_jobs WHERE json_extract(payload,'$.parent_job')=? AND state IN ('queued','running') LIMIT 1",
      rootId,
    ))
  )
    return "pending";
  if (!failed && root?.kind === "source" && root.state === "waiting") {
    const data = JSON.parse(root.payload);
    if (
      !data.source_planned &&
      (await one(
        env,
        "SELECT 1 FROM compute_jobs WHERE json_extract(payload,'$.parent_job')=? AND state='error' LIMIT 1",
        rootId,
      ))
    ) {
      await run(
        env,
        "UPDATE comic_jobs SET state='error',message='来源解析失败，请重试',updated_at=? WHERE id=? AND state='waiting'",
        now(),
        rootId,
      );
      return "stopped";
    }
    if (data.source_planned && data.source_cursor < data.source_total) {
      const continued = await one(
        env,
        "UPDATE comic_jobs SET state='queued',claim_token=NULL,lease_until=0,attempts=0,message='本批章节已完成，继续下载',updated_at=? WHERE id=? AND state='waiting' RETURNING id",
        now(),
        rootId,
      );
      if (continued)
        try {
          await env.DOWNLOADS.send({ id: rootId, kind: "comic" });
        } catch (error) {
          if (!(await holdComic(env, rootId, error))) throw error;
        }
      return "continued";
    }
  }
  await run(
    env,
    "UPDATE comic_jobs SET state=?,message=?,updated_at=? WHERE id=? AND state='waiting'",
    failed ? "error" : "completed",
    failed ? "部分章节失败，请重试" : "下载完成",
    now(),
    rootId,
  );
}
// Reconcile last-page callbacks as well as old half-created chapter plans.
export async function completeNativeChapter(
  env: Env,
  chapterJob: string,
  data: Row,
) {
  if (
    await one(
      env,
      "SELECT 1 FROM comic_jobs WHERE parent_id=? AND kind='page' AND state IN ('queued','submitting','running','waiting','cancelled','error') LIMIT 1",
      chapterJob,
    )
  )
    return;
  const aggregate = await one(
    env,
    "SELECT c.page_count,c.manifest_key,COUNT(p.id) AS total,SUM(CASE WHEN p.state='completed' THEN 1 ELSE 0 END) AS done,COALESCE(SUM(p.size),0) AS bytes FROM chapters c LEFT JOIN comic_jobs p ON p.parent_id=? AND p.kind='page' WHERE c.id=? GROUP BY c.id",
    chapterJob,
    data.chapter_id,
  );
  if (
    !aggregate?.page_count ||
    aggregate.total !== aggregate.page_count ||
    aggregate.done !== aggregate.page_count
  )
    return;
  await run(
    env,
    "UPDATE chapters SET state='ready',size=? WHERE id=?",
    aggregate.bytes,
    data.chapter_id,
  );
  if (data.format === 1) {
    await queueCompute(env, "pdf", aggregate.manifest_key, {
      chapter_id: data.chapter_id,
      parent_job: chapterJob,
    });
    await run(
      env,
      "UPDATE chapters SET state='converting' WHERE id=?",
      data.chapter_id,
    );
    return;
  }
  const parent = await one(
    env,
    "SELECT parent_id FROM comic_jobs WHERE id=?",
    chapterJob,
  );
  await run(
    env,
    "UPDATE comic_jobs SET state='completed',message='章节下载完成',updated_at=? WHERE id=?",
    now(),
    chapterJob,
  );
  if (parent?.parent_id) await completeParent(env, parent.parent_id);
}

export async function recoverNativeChapters(env: Env) {
  return env.DB.prepare(
    "UPDATE comic_jobs SET state='queued',lease_until=0,claim_token=NULL,updated_at=?,message='恢复未完成的图片任务创建' WHERE id IN (SELECT j.id FROM comic_jobs j INDEXED BY comic_native_waiting JOIN comic_jobs root ON root.id=j.parent_id JOIN chapters c ON c.id=json_extract(j.payload,'$.chapter_id') WHERE j.kind='chapter' AND j.state='waiting' AND j.remote_poll_at IS NULL AND j.updated_at<? AND j.lease_until<=? AND j.attempts<? AND json_extract(j.payload,'$.provider')!='fanqie' AND root.state IN ('queued','running','waiting') AND c.state IN ('downloading','ready') AND c.page_count>0 AND NOT EXISTS(SELECT 1 FROM compute_jobs x WHERE json_extract(x.payload,'$.parent_job')=j.id AND x.state IN ('queued','running')) AND ((SELECT COUNT(*) FROM comic_jobs p WHERE p.parent_id=j.id AND p.kind='page')<c.page_count OR NOT EXISTS(SELECT 1 FROM comic_jobs p WHERE p.parent_id=j.id AND p.kind='page' AND p.state!='completed')) ORDER BY j.updated_at,j.id LIMIT 4) RETURNING id,kind",
  )
    .bind(now(), now() - 900, now(), MAX_JOB_ATTEMPTS)
    .all<{ id: string; kind: string }>();
}

export async function runJob(env: Env, id: string) {
  const token = crypto.randomUUID();
  const job = await env.DB.prepare(
    "UPDATE comic_jobs SET state='running',claim_token=?,lease_until=?,remote_poll_at=CASE WHEN remote_poll_at IS NOT NULL THEN ? ELSE NULL END,attempts=attempts+1,updated_at=? WHERE id=? AND state IN ('queued','running') AND lease_until<=? AND attempts<? RETURNING *",
  )
    .bind(token, now() + 300, now() + 300, now(), id, now(), MAX_JOB_ATTEMPTS)
    .first<Row>();
  if (!job) return;
  env = fencedEnv(env, "comic_jobs", job);
  try {
    const user = await currentCreator(env, job),
      data = JSON.parse(job.payload);
    if (job.kind === "r2scan") {
      admin(user);
      await runR2Scan(env, job, data);
      await run(
        env,
        "UPDATE comic_jobs SET state='completed',lease_until=0,message='R2 目录扫描完成',updated_at=? WHERE id=?",
        now(),
        id,
      );
      return;
    }
    if (job.kind === "update-check") {
      await checkUpdates(env, user, !!data.automatic);
      await run(
        env,
        "UPDATE comic_jobs SET state='completed',lease_until=0,updated_at=? WHERE id=?",
        now(),
        id,
      );
      return;
    }
    if (job.kind === "webdav") {
      await runWebdavScan(env, job, data);
      await run(
        env,
        "UPDATE comic_jobs SET state='completed',lease_until=0,updated_at=? WHERE id=?",
        now(),
        id,
      );
      await run(
        env,
        "UPDATE remote_mounts SET state='idle',message='扫描完成' WHERE id=? AND state='running' AND NOT EXISTS (SELECT 1 FROM comic_jobs WHERE kind='webdav' AND json_extract(payload,'$.mount_id')=? AND state IN ('queued','running','waiting'))",
        data.mount_id,
        data.mount_id,
      );
      return;
    }
    if (job.kind === "source") {
      const source = data.source_key
        ? ((await (await env.LIBRARY.get(data.source_key))!.json()) as any)
        : await loadComic(data.url, env);
      if (
        source.adult &&
        !user.can_view_adult &&
        (await setting(env, "adult_content_enabled", "0")) !== "1"
      )
        throw new HttpError(403, "18+ 来源已关闭");
      const existing = await one(
          env,
          "SELECT * FROM comics WHERE name=?",
          source.title,
        ),
        comicId = existing?.id || "c_" + (await hash(data.url)).slice(0, 12);
      if (existing && !(await canAccess(env, user, existing)))
        throw new HttpError(403, "当前账号不能更新此作品");
      const group = existing?.group_name || data.group;
      if (!(await canAccess(env, user, { group_name: group })))
        throw new HttpError(403, "下载分组权限已撤销");
      await assertComicActive(env, job);
      await run(
        env,
        "INSERT INTO comics(id,name,provider,source_url,description,group_name,adult,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET description=excluded.description,updated_at=excluded.updated_at",
        comicId,
        source.title,
        source.provider,
        data.url,
        source.description,
        group,
        source.adult ? 1 : 0,
        now(),
        now(),
      );
      await run(
        env,
        "UPDATE comics SET comic_format=? WHERE id=?",
        data.format,
        comicId,
      );
      await run(
        env,
        "UPDATE comic_jobs SET comic_id=?,state='running',message=?,updated_at=? WHERE id=?",
        comicId,
        `《${source.title}》目录已解析`,
        now(),
        id,
      );
      if (source.provider === "fanqie" && !existing?.cover_key) {
        const bookId = source.chapters[0]?.book_id;
        if (bookId)
          await enqueue(
            env,
            user.user_id,
            "cover",
            {
              url: "/v1/resources/" + bookId + "/cover",
              key: `comics/${comicId}/cover/original`,
              referer: data.url,
              fanqie: true,
            },
            id,
            comicId,
          );
      }
      if (
        source.cover &&
        !existing?.cover_key &&
        !(await one(
          env,
          "SELECT 1 FROM comic_jobs WHERE parent_id=? AND kind='cover'",
          id,
        ))
      )
        await enqueue(
          env,
          user.user_id,
          "cover",
          {
            url: source.cover,
            key: `comics/${comicId}/cover/original`,
            referer: data.url,
          },
          id,
          comicId,
        );
      let planned = source;
      if (!data.source_planned) {
        const readyIds = (
          await all(
            env,
            "SELECT source_id FROM chapters WHERE comic_id=? AND state='ready'",
            comicId,
          )
        ).map((row) => String(row.source_id));
        const chosen = selectSourceChapters(
          source.chapters,
          data.max_chapters,
          !!data.is_update,
          readyIds,
        );
        planned = { ...source, chapters: chosen };
        const sourceKey = `comics/${comicId}/sources/${id}.json`;
        await putBytes(
          env,
          sourceKey,
          new TextEncoder().encode(JSON.stringify(planned)),
          "application/json",
        );
        Object.assign(data, {
          source_key: sourceKey,
          source_planned: true,
          source_cursor: 0,
          source_total: chosen.length,
        });
        await run(
          env,
          "UPDATE comic_jobs SET payload=?,updated_at=? WHERE id=?",
          JSON.stringify(data),
          now(),
          id,
        );
      }
      const cursor = Number(data.source_cursor) || 0;
      const selected = planned.chapters.slice(
        cursor,
        cursor + SOURCE_BATCH_SIZE,
      );
      for (const chapter of selected) {
        await assertComicActive(env, job);
        const chapterId =
          "ch_" + (await hash(comicId + "|" + chapter.id)).slice(0, 24);
        const local = await one(
          env,
          "SELECT state FROM chapters WHERE id=?",
          chapterId,
        );
        if (local?.state === "ready") continue;
        await env.DB.batch([
          env.DB.prepare(
            "INSERT INTO chapters(id,comic_id,ordinal,title,source_id) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET ordinal=excluded.ordinal,title=excluded.title",
          ).bind(chapterId, comicId, chapter.order, chapter.title, chapter.id),
        ]);
        await enqueue(
          env,
          user.user_id,
          "chapter",
          {
            chapter,
            chapter_id: chapterId,
            provider: source.provider,
            format: data.format,
          },
          id,
          comicId,
          `${id}:${chapterId}`,
        );
      }
      data.source_cursor = cursor + selected.length;
      await run(
        env,
        "UPDATE comic_jobs SET state='waiting',lease_until=0,payload=?,message=?,updated_at=? WHERE id=?",
        JSON.stringify(data),
        `目录进度 ${data.source_cursor}/${data.source_total}，等待本批章节完成`,
        now(),
        id,
      );
      await completeParent(env, id);
      return;
    }
    const comic = await one(
      env,
      "SELECT * FROM comics WHERE id=?",
      job.comic_id,
    );
    if (!comic || !(await canAccess(env, user, comic)))
      throw new HttpError(403, "作品权限已撤销");
    if (job.kind === "chapter" && data.provider === "fanqie") {
      await fanqieChapter(env, job, data);
      return;
    }
    if (job.kind === "chapter") {
      const images = await chapterImages(data.provider, data.chapter);
      await assertComicActive(env, job);
      const keys = images.map((url, i) => ({
        key: `comics/${comic.id}/chapters/${data.chapter_id}/${String(i).padStart(5, "0")}`,
        url,
      }));
      const manifestKey = `comics/${comic.id}/chapters/${data.chapter_id}/manifest.json`;
      await putBytes(
        env,
        manifestKey,
        new TextEncoder().encode(
          JSON.stringify(keys.map((v) => ({ key: v.key }))),
        ),
        "application/json",
      );
      await env.DB.batch([
        env.DB.prepare(
          "UPDATE chapters SET manifest_key=?,page_count=?,state='downloading' WHERE id=?",
        ).bind(manifestKey, keys.length, data.chapter_id),
      ]);
      const queuedPages = new Set(
        (
          await all(
            env,
            "SELECT json_extract(payload,'$.page') AS page FROM comic_jobs WHERE parent_id=?",
            id,
          )
        ).map((row) => Number(row.page)),
      );
      // Each batch is one Turso request, rather than one external fetch per page.
      // Persist at most 200 new rows per invocation; continuation reuses rows.
      const missing = keys
        .map((item, page) => ({ ...item, page }))
        .filter((item) => !queuedPages.has(item.page));
      const admitted = missing.slice(0, 200);
      for (let offset = 0; offset < admitted.length; offset += 50) {
        await assertComicActive(env, job);
        const results = await env.DB.batch(
          admitted.slice(offset, offset + 50).map((item) =>
            env.DB.prepare(
              "INSERT INTO comic_jobs(id,user_id,comic_id,parent_id,kind,payload,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ).bind(
              crypto.randomUUID(),
              user.user_id,
              comic.id,
              id,
              "page",
              JSON.stringify({
                ...item,
                chapter_id: data.chapter_id,
                referer: data.chapter.url || comic.source_url,
                format: data.format,
              }),
              now(),
              now(),
            ),
          ),
        );
        if (results.some((result) => !result.meta.changes))
          throw new HttpError(409, "任务已停止或执行已过期");
      }
      if (missing.length > admitted.length) {
        await run(
          env,
          "UPDATE comic_jobs SET state='queued',lease_until=0,attempts=0,message='继续创建图片任务',updated_at=? WHERE id=?",
          now(),
          id,
        );
        try {
          await env.DOWNLOADS.send({ id, kind: "comic" });
        } catch (error) {
          if (!(await holdComic(env, id, error))) throw error;
        }
        return;
      }
      // Only publish waiting after the complete image plan is durable.
      await run(
        env,
        "UPDATE comic_jobs SET state='waiting',lease_until=0,message='图片下载中',updated_at=? WHERE id=?",
        now(),
        id,
      );
      const pages = await all(
        env,
        "SELECT id FROM comic_jobs WHERE parent_id=? AND kind='page' AND state='queued' AND lease_until<?",
        id,
        now(),
      );
      const batches: MessageSendRequest<import("./index").QueueBody>[] = [];
      for (let i = 0; i < pages.length; i += 10)
        batches.push({
          body: {
            kind: "comic-pages",
            ids: pages.slice(i, i + 10).map((row) => row.id),
          },
        });
      for (let i = 0; i < batches.length; i += 100) {
        try {
          await env.DOWNLOADS.sendBatch(batches.slice(i, i + 100));
        } catch (error) {
          if (!(error instanceof QueuePausedError)) throw error;
          await holdComic(env, id, error);
          await run(
            env,
            "UPDATE comic_jobs SET message=?,lease_until=? WHERE parent_id=? AND state='queued'",
            QUEUE_PAUSED_MESSAGE,
            now() + error.retryAfter,
            id,
          );
          break;
        }
      }
      await completeNativeChapter(env, id, data);
      return;
    }
    if (["page", "cover"].includes(job.kind)) {
      const existing = await env.LIBRARY.head(data.key);
      let size = existing?.size || 0;
      if (!existing) {
        let response = data.fanqie
          ? await source(env, data.url)
          : await safeFetch(
              data.url,
              { headers: { "User-Agent": UA, Referer: data.referer } },
              providerHosts(String(comic.provider || "")),
            );
        if (!response.ok || !response.body) {
          await response.body?.cancel();
          throw new HttpError(502, `图片 HTTP ${response.status}`);
        }
        await assertComicActive(env, job);
        response = await imageResponse(response);
        const mime = response.headers.get("Content-Type")?.split(";")[0] || "";
        if (!mime.startsWith("image/")) {
          await response.body!.cancel();
          throw new HttpError(502, "来源未返回图片");
        }
        const length = Number(response.headers.get("Content-Length"));
        if (length > 0) {
          if (length > 20971520) {
            await response.body!.cancel();
            throw new HttpError(413, "图片超过 20 MB");
          }
          await putStream(env, data.key, response.body!, length, mime);
          size = length;
        } else {
          const reader = response.body!.getReader(),
            parts: Uint8Array[] = [];
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            size += value.byteLength;
            if (size > 20971520) {
              await reader.cancel();
              throw new HttpError(413, "图片超过 20 MB");
            }
            parts.push(value);
          }
          const bytes = new Uint8Array(size);
          let offset = 0;
          for (const part of parts) {
            bytes.set(part, offset);
            offset += part.byteLength;
          }
          await putBytes(env, data.key, bytes, mime);
        }
      }
      await assertComicActive(env, job);
      if (job.kind === "cover") {
        await run(
          env,
          "UPDATE comics SET cover_key=? WHERE id=?",
          data.key,
          comic.id,
        );
        await queueCompute(env, "cover", data.key, {
          kind: "comic",
          media_id: comic.id,
        });
      }
      await run(
        env,
        "UPDATE comic_jobs SET state='completed',size=?,lease_until=0,updated_at=? WHERE id=?",
        size,
        now(),
        id,
      );
      if (job.kind === "page") {
        await completeNativeChapter(env, job.parent_id, data);
      } else if (job.parent_id) await completeParent(env, job.parent_id);
      return;
    }
    throw new HttpError(400, "未知下载作业");
  } catch (error) {
    const latest = await one(
      env,
      "SELECT state,payload FROM comic_jobs WHERE id=?",
      job.id,
    );
    if (!latest || ["cancelled", "error", "completed"].includes(latest.state))
      return;
    if (error instanceof HttpError && error.status === 425) {
      await run(
        env,
        "UPDATE comic_jobs SET state='waiting',lease_until=0,message='同步已暂停' WHERE id=?",
        id,
      );
      return;
    }
    if (latest?.state === "submitting") {
      await run(
        env,
        "UPDATE comic_jobs SET state='error',lease_until=0,message='提交状态未知，请检查来源后重试' WHERE id=?",
        job.id,
      );
      if (job.parent_id)
        await run(
          env,
          "UPDATE comic_jobs SET state='error',message='来源提交状态未知' WHERE id=?",
          job.parent_id,
        );
      return;
    }
    if (
      ["source", "chapter"].includes(job.kind) &&
      JSON.parse(job.payload).provider !== "fanqie" &&
      error instanceof HttpError &&
      error.status >= 500 &&
      error.status !== 507
    ) {
      const data = JSON.parse(job.payload);
      const key = "legacy-" + job.kind + "/" + job.id;
      await queueCompute(env, "legacy-" + job.kind, key, {
        ...data,
        parent_job: job.id,
        comic_id: job.comic_id,
      });
      await run(
        env,
        "UPDATE comic_jobs SET state='waiting',lease_until=0,message='正在由云端兼容作业解析来源',updated_at=? WHERE id=?",
        now(),
        job.id,
      );
      return;
    }
    if (
      job.kind === "page" &&
      error instanceof HttpError &&
      error.status >= 500 &&
      error.status !== 507
    ) {
      const parent = await one(
        env,
        "SELECT * FROM comic_jobs WHERE id=?",
        job.parent_id,
      );
      if (parent) {
        const data = JSON.parse(parent.payload);
        await queueCompute(
          env,
          "legacy-chapter",
          "legacy-chapter/" + parent.id,
          { ...data, parent_job: parent.id, comic_id: job.comic_id },
        );
        await run(
          env,
          "UPDATE comic_jobs SET state='waiting',message='正在由云端兼容作业下载章节',lease_until=0,updated_at=? WHERE id=?",
          now(),
          parent.id,
        );
        await run(
          env,
          "UPDATE comic_jobs SET state='cancelled',message='改由云端兼容下载' WHERE parent_id=? AND state IN ('queued','submitting','running','waiting')",
          parent.id,
        );
        return;
      }
    }
    const message =
      error instanceof HttpError ? error.message : "来源或存储暂时不可用";
    const retry =
      job.attempts < MAX_JOB_ATTEMPTS &&
      !(
        error instanceof HttpError &&
        [400, 403, 409, 413, 504, 507].includes(error.status)
      );
    await run(
      env,
      "UPDATE comic_jobs SET state=?,lease_until=0,message=?,updated_at=? WHERE id=?",
      retry ? "queued" : "error",
      message,
      now(),
      id,
    );
    if (retry) {
      // Backoff is enforced in D1 via lease_until so neither the queue nor the
      // maintenance cron can claim the job again before the delay has passed.
      const delay = retryDelay(job.attempts);
      await run(
        env,
        "UPDATE comic_jobs SET lease_until=? WHERE id=? AND state='queued'",
        now() + delay,
        id,
      );
      try {
        const data = JSON.parse(latest.payload);
        if (
          job.kind === "chapter" &&
          data.provider === "fanqie" &&
          data.remote_id
        ) {
          await run(
            env,
            "UPDATE comic_jobs SET remote_poll_at=? WHERE id=? AND state='queued'",
            now() + delay,
            id,
          );
          await wakeRemotePolls(env, now() + delay);
        } else
          await env.DOWNLOADS.send(
            { id, kind: "comic" },
            { delaySeconds: delay },
          );
      } catch {}
    } else if (job.parent_id) {
      await run(
        env,
        "UPDATE comic_jobs SET state='error',message='部分下载失败',updated_at=? WHERE id=?",
        now(),
        job.parent_id,
      );
      const parent = await one(
        env,
        "SELECT parent_id FROM comic_jobs WHERE id=?",
        job.parent_id,
      );
      if (parent?.parent_id)
        await run(
          env,
          "UPDATE comic_jobs SET state='error',message='部分章节失败',updated_at=? WHERE id=?",
          now(),
          parent.parent_id,
        );
    }
  }
}
export async function tasks(
  env: Env,
  user: Identity,
  taskId?: string,
): Promise<Row[]> {
  const rows = await all(
    env,
    `SELECT j.*,c.name AS comic_name,
      CASE WHEN j.state IN ('waiting','running') THEN (
        SELECT MAX(phase) FROM (
          SELECT CASE WHEN q.state='running' AND q.lease_until>? THEN 2 ELSE 1 END AS phase
          FROM compute_jobs q WHERE json_extract(q.payload,'$.parent_job')=j.id AND q.state IN ('queued','running')
          UNION ALL
          SELECT CASE WHEN q.state='running' AND q.lease_until>? THEN 2 ELSE 1 END AS phase
          FROM comic_jobs child JOIN compute_jobs q ON json_extract(q.payload,'$.parent_job')=child.id
          WHERE child.parent_id=j.id AND child.state IN ('waiting','running') AND q.state IN ('queued','running')
        )
      ) ELSE NULL END AS cloud_phase,
      EXISTS(SELECT 1 FROM comic_jobs held WHERE held.parent_id=j.id AND held.message='队列免费预算已暂停，等待重置后自动恢复' AND held.state IN ('queued','waiting')) AS queue_held,
      CASE WHEN j.state IN ('waiting','running') THEN EXISTS(
        SELECT 1 FROM comic_jobs child WHERE child.parent_id=j.id AND child.state IN ('running','submitting') AND child.lease_until>?
      ) OR EXISTS (
        SELECT 1 FROM comic_jobs child JOIN comic_jobs leaf ON leaf.parent_id=child.id
        WHERE child.parent_id=j.id AND child.state IN ('waiting','running')
        AND leaf.state IN ('running','submitting') AND leaf.lease_until>?
      ) ELSE 0 END AS native_running
      FROM comic_jobs j LEFT JOIN comics c ON c.id=j.comic_id
      WHERE j.kind='source' AND (?='admin' OR j.user_id=?) ${taskId ? "AND j.id=?" : ""}
      ORDER BY j.created_at DESC LIMIT 100`,
    now(),
    now(),
    now(),
    now(),
    user.role,
    user.user_id,
    ...(taskId ? [taskId] : []),
  );
  const novels = await all(
    env,
    `SELECT j.*,b.title AS comic_name FROM jobs j LEFT JOIN books b ON b.id=j.source_id
      WHERE (?='admin' OR j.user_id=?) ${taskId ? "AND j.id=?" : ""} ORDER BY j.created_at DESC LIMIT 100`,
    user.role,
    user.user_id,
    ...(taskId ? [taskId] : []),
  );
  for (const row of rows) {
    const payload = JSON.parse(row.payload);
    if (payload.source_planned) row.total_chapters = payload.source_total;
    row.progress_percent = row.total_chapters
      ? Math.round((row.completed_chapters / row.total_chapters) * 100)
      : row.state === "completed"
        ? 100
        : 0;
    row.is_update = payload.is_update || false;
  }
  return [
    ...rows,
    ...novels.map((row): Row => ({
      ...row,
      comic_name: row.comic_name || `番茄小说 ${row.source_id}`,
      is_novel: true,
      total_chapters: 1,
      completed_chapters: row.state === "completed" ? 1 : 0,
      progress_percent: row.state === "completed" ? 100 : 0,
    })),
  ]
    .sort((a, b) => b.created_at - a.created_at)
    .map((row): Row => {
      const cloudRunning = row.cloud_phase === 2;
      const status =
        row.state === "waiting"
          ? cloudRunning || row.native_running
            ? "running"
            : "waiting"
          : row.state === "queued" ||
              (row.state === "running" &&
                !(
                  cloudRunning ||
                  row.native_running ||
                  Number(row.lease_until) > now()
                ))
            ? "pending"
            : row.state === "submitting"
              ? "running"
              : row.state;
      const paused = row.state === "waiting" && row.message === "同步已暂停";
      const queueHeld =
        (row.message === QUEUE_PAUSED_MESSAGE ||
          row.queue_held ||
          Number(env.QUEUE_SEND_PAUSED_UNTIL || 0) > now()) &&
        ["waiting", "pending"].includes(status);
      const statusLabel = queueHeld
        ? "等待队列额度重置"
        : paused
          ? "已暂停"
          : row.state === "waiting" &&
              row.cloud_phase === 1 &&
              !row.native_running
            ? "等待云执行器"
            : cloudRunning
              ? "云端处理中"
              : (
                  {
                    pending:
                      row.state === "running" ? "等待任务恢复" : "等待启动",
                    waiting: "等待章节执行",
                    running: "下载中",
                    completed: "已完成",
                    cancelled: "已取消",
                    error: "出错",
                  } as Record<string, string>
                )[status] || status;
      const logs = row.message ? [String(row.message)] : [];
      if (!logs.length) logs.push(statusLabel);
      if (statusLabel === "等待云执行器")
        logs.push(
          "任务已排队，云执行器尚未开始；定时调度可能延迟，请勿重复提交。",
        );
      return {
        ...row,
        status,
        status_label: statusLabel,
        start_time: row.started_at
          ? new Date(row.started_at * 1000).toISOString()
          : null,
        end_time: row.finished_at
          ? new Date(row.finished_at * 1000).toISOString()
          : null,
        log: logs,
      };
    });
}
export async function taskAction(
  env: Env,
  user: Identity,
  id: string,
  action: string,
) {
  const task = (await tasks(env, user, id)).find((row) => row.id === id);
  if (!task) throw new HttpError(404, "任务不存在或无权访问");
  const table = task.is_novel ? "jobs" : "comic_jobs";
  if (action === "cancel") {
    if (["completed", "error"].includes(task.state))
      throw new HttpError(409, "任务已经结束，不能取消");
    const upstream = task.is_novel
      ? [task.remote_id].filter(Boolean)
      : (
          await all(
            env,
            "SELECT payload FROM comic_jobs WHERE (id=? OR parent_id=? OR parent_id IN (SELECT id FROM comic_jobs WHERE parent_id=?)) AND state IN ('queued','submitting','running','waiting')",
            id,
            id,
            id,
          )
        )
          .map((row) => JSON.parse(row.payload).remote_id)
          .filter(Boolean);
    await run(
      env,
      `UPDATE ${table} SET state='cancelled',message='用户取消任务'${table === "jobs" ? ",reserved_bytes=0" : ""} WHERE id=? AND state IN ('queued','submitting','running','waiting')`,
      id,
    );
    if (table === "comic_jobs") await cancelComputeForTask(env, id);
    if (table === "comic_jobs")
      await run(
        env,
        "UPDATE comic_jobs SET state='cancelled' WHERE (parent_id=? OR parent_id IN (SELECT id FROM comic_jobs WHERE parent_id=?)) AND state IN ('queued','submitting','running','waiting')",
        id,
        id,
      );
    for (const remoteId of new Set(upstream)) {
      try {
        await sourceJson(
          env,
          `/v1/jobs/${encodeURIComponent(String(remoteId))}`,
          { method: "DELETE" },
        );
      } catch {
        await run(
          env,
          `UPDATE ${table} SET message='本地任务已取消；来源服务暂不可达，远端任务可能仍在结束中' WHERE id=?`,
          id,
        );
      }
    }
  } else {
    if (!["completed", "cancelled", "error"].includes(task.status))
      throw new HttpError(409, "只能删除已结束的任务");
    if (table === "comic_jobs")
      await env.DB.batch([
        env.DB.prepare(
          "DELETE FROM comic_jobs WHERE parent_id IN (SELECT id FROM comic_jobs WHERE parent_id=?)",
        ).bind(id),
        env.DB.prepare("DELETE FROM comic_jobs WHERE parent_id=?").bind(id),
      ]);
    await run(env, `DELETE FROM ${table} WHERE id=?`, id);
  }
}

async function fanqieChapter(env: Env, job: Row, data: Row) {
  if (
    now() - job.created_at > 7200 ||
    Number(data.polls || 0) >= MAX_REMOTE_POLLS
  )
    throw new HttpError(504, "番茄漫画任务超时，已停止轮询");
  const remoteId = data.remote_id;
  if (!remoteId) {
    await run(
      env,
      "UPDATE comic_jobs SET state='submitting',payload=?,updated_at=? WHERE id=?",
      JSON.stringify(data),
      now(),
      job.id,
    );
    const created = await sourceJson(env, "/v1/jobs", {
      method: "POST",
      body: JSON.stringify({
        target: data.chapter.book_id,
        kind: "comic",
        format: data.format === 1 ? "pdf" : "cbz",
        chapter_ids: [data.chapter.id],
      }),
    });
    if (
      typeof created.id !== "string" ||
      !/^[a-zA-Z0-9_-]{1,100}$/.test(created.id)
    )
      throw new HttpError(502, "番茄漫画任务 ID 无效");
    data.remote_id = created.id;
    await run(
      env,
      "UPDATE comic_jobs SET state='running',payload=?,updated_at=? WHERE id=?",
      JSON.stringify(data),
      now(),
      job.id,
    );
  }
  const remote = await sourceJson(env, `/v1/jobs/${data.remote_id}`);
  if (["queued", "running"].includes(remote.status)) {
    data.polls = Number(data.polls || 0) + 1;
    if (data.polls >= MAX_REMOTE_POLLS)
      throw new HttpError(504, "番茄漫画轮询次数已用尽");
    const delay = Math.min(retryDelay(data.polls), 300);
    await run(
      env,
      "UPDATE comic_jobs SET state='queued',attempts=MAX(0,attempts-1),payload=?,lease_until=?,remote_poll_at=?,updated_at=?,message='番茄服务生成章节中' WHERE id=?",
      JSON.stringify(data),
      now() + delay,
      now() + delay,
      now(),
      job.id,
    );
    await wakeRemotePolls(env, now() + delay);
    return;
  }
  await assertComicActive(env, job);
  const artifact = remote.artifact;
  if (
    remote.status !== "completed" ||
    !artifact ||
    !Number.isSafeInteger(artifact.size) ||
    artifact.size <= 0 ||
    artifact.size > 2147483648 ||
    !/^[a-f0-9]{64}$/i.test(artifact.sha256) ||
    artifact.url !== `/v1/jobs/${data.remote_id}/artifact`
  )
    throw new HttpError(502, "番茄漫画产物无效");
  const key = `fanqie/${data.chapter.book_id}/${artifact.sha256}.zip`;
  if (!(await env.LIBRARY.head(key))) {
    const response = await source(env, artifact.url);
    if (!response.body) throw new HttpError(502, "产物为空");
    await putStream(
      env,
      key,
      response.body,
      artifact.size,
      "application/zip",
      artifact.sha256,
    );
  }
  await assertComicActive(env, job);
  await queueCompute(env, "fanqie-archive", key, {
    chapter_id: data.chapter_id,
    source_id: data.chapter.id,
    book_id: data.chapter.book_id,
    format: data.format === 1 ? "pdf" : "cbz",
    parent_job: job.id,
  });
  await run(
    env,
    "UPDATE comic_jobs SET state='waiting',lease_until=0,message='云端正在导入章节' WHERE id=?",
    job.id,
  );
}
