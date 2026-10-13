import { HttpError, now, json, type Env, type Identity } from "./index";
import {
  all,
  one,
  run,
  admin,
  putBytes,
  stored,
  input,
  type Row,
} from "./library";
import { notifyCompute } from "./compute-scheduler";
import { queueGarbage } from "./storage";
import { fencedEnv, assertComputeLease } from "./job-state";
import { completeParent } from "./downloads";
import { webdavRequest } from "./integrations";
import { remotePage } from "./remote-library";
import { chapterManifest } from "./media";
const ORPHAN_COMPUTE =
  "json_extract(compute_jobs.payload,'$.parent_job') IS NOT NULL AND NOT EXISTS(SELECT 1 FROM comic_jobs p LEFT JOIN comic_jobs r ON r.id=p.parent_id WHERE p.id=json_extract(compute_jobs.payload,'$.parent_job') AND p.state IN ('queued','running','waiting','submitting') AND (r.id IS NULL OR r.state NOT IN ('cancelled','error')))";
/** Parent-job cancellation is maintenance work, not the jobs poll. */
export async function cancelOrphanedCompute(env: Env) {
  await run(
    env,
    `UPDATE compute_jobs SET state='cancelled',claim_token=NULL,lease_until=0 WHERE state IN ('queued','running') AND ${ORPHAN_COMPUTE}`,
  );
}
export async function queueCompute(
  env: Env,
  kind: string,
  inputKey: string,
  payload: Row,
) {
  const known = await one(
    env,
    "SELECT id FROM compute_jobs WHERE kind=? AND input_key=? AND state IN ('queued','running')",
    kind,
    inputKey,
  );
  if (known) {
    // Reusing a queued job must also repair a lost scheduler wake.
    await notifyCompute(env);
    return known.id;
  }
  const id = crypto.randomUUID();
  const inserted = await run(
    env,
    "INSERT INTO compute_jobs(id,kind,input_key,payload,created_at,updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
    id,
    kind,
    inputKey,
    JSON.stringify(payload),
    now(),
    now(),
  );
  if (!inserted.meta.changes) {
    const concurrent = await one(
      env,
      "SELECT id FROM compute_jobs WHERE kind=? AND input_key=? AND state IN ('queued','running')",
      kind,
      inputKey,
    );
    if (concurrent) return concurrent.id;
    throw new HttpError(409, "下载任务已停止");
  }
  await notifyCompute(env);
  return id;
}
export async function computeApi(
  request: Request,
  env: Env,
  user: Identity,
): Promise<Response | null> {
  const url = new URL(request.url);
  if (!url.pathname.startsWith("/api/admin/compute/")) return null;
  admin(user);
  if (
    url.pathname === "/api/admin/compute/discover" &&
    request.method === "POST"
  ) {
    // Explicit administrator action may retry failed indexes. Ordinary reads
    // never create endless replacement jobs for an unreadable EPUB.
    for (const book of await all(
      env,
      "SELECT id,object_key,sha256 FROM books WHERE index_key IS NULL LIMIT 50",
    )) {
      await queueCompute(env, "index-epub", book.object_key, {
        book_id: book.id,
        sha256: book.sha256,
      });
    }
    for (const comic of await all(
      env,
      "SELECT id,name FROM comics c WHERE banner_key IS NULL AND NOT EXISTS (SELECT 1 FROM compute_jobs j WHERE j.kind='banner' AND j.input_key='banner/'||c.id AND j.state='completed') LIMIT 50",
    ))
      await queueCompute(env, "banner", "banner/" + comic.id, {
        comic_id: comic.id,
        comic_name: comic.name,
      });
    for (const kind of ["comic", "novel"])
      for (const media of await all(
        env,
        `SELECT id,cover_key FROM ${kind === "comic" ? "comics" : "books"} WHERE cover_key IS NOT NULL AND hero_key IS NULL LIMIT 100`,
      ))
        await queueCompute(env, "cover", media.cover_key, {
          kind,
          media_id: media.id,
        });
    return json({ ok: true });
  }
  if (url.pathname === "/api/admin/compute/jobs" && request.method === "GET") {
    await run(
      env,
      "UPDATE compute_jobs SET state='error',message='云端转换重试次数已用尽',updated_at=? WHERE state IN ('queued','running') AND attempts>=3 AND lease_until<?",
      now(),
      now(),
    );
    return json({
      ok: true,
      maintenance_pending: !!(await one(
        env,
        "SELECT 1 WHERE EXISTS(SELECT 1 FROM storage_gc) OR EXISTS(SELECT 1 FROM uploads WHERE state='aborted' OR state='uploading' AND created_at<?) OR EXISTS(SELECT 1 FROM storage_ledger WHERE state='reserved' AND created_at<?) OR EXISTS(SELECT 1 FROM comic_jobs WHERE state IN ('queued','running')) OR EXISTS(SELECT 1 FROM jobs WHERE state IN ('queued','running'))",
        now() - 86400,
        now() - 86400,
      )),
      data: await all(
        env,
        `SELECT id,kind,payload,state FROM compute_jobs WHERE attempts<3 AND (state='queued' OR state='running' AND lease_until<?) AND NOT (${ORPHAN_COMPUTE}) ORDER BY created_at LIMIT 20`,
        now(),
      ),
    });
  }
  const match = url.pathname.match(
    /^\/api\/admin\/compute\/jobs\/([^/]+)\/(claim|input|pages\/(\d+)|output\/(hero|mobile|original|artifact|index|source|chapter\/\d+|page\/(\d+))|complete|fail)$/,
  );
  if (!match) throw new HttpError(404, "计算作业接口不存在");
  const id = match[1],
    job = await one(env, "SELECT * FROM compute_jobs WHERE id=?", id);
  if (!job) throw new HttpError(404, "计算作业不存在");
  const data = JSON.parse(job.payload),
    action = match[2];
  if (action === "claim" && request.method === "POST") {
    const token = crypto.randomUUID();
    const row = await env.DB.prepare(
      "UPDATE compute_jobs SET state='running',claim_token=?,attempts=attempts+1,lease_until=?,updated_at=? WHERE id=? AND attempts<3 AND (state='queued' OR state='running' AND lease_until<?) RETURNING id",
    )
      .bind(token, now() + 1800, now(), id, now())
      .first();
    if (!row) throw new HttpError(409, "作业正在运行");
    return json({
      ok: true,
      data: {
        ...data,
        id,
        kind: job.kind,
        lease_token: token,
        output_prefix: `compute/${id}/${token}`,
      },
    });
  }
  const token = request.headers.get("X-Compute-Lease");
  await assertComputeLease(env, id, token);
  const originalEnv = env;
  env = fencedEnv(env, "compute_jobs", job);
  const outputPrefix = `compute/${id}/${token}`;
  if (action === "input" && ["GET", "HEAD"].includes(request.method)) {
    if (job.input_key.startsWith("webdav://")) {
      const match = job.input_key.match(/^webdav:\/\/([^/]+)\/(.+)$/),
        mount = await one(
          env,
          "SELECT * FROM remote_mounts WHERE id=? AND enabled=1",
          match?.[1],
        );
      if (!mount) throw new HttpError(503, "网盘已断开");
      return webdavRequest(env, mount, decodeURIComponent(match![2]));
    }
    return stored(env, job.input_key, request);
  }
  if (action.startsWith("pages/") && request.method === "GET") {
    const chapter = await one(
      env,
      "SELECT * FROM chapters WHERE id=?",
      data.chapter_id,
    );
    if (!chapter) throw new HttpError(404, "章节不存在");
    const pages = await chapterManifest(env, chapter),
      page = pages[Number(match[3])];
    if (!page) throw new HttpError(404, "页面不存在");
    return page.key
      ? stored(env, page.key, request)
      : remotePage(env, chapter, page);
  }
  if (job.state !== "running") throw new HttpError(409, "请先领取作业");
  if (action.startsWith("output/") && request.method === "PUT") {
    const reader = request.body?.getReader(),
      parts: Uint8Array[] = [];
    let size = 0;
    if (!reader) throw new HttpError(400, "输出为空");
    for (;;) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > 20971520) {
        await reader.cancel();
        throw new HttpError(413, "输出超过 20 MB，请拆分");
      }
      parts.push(part.value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const part of parts) {
      bytes.set(part, offset);
      offset += part.byteLength;
    }
    const key = `${outputPrefix}/${match[4]}`,
      mime = request.headers.get("Content-Type") || "application/octet-stream";
    if (
      job.kind === "cover" &&
      !["image/jpeg", "image/png", "image/webp"].includes(mime)
    )
      throw new HttpError(400, "封面输出格式无效");
    await assertComputeLease(originalEnv, id, token);
    await putBytes(env, key, bytes, mime);
    await assertComputeLease(originalEnv, id, token).catch(async (error) => {
      await queueGarbage(originalEnv, outputPrefix + "/", true);
      throw error;
    });
    return json({ ok: true, data: { key, size } });
  }
  if (action === "fail" && request.method === "POST") {
    if (data.parent_job) {
      await run(
        env,
        "UPDATE comic_jobs SET state='error',message='云端转换失败，请重试',lease_until=0,updated_at=? WHERE id=?",
        now(),
        data.parent_job,
      );
      await run(
        env,
        "UPDATE comic_jobs SET state='error',message='章节转换失败，请重试',updated_at=? WHERE id=(SELECT parent_id FROM comic_jobs WHERE id=?)",
        now(),
        data.parent_job,
      );
    }
    await run(
      env,
      "UPDATE compute_jobs SET state='error',message='云端转换失败，请检查 CI 日志',lease_until=0,updated_at=? WHERE id=?",
      now(),
      id,
    );
    return json({ ok: true });
  }
  if (action === "complete" && request.method === "POST") {
    const result = await input(request);
    if (job.kind === "cover") {
      for (const variant of ["original", "hero", "mobile"])
        if (!(await env.LIBRARY.head(`${outputPrefix}/${variant}`)))
          throw new HttpError(409, "封面输出不完整");
      const table = data.kind === "novel" ? "books" : "comics",
        media = await one(
          env,
          `SELECT cover_key FROM ${table} WHERE id=?`,
          data.media_id,
        );
      if (media?.cover_key !== job.input_key) {
        await run(
          env,
          "UPDATE compute_jobs SET state='cancelled',message='原封面已更新' WHERE id=?",
          id,
        );
        return json({ ok: true });
      }
      await run(
        env,
        `UPDATE ${table} SET cover_key=?,hero_key=?,mobile_hero_key=? WHERE id=?`,
        `${outputPrefix}/original`,
        `${outputPrefix}/hero`,
        `${outputPrefix}/mobile`,
        data.media_id,
      );
    } else if (job.kind === "banner") {
      if (result.found) {
        const key = `${outputPrefix}/hero`;
        if (!(await env.LIBRARY.head(key)))
          throw new HttpError(409, "横幅未上传");
        await run(
          env,
          "UPDATE comics SET banner_key=? WHERE id=?",
          key,
          data.comic_id,
        );
      }
    } else if (job.kind === "index-epub") {
      const key = `${outputPrefix}/index`,
        indexObject = await env.LIBRARY.get(key);
      if (!indexObject) throw new HttpError(409, "章节索引未上传");
      const index = (await indexObject.json()) as Row;
      if (
        !Array.isArray(index.chapters) ||
        !index.chapters.length ||
        index.chapters.length > 10000
      )
        throw new HttpError(400, "章节索引无效");
      for (const chapter of index.chapters)
        if (
          chapter.key !== `${outputPrefix}/chapter/${chapter.index}` ||
          !(await env.LIBRARY.head(chapter.key))
        )
          throw new HttpError(409, "章节正文未全部上传");
      await run(
        env,
        "UPDATE books SET index_key=?,chapter_count=? WHERE id=? AND sha256=?",
        key,
        index.chapters.length,
        data.book_id,
        data.sha256,
      );
    } else if (job.kind === "pdf") {
      const key = `${outputPrefix}/artifact`;
      if (!(await env.LIBRARY.head(key)))
        throw new HttpError(409, "PDF 产物不存在");
      await env.DB.batch([
        env.DB.prepare(
          "UPDATE chapters SET object_key=?,format='pdf',state='ready' WHERE id=?",
        ).bind(key, data.chapter_id),
      ]);
    } else if (job.kind === "legacy-source") {
      const uploaded = await env.LIBRARY.get(`${outputPrefix}/source`);
      if (uploaded && uploaded.size > 2097152)
        throw new HttpError(413, "来源元数据过大");
      const source = uploaded ? await uploaded.json<Row>() : result.source;
      if (
        !source ||
        typeof source.title !== "string" ||
        !Array.isArray(source.chapters) ||
        !source.chapters.length ||
        source.chapters.length > 10000
      )
        throw new HttpError(400, "来源元数据无效");
      const key = `${outputPrefix}/source.json`;
      await putBytes(
        env,
        key,
        new TextEncoder().encode(JSON.stringify(source)),
        "application/json",
      );
      const parent = await one(
        env,
        "SELECT payload FROM comic_jobs WHERE id=? AND state IN ('running','waiting')",
        data.parent_job,
      );
      if (!parent) throw new HttpError(409, "来源任务已停止");
      const payload = { ...JSON.parse(parent.payload), source_key: key };
      await run(
        env,
        "UPDATE comic_jobs SET payload=?,state='queued',lease_until=0,updated_at=? WHERE id=?",
        JSON.stringify(payload),
        now(),
        data.parent_job,
      );
    } else if (job.kind === "fanqie-archive") {
      const key = `${outputPrefix}/artifact`,
        object = await env.LIBRARY.head(key);
      if (!object) throw new HttpError(409, "章节文件未上传");
      await env.DB.batch([
        env.DB.prepare(
          "UPDATE chapters SET object_key=?,size=?,format=?,state='ready' WHERE id=?",
        ).bind(key, object.size, data.format, data.chapter_id),
      ]);
    } else if (job.kind === "archive" || job.kind === "legacy-chapter") {
      if (job.kind === "archive") {
        const chapter = await one(
          env,
          "SELECT source_version,object_key,remote_url,mount_id FROM chapters WHERE id=?",
          data.chapter_id,
        );
        const current =
          chapter &&
          (chapter.object_key ||
            `webdav://${chapter.mount_id}/${encodeURIComponent(chapter.remote_url)}`);
        if (
          !chapter ||
          current !== job.input_key ||
          (data.source_version !== undefined &&
            (chapter.source_version || null) !== data.source_version)
        ) {
          await run(
            env,
            "UPDATE compute_jobs SET state='cancelled',message='章节已更新或移除',lease_until=0 WHERE id=?",
            id,
          );
          return json({ ok: true });
        }
      }
      if (
        !Array.isArray(result.pages) ||
        !result.pages.length ||
        result.pages.length > 10000
      )
        throw new HttpError(400, "页面清单无效");
      const manifest = [];
      for (const [i, page] of result.pages.entries()) {
        const key = `${outputPrefix}/page/${i}`;
        if (!(await env.LIBRARY.head(key)))
          throw new HttpError(409, "页面未全部上传");
        manifest.push({ key, width: page.width, height: page.height });
      }
      const key = `${outputPrefix}/manifest.json`;
      await putBytes(
        env,
        key,
        new TextEncoder().encode(JSON.stringify(manifest)),
        "application/json",
      );
      await env.DB.batch([
        env.DB.prepare(
          "UPDATE chapters SET manifest_key=?,page_count=?,state='ready' WHERE id=?",
        ).bind(key, manifest.length, data.chapter_id),
      ]);
    }
    await assertComputeLease(originalEnv, id, token);
    await run(
      env,
      "UPDATE compute_jobs SET state='completed',lease_until=0,updated_at=? WHERE id=?",
      now(),
      id,
    );
    if (job.kind === "legacy-source")
      await env.DOWNLOADS.send({ id: data.parent_job, kind: "comic" });
    if (data.parent_job && job.kind !== "legacy-source") {
      await run(
        env,
        "UPDATE comic_jobs SET state='completed',updated_at=? WHERE id=? AND state IN ('running','waiting')",
        now(),
        data.parent_job,
      );
      const parent = await one(
        env,
        "SELECT parent_id FROM comic_jobs WHERE id=?",
        data.parent_job,
      );
      if (parent?.parent_id)
        await completeParent(originalEnv, parent.parent_id);
    }
    return json({ ok: true });
  }
  throw new HttpError(405, "不支持的作业操作");
}
