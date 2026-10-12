// Background download pipeline (replaces the Flask subprocess workers).
// Each message stays well under the free plan's 50 external subrequests:
//   source  -> 2 fetches (detail page + API) + cover, then fans out chapter messages
//   chapter -> 1 fetch (image list), fans out pages messages of PAGES_PER_MESSAGE images
//   pages   -> N image fetches + N R2 puts (R2 counts as internal service, 1,000 limit)
import type { DownloadMessage, Env } from './env';
import { BAOZIMH_API_BASE, extOf, headers, loadChapterImages, loadSource } from './baozimh';

const now = () => new Date().toISOString();

async function log(env: Env, taskId: string, line: string, extra: Record<string, unknown> = {}) {
  const sets = Object.keys(extra).map((k) => `${k} = ?`);
  await env.DB.prepare(`UPDATE download_task SET log = COALESCE(log,'') || ? ${sets.length ? ',' + sets.join(',') : ''} WHERE id = ?`)
    .bind(`[${now()}] ${line}\n`, ...Object.values(extra), taskId)
    .run();
}

export async function handleMessage(msg: DownloadMessage, env: Env): Promise<void> {
  const api = env.BAOZIMH_API_BASE || BAOZIMH_API_BASE;
  if (msg.type === 'source') {
    await log(env, msg.taskId, `解析 ${msg.url}`, { status: 'running', start_time: now() });
    const src = await loadSource(msg.url, api);
    const limit = msg.maxChapters || Number(env.MAX_CHAPTERS_PER_TASK || 0) || src.chapters.length;
    const chapters = src.chapters.slice(0, limit);
    let coverKey: string | null = null;
    if (src.cover) {
      const r = await fetch(src.cover, { headers: headers() });
      if (r.ok) {
        coverKey = `comics/${src.title}/cover${extOf(src.cover, '.jpg')}`;
        await env.LIBRARY.put(coverKey, await r.arrayBuffer(), { httpMetadata: { contentType: r.headers.get('Content-Type') || 'image/jpeg' } });
      } else r.body?.cancel();
    }
    await env.DB.batch([
      env.DB.prepare('INSERT OR REPLACE INTO cf_comic (name, title, provider, source_url, cover_key, description, created_at) VALUES (?,?,?,?,?,?,?)')
        .bind(src.title, src.title, 'baozimh_org', msg.url, coverKey, src.description, now()),
      env.DB.prepare('UPDATE download_task SET comic_name = ?, total_chapters = ? WHERE id = ?').bind(src.title, chapters.length, msg.taskId),
    ]);
    if (!chapters.length) {
      await log(env, msg.taskId, '没有章节', { status: 'completed', end_time: now(), progress_percent: 100 });
      return;
    }
    const out = chapters.map((c) => ({ body: { type: 'chapter' as const, taskId: msg.taskId, comicName: src.title, mid: src.mid, chapterId: c.chapterId, order: c.order, title: c.title, filenameBase: c.filenameBase } }));
    for (let i = 0; i < out.length; i += 100) await env.DOWNLOADS.sendBatch(out.slice(i, i + 100));
    await log(env, msg.taskId, `《${src.title}》共 ${chapters.length} 章，已入队`);
    return;
  }

  if (msg.type === 'chapter') {
    const urls = await loadChapterImages(msg.mid, msg.chapterId, api);
    if (!urls.length) throw new Error(`章节 ${msg.title} 未找到图片`);
    const jobs = urls.map((url, i) => ({ url, key: `comics/${msg.comicName}/${msg.filenameBase}/${String(i + 1).padStart(3, '0')}${extOf(url)}` }));
    await env.DB.prepare(
      `INSERT OR REPLACE INTO cf_chapter (comic_name, chapter_order, filename_base, title, page_count, page_keys, pages_done, status, task_id) VALUES (?,?,?,?,?,?,0,'downloading',?)`,
    ).bind(msg.comicName, msg.order, msg.filenameBase, msg.title, jobs.length, JSON.stringify(jobs.map((j) => j.key)), msg.taskId).run();
    const per = Math.max(1, Math.min(40, Number(env.PAGES_PER_MESSAGE || 20)));
    const batches = [];
    for (let i = 0; i < jobs.length; i += per) batches.push({ body: { type: 'pages' as const, taskId: msg.taskId, comicName: msg.comicName, order: msg.order, jobs: jobs.slice(i, i + per) } });
    await env.DOWNLOADS.sendBatch(batches);
    return;
  }

  // pages
  let done = 0;
  for (const job of msg.jobs) {
    const head = await env.LIBRARY.head(job.key);
    if (head) { done++; continue; } // idempotent on retry
    const r = await fetch(job.url, { headers: headers() });
    if (!r.ok) { r.body?.cancel(); throw new Error(`图片 HTTP ${r.status}: ${job.url}`); }
    await env.LIBRARY.put(job.key, await r.arrayBuffer(), { httpMetadata: { contentType: r.headers.get('Content-Type') || 'image/webp' } });
    done++;
  }
  const row = await env.DB.prepare(
    `UPDATE cf_chapter SET pages_done = MIN(page_count, pages_done + ?), status = CASE WHEN pages_done + ? >= page_count THEN 'ready' ELSE status END
     WHERE comic_name = ? AND chapter_order = ? RETURNING status`,
  ).bind(done, done, msg.comicName, msg.order).first<{ status: string }>();
  if (row?.status === 'ready') {
    const t = await env.DB.prepare(
      `UPDATE download_task SET completed_chapters = (SELECT COUNT(*) FROM cf_chapter WHERE task_id = ?1 AND status = 'ready'),
         progress_percent = CAST(100.0 * (SELECT COUNT(*) FROM cf_chapter WHERE task_id = ?1 AND status = 'ready') / MAX(total_chapters, 1) AS INTEGER)
       WHERE id = ?1 RETURNING completed_chapters, total_chapters`,
    ).bind(msg.taskId).first<{ completed_chapters: number; total_chapters: number }>();
    if (t && t.completed_chapters >= t.total_chapters) await log(env, msg.taskId, '下载完成', { status: 'completed', end_time: now() });
  }
}

export async function queueHandler(batch: MessageBatch<DownloadMessage>, env: Env): Promise<void> {
  for (const m of batch.messages) {
    try {
      await handleMessage(m.body, env);
      m.ack();
    } catch (e) {
      const err = e instanceof Error ? e.message : String(e);
      if (m.attempts >= 3) {
        await log(env, m.body.taskId, `失败: ${err}`, { status: 'error' }).catch(() => {});
        m.ack();
      } else m.retry({ delaySeconds: 10 * m.attempts });
    }
  }
}
