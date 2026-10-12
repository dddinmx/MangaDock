import type { DownloadMessage, Env } from './env';
import { currentUser, hashPassword, sameOrigin, sessionCookie, signSession, verifyPassword, type User } from './auth';
import { URL_PATTERN } from './baozimh';
import { esc, layout, loginPage, readerScript } from './html';
import { queueHandler } from './queue';

export type { Env };

const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json; charset=utf-8' } });
const redirect = (to: string, extra: HeadersInit = {}) => new Response(null, { status: 302, headers: { Location: to, ...extra } });
const clampInt = (v: unknown, def = 0, max = 1_000_000) => { const n = Math.trunc(Number(v)); return Number.isFinite(n) ? Math.max(0, Math.min(n, max)) : def; };

async function login(req: Request, env: Env): Promise<Response> {
  if (req.method === 'GET') return loginPage();
  if (!sameOrigin(req)) return new Response('CSRF check failed', { status: 403 });
  const form = await req.formData();
  const username = String(form.get('username') || '').trim();
  const password = String(form.get('password') || '');
  const u = await env.DB.prepare('SELECT id, password_hash, session_version FROM user WHERE username = ?').bind(username).first<User>();
  const ip = req.headers.get('CF-Connecting-IP') || '';
  const result = u ? await verifyPassword(u.password_hash, password) : 'bad';
  await env.DB.prepare('INSERT INTO login_log (username, ip_address, user_agent, login_time, success) VALUES (?,?,?,?,?)')
    .bind(username, ip, (req.headers.get('User-Agent') || '').slice(0, 500), new Date().toISOString(), result === 'ok' ? 1 : 0).run().catch(() => {});
  if (result === 'legacy_unsupported') return loginPage('该账号密码哈希来自旧版（迭代次数超过 Workers 上限），请用 scripts/set-password.mjs 重置。');
  if (result !== 'ok' || !u) return loginPage('用户名或密码错误');
  const token = await signSession(env, { uid: u.id, ver: Number(u.session_version || 1), exp: Date.now() + 30 * 86400_000 });
  return redirect('/', { 'Set-Cookie': sessionCookie(token) });
}

async function library(env: Env, user: User): Promise<Response> {
  const { results } = await env.DB.prepare(
    `SELECT c.name, c.cover_key, (SELECT COUNT(*) FROM cf_chapter ch WHERE ch.comic_name = c.name AND ch.status='ready') AS chapters FROM cf_comic c ORDER BY c.created_at DESC LIMIT 200`,
  ).all<{ name: string; cover_key: string | null; chapters: number }>();
  const cards = results.map((c) => `<a href="/comic/${encodeURIComponent(c.name)}" style="display:block;width:150px">
      <img src="${c.cover_key ? '/api/comic-pages/' + encodeURIComponent(c.cover_key) : '/static/default-comic-cover.jpg'}" alt="" style="width:150px;aspect-ratio:3/4;object-fit:cover;border-radius:8px" loading="lazy">
      <div>${esc(c.name)}</div><small>${c.chapters} 章</small></a>`).join('');
  return layout('书架', `<h1>书架</h1><div style="display:flex;flex-wrap:wrap;gap:1rem">${cards || '<p>还没有漫画，去 <a href="/download">下载</a>。</p>'}</div>`, { user: user.username });
}

async function chapters(env: Env, name: string) {
  const { results } = await env.DB.prepare(`SELECT chapter_order, title, page_count, filename_base FROM cf_chapter WHERE comic_name = ? AND status='ready' ORDER BY chapter_order`).bind(name).all<{ chapter_order: number; title: string; page_count: number; filename_base: string }>();
  return results;
}

async function route(req: Request, env: Env): Promise<Response> {
  const url = new URL(req.url);
  const p = url.pathname;
  if (p === '/login') return login(req, env);
  if (p === '/healthz') return json({ ok: true });

  const user = await currentUser(env, req);
  if (!user) return p.startsWith('/api/') || req.method !== 'GET' ? json({ error: 'unauthorized' }, 401) : redirect('/login');
  if (req.method === 'POST' && !sameOrigin(req)) return json({ error: 'CSRF check failed' }, 403);

  if (p === '/logout' && req.method === 'POST') return redirect('/login', { 'Set-Cookie': sessionCookie('', 0) });
  if (p === '/' || p === '/comics') return library(env, user);

  if (p === '/download' && req.method === 'GET')
    return layout('下载', `<h1>下载漫画</h1><p>当前 Workers 版本已移植的源：baozimh.org（<code>https://baozimh.org/manga/&lt;slug&gt;</code>）。</p>
      <form id="f"><input name="url" style="width:100%" placeholder="https://baozimh.org/manga/..." required><br><label>最多章节数 <input name="max_chapters" type="number" min="0" value="0"></label> <button>开始</button></form><pre id="out"></pre>
      <script>f.onsubmit=async e=>{e.preventDefault();const d=Object.fromEntries(new FormData(f));const r=await fetch('/api/download',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)});const j=await r.json();out.textContent=JSON.stringify(j,null,2);
      if(j.task_id){const t=setInterval(async()=>{const s=await (await fetch('/api/tasks/'+j.task_id)).json();out.textContent=JSON.stringify(s,null,2);if(s.status==='completed'||s.status==='error')clearInterval(t)},2000)}}</script>`, { user: user.username });

  if (p === '/api/download' && req.method === 'POST') {
    if (user.role !== 'admin' && !user.can_download) return json({ error: '无下载权限' }, 403);
    const body = (await req.json().catch(() => ({}))) as { url?: string; max_chapters?: number | string };
    const src = String(body.url || '').trim();
    if (!URL_PATTERN.test(src)) return json({ error: '暂不支持该链接（Workers 版目前仅移植 baozimh.org）' }, 400);
    const id = crypto.randomUUID();
    await env.DB.prepare(`INSERT INTO download_task (id, comic_name, url, status, progress_percent, total_chapters, completed_chapters, comic_format, log, created_at, is_update, "group", allow_adult, created_by_user_id)
      VALUES (?, '', ?, 'pending', 0, 0, 0, 1, '', ?, 0, '默认分组', 0, ?)`).bind(id, src, new Date().toISOString(), user.id).run();
    const msg: DownloadMessage = { type: 'source', taskId: id, url: src, maxChapters: clampInt(body.max_chapters, 0, 10000) || undefined };
    await env.DOWNLOADS.send(msg);
    return json({ status: 'queued', task_id: id }, 202);
  }

  let m: RegExpExecArray | null;
  if ((m = /^\/api\/tasks\/([0-9a-f-]{36})$/.exec(p))) {
    const t = await env.DB.prepare('SELECT id, comic_name, status, progress_percent, total_chapters, completed_chapters, log FROM download_task WHERE id = ?').bind(m[1]).first();
    return t ? json(t) : json({ error: 'not found' }, 404);
  }

  if ((m = /^\/api\/chapters\/(.+)$/.exec(p))) {
    const name = decodeURIComponent(m[1]);
    const list = await chapters(env, name);
    if (!list.length) return json({ error: '漫画文件不存在' }, 404);
    return json({ comic_name: name, chapters: list.map((c) => ({ order: c.chapter_order, title: c.title, pages: c.page_count, filename: c.filename_base })), total_chapters: list.length });
  }

  if ((m = /^\/comic\/(.+)$/.exec(p))) {
    const name = decodeURIComponent(m[1]);
    const list = await chapters(env, name);
    const items = list.map((c) => `<li><a href="/reader/${encodeURIComponent(name)}/${c.chapter_order}">${esc(c.title)}</a> <small>${c.page_count}P</small></li>`).join('');
    return layout(name, `<h1>${esc(name)}</h1><ol>${items || '<li>暂无已完成章节</li>'}</ol>`, { user: user.username });
  }

  if ((m = /^\/reader\/(.+)\/(\d+)$/.exec(p))) {
    const name = decodeURIComponent(m[1]);
    const order = Number(m[2]);
    const all = await chapters(env, name);
    const ch = await env.DB.prepare(`SELECT title, page_keys FROM cf_chapter WHERE comic_name = ? AND chapter_order = ? AND status='ready'`).bind(name, order).first<{ title: string; page_keys: string }>();
    if (!ch) return layout('未找到', '<p>章节不存在</p>', { user: user.username });
    const keys = JSON.parse(ch.page_keys) as string[];
    const idx = all.findIndex((c) => c.chapter_order === order);
    const next = all[idx + 1];
    const imgs = keys.map((k, i) => `<img class="md-page" src="/api/comic-pages/${encodeURIComponent(k)}" alt="${i + 1}" loading="lazy" style="display:block;width:100%;max-width:900px;margin:0 auto">`).join('');
    return layout(`${name} ${ch.title}`, `<h2>${esc(name)} · ${esc(ch.title)}</h2>${imgs}
      <p style="text-align:center">${next ? `<a href="/reader/${encodeURIComponent(name)}/${next.chapter_order}">下一章 →</a>` : '已是最后一章'}</p>${readerScript(name, order, all.length, keys.length)}`, { user: user.username });
  }

  if ((m = /^\/api\/comic-pages\/(.+)$/.exec(p))) {
    const key = decodeURIComponent(m[1]);
    if (!key.startsWith('comics/') || key.includes('..')) return json({ error: 'bad key' }, 400);
    const obj = await env.LIBRARY.get(key, { onlyIf: req.headers });
    if (!obj) return new Response('Not found', { status: 404 });
    const h = new Headers();
    obj.writeHttpMetadata(h);
    h.set('ETag', obj.httpEtag);
    h.set('Cache-Control', 'private, max-age=31536000, immutable');
    return 'body' in obj && obj.body ? new Response(obj.body, { headers: h }) : new Response(null, { status: 304, headers: h });
  }

  if (p === '/save_progress' && req.method === 'POST') {
    const d = (await req.json().catch(() => null)) as Record<string, unknown> | null;
    if (!d) return json({ status: 'error', message: '无效的JSON格式' }, 400);
    const comic = String(d.comic_name || '').trim();
    if (!comic) return json({ status: 'error', message: '漫画名称不能为空' }, 400);
    const nowIso = new Date().toISOString();
    const clientMs = d.client_progress_ms == null ? null : clampInt(d.client_progress_ms, 0, Number.MAX_SAFE_INTEGER);
    // Upsert; an older client snapshot (smaller client_progress_ms) never overwrites a newer one.
    await env.DB.prepare(`INSERT INTO reading_progress (user_id, comic_name, last_chapter, last_page, scroll_position, anchor_paragraph, anchor_offset, total_chapters, total_pages, client_progress_ms, last_read_at, created_at)
       VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?11)
       ON CONFLICT (user_id, comic_name) DO UPDATE SET last_chapter=excluded.last_chapter, last_page=excluded.last_page, scroll_position=excluded.scroll_position,
         anchor_paragraph=excluded.anchor_paragraph, anchor_offset=excluded.anchor_offset, total_chapters=excluded.total_chapters, total_pages=excluded.total_pages,
         client_progress_ms=excluded.client_progress_ms, last_read_at=excluded.last_read_at
       WHERE excluded.client_progress_ms IS NULL OR reading_progress.client_progress_ms IS NULL OR excluded.client_progress_ms >= reading_progress.client_progress_ms`)
      .bind(user.id, comic, clampInt(d.chapter), clampInt(d.page), clampInt(d.scroll_position), d.anchor_paragraph == null ? null : clampInt(d.anchor_paragraph),
        d.anchor_offset == null ? null : clampInt(d.anchor_offset), clampInt(d.total_chapters), clampInt(d.total_pages), clientMs, nowIso).run();
    return json({ status: 'success' });
  }

  if ((m = /^\/get_progress\/(.+)$/.exec(p))) {
    const r = await env.DB.prepare('SELECT last_chapter, last_page, scroll_position, anchor_paragraph, anchor_offset FROM reading_progress WHERE user_id = ? AND comic_name = ?')
      .bind(user.id, decodeURIComponent(m[1])).first<any>();
    return json({ status: 'success', chapter: r?.last_chapter ?? 0, page: r?.last_page ?? 0, scroll_position: r?.scroll_position ?? 0, anchor_paragraph: r?.anchor_paragraph ?? null, anchor_offset: r?.anchor_offset ?? null });
  }

  return new Response('Not found', { status: 404 });
}

export default {
  async fetch(req, env, _ctx): Promise<Response> {
    try {
      return await route(req, env);
    } catch (e) {
      console.error(e);
      return json({ error: 'internal error' }, 500);
    }
  },
  queue: queueHandler,
} satisfies ExportedHandler<Env, DownloadMessage>;

export { hashPassword };
