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
  loadComic,
  chapterImages,
  UA,
  type SourceChapter,
} from "./providers";
import { remote } from "./security";
/** Hard cap on attempts per comic job; after this the job is dead ('error'). */
export const MAX_JOB_ATTEMPTS = 5;
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
) {
  const id = crypto.randomUUID();
  await run(
    env,
    "INSERT INTO comic_jobs(id,user_id,comic_id,parent_id,kind,payload,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
    id,
    userId,
    comic,
    parent,
    kind,
    JSON.stringify(payload),
    now(),
    now(),
  );
  try {
    await env.DOWNLOADS.send({ id, kind: "comic" });
  } catch {}
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
    max_chapters: Math.max(0, Math.min(10000, Number(data.max_chapters) || 0)),
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
async function completeParent(env: Env, rootId: string) {
  const counts = await one(
    env,
    "SELECT COALESCE(SUM(state NOT IN ('completed','cancelled','error')),0) AS n,COALESCE(SUM(state='error'),0) AS failed FROM comic_jobs WHERE parent_id=?",
    rootId,
  );
  const failed = !!counts?.failed;
  if (!counts?.n)
    await run(
      env,
      "UPDATE comic_jobs SET state=?,message=?,updated_at=? WHERE id=? AND state='waiting'",
      failed ? "error" : "completed",
      failed ? "部分章节失败，请重试" : "下载完成",
      now(),
      rootId,
    );
}
export async function runJob(env: Env, id: string) {
  const job = await env.DB.prepare(
    "UPDATE comic_jobs SET state='running',lease_until=?,attempts=attempts+1,updated_at=? WHERE id=? AND state IN ('queued','running') AND lease_until<? AND attempts<? RETURNING *",
  )
    .bind(now() + 300, now(), id, now(), MAX_JOB_ATTEMPTS)
    .first<Row>();
  if (!job) return;
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
        "UPDATE comic_jobs SET comic_id=?,state='running',message=?,lease_until=0,updated_at=? WHERE id=?",
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
      const queuedChapters = new Set(
        (
          await all(
            env,
            "SELECT json_extract(payload,'$.chapter_id') AS chapter_id FROM comic_jobs WHERE parent_id=? AND kind='chapter'",
            id,
          )
        ).map((row) => row.chapter_id),
      );
      const selected = data.max_chapters
        ? source.chapters.slice(0, data.max_chapters)
        : source.chapters;
      for (const chapter of selected) {
        const chapterId =
          "ch_" + (await hash(comicId + "|" + chapter.id)).slice(0, 24);
        const local = await one(
          env,
          "SELECT state FROM chapters WHERE id=?",
          chapterId,
        );
        if (local?.state === "ready") continue;
        await run(
          env,
          "INSERT INTO chapters(id,comic_id,ordinal,title,source_id) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET ordinal=excluded.ordinal,title=excluded.title",
          chapterId,
          comicId,
          chapter.order,
          chapter.title,
          chapter.id,
        );
        if (!queuedChapters.has(chapterId))
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
          );
      }
      await run(
        env,
        "UPDATE comic_jobs SET state='waiting',lease_until=0 WHERE id=?",
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
      await run(
        env,
        "UPDATE chapters SET manifest_key=?,page_count=?,state='downloading' WHERE id=?",
        manifestKey,
        keys.length,
        data.chapter_id,
      );
      await run(
        env,
        "UPDATE comic_jobs SET state='waiting',lease_until=0,message='图片下载中' WHERE id=?",
        id,
      );
      const queuedPages = new Set(
        (
          await all(
            env,
            "SELECT json_extract(payload,'$.page') AS page FROM comic_jobs WHERE parent_id=?",
            id,
          )
        ).map((row) => Number(row.page)),
      );
      for (const [index, item] of keys.entries())
        if (!queuedPages.has(index))
          await enqueue(
            env,
            user.user_id,
            "page",
            {
              ...item,
              page: index,
              chapter_id: data.chapter_id,
              referer: data.chapter.url || comic.source_url,
              format: data.format,
            },
            id,
            comic.id,
          );
      return;
    }
    if (["page", "cover"].includes(job.kind)) {
      const existing = await env.LIBRARY.head(data.key);
      let size = existing?.size || 0;
      if (!existing) {
        let response = data.fanqie
          ? await source(env, data.url)
          : await remote(data.url, {
              headers: { "User-Agent": UA, Referer: data.referer },
            });
        if (!response.ok || !response.body) {
          await response.body?.cancel();
          throw new HttpError(502, `图片 HTTP ${response.status}`);
        }
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
          await reserve(env, data.key, length);
          const fixed = new FixedLengthStream(length),
            transfer = response.body!.pipeTo(fixed.writable);
          const results = await Promise.allSettled([
            transfer,
            env.LIBRARY.put(data.key, fixed.readable, {
              httpMetadata: { contentType: mime },
            }),
          ]);
          if (results.some((r) => r.status === "rejected"))
            throw new HttpError(502, "图片传输长度不一致");
          size = length;
          await run(
            env,
            "UPDATE storage_ledger SET state='stored' WHERE key=?",
            data.key,
          );
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
        const unfinished = await one(
          env,
          "SELECT 1 FROM comic_jobs WHERE parent_id=? AND state!='completed' LIMIT 1",
          job.parent_id,
        );
        if (!unfinished) {
          const aggregate = await one(
            env,
            "SELECT SUM(size) AS bytes FROM comic_jobs WHERE parent_id=?",
            job.parent_id,
          );
          await run(
            env,
            "UPDATE chapters SET state='ready',size=? WHERE id=?",
            aggregate?.bytes || 0,
            data.chapter_id,
          );
          if (data.format === 1) {
            const chapter = await one(
              env,
              "SELECT manifest_key FROM chapters WHERE id=?",
              data.chapter_id,
            );
            await queueCompute(env, "pdf", chapter!.manifest_key, {
              chapter_id: data.chapter_id,
              parent_job: job.parent_id,
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
            "SELECT parent_id,payload FROM comic_jobs WHERE id=?",
            job.parent_id,
          );
          await run(
            env,
            "UPDATE comic_jobs SET state='completed',message='章节下载完成',updated_at=? WHERE id=?",
            now(),
            job.parent_id,
          );
          if (parent?.parent_id) await completeParent(env, parent.parent_id);
        }
      } else if (job.parent_id) await completeParent(env, job.parent_id);
      return;
    }
    throw new HttpError(400, "未知下载作业");
  } catch (error) {
    const latest = await one(
      env,
      "SELECT state FROM comic_jobs WHERE id=?",
      job.id,
    );
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
          "UPDATE comic_jobs SET state='cancelled',message='改由云端兼容下载' WHERE parent_id=?",
          parent.id,
        );
        await run(
          env,
          "UPDATE comic_jobs SET state='waiting',message='正在由云端兼容作业下载章节',lease_until=0,updated_at=? WHERE id=?",
          now(),
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
        [400, 403, 409, 413, 507].includes(error.status)
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
export async function tasks(env: Env, user: Identity): Promise<Row[]> {
  const rows = await all(
    env,
    "SELECT j.*,c.name AS comic_name FROM comic_jobs j LEFT JOIN comics c ON c.id=j.comic_id WHERE j.kind='source' AND (?='admin' OR j.user_id=?) ORDER BY j.created_at DESC LIMIT 100",
    user.role,
    user.user_id,
  );
  const novels = await all(
    env,
    "SELECT j.*,b.title AS comic_name FROM jobs j LEFT JOIN books b ON b.id=j.source_id WHERE (?='admin' OR j.user_id=?) ORDER BY j.created_at DESC LIMIT 100",
    user.role,
    user.user_id,
  );
  for (const row of rows) {
    const counts = await one(
      env,
      "SELECT COUNT(*) AS total,SUM(state='completed') AS done FROM comic_jobs WHERE parent_id=? AND kind='chapter'",
      row.id,
    );
    row.total_chapters = counts?.total || 0;
    row.completed_chapters = counts?.done || 0;
    row.progress_percent = row.total_chapters
      ? Math.round((row.completed_chapters / row.total_chapters) * 100)
      : row.state === "completed"
        ? 100
        : 0;
    row.is_update = JSON.parse(row.payload).is_update || false;
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
    .map((row): Row => ({
      ...row,
      status:
        row.state === "waiting"
          ? "running"
          : row.state === "queued"
            ? "pending"
            : row.state,
      log: row.message,
    }));
}
export async function taskAction(
  env: Env,
  user: Identity,
  id: string,
  action: string,
) {
  const task = (await tasks(env, user)).find((row) => row.id === id);
  if (!task) throw new HttpError(404, "任务不存在或无权访问");
  const table = task.is_novel ? "jobs" : "comic_jobs";
  if (action === "cancel") {
    const upstream = task.is_novel
      ? [task.remote_id].filter(Boolean)
      : (
          await all(
            env,
            "SELECT payload FROM comic_jobs WHERE id=? OR parent_id=? OR parent_id IN (SELECT id FROM comic_jobs WHERE parent_id=?)",
            id,
            id,
            id,
          )
        )
          .map((row) => JSON.parse(row.payload).remote_id)
          .filter(Boolean);
    await run(
      env,
      `UPDATE ${table} SET state='cancelled',message='用户取消任务' WHERE id=? AND state IN ('queued','submitting','running','waiting')`,
      id,
    );
    if (table === "comic_jobs")
      await run(
        env,
        "UPDATE comic_jobs SET state='cancelled' WHERE parent_id=? OR parent_id IN (SELECT id FROM comic_jobs WHERE parent_id=?)",
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
  if (now() - job.created_at > 7200 || job.attempts >= 180)
    throw new HttpError(502, "番茄漫画任务超时");
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
    await run(
      env,
      "UPDATE comic_jobs SET state='queued',lease_until=0,updated_at=?,message='番茄服务生成章节中' WHERE id=?",
      now(),
      job.id,
    );
    await env.DOWNLOADS.send(
      { id: job.id, kind: "comic" },
      { delaySeconds: 60 },
    );
    return;
  }
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
    await reserve(env, key, artifact.size);
    const response = await source(env, artifact.url);
    if (!response.body) throw new HttpError(502, "产物为空");
    const fixed = new FixedLengthStream(artifact.size),
      transfer = response.body!.pipeTo(fixed.writable);
    const results = await Promise.allSettled([
      transfer,
      env.LIBRARY.put(key, fixed.readable, {
        sha256: artifact.sha256,
        httpMetadata: { contentType: "application/zip" },
      }),
    ]);
    if (results.some((r) => r.status === "rejected"))
      throw new HttpError(502, "产物校验失败");
    await run(env, "UPDATE storage_ledger SET state='stored' WHERE key=?", key);
  }
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
