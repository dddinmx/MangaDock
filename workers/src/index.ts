import { DurableObject } from 'cloudflare:workers';
import { pbkdf2 } from '@noble/hashes/pbkdf2.js';
import { sha256 } from '@noble/hashes/sha2.js';
export interface Env {
  AUTH_SERVICE: DurableObjectNamespace; DB: D1Database; LIBRARY: R2Bucket; DOWNLOADS: Queue<{id: string}>; ASSETS: Fetcher;
  FANQIE_API_URL: string; FANQIE_API_TOKEN?: string; FANQIE_INSTANCE_ID?: string; MAX_ARTIFACT_BYTES: string; MAX_LIBRARY_BYTES?: string;
}
class HttpError extends Error { constructor(public status: number, message: string) { super(message); } }
const now = () => Math.floor(Date.now() / 1000);
const hex = (v: ArrayBuffer) => [...new Uint8Array(v)].map(x => x.toString(16).padStart(2, '0')).join('');
export const hash = async (value: string) => hex(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)));
export async function verifyPassword(password: string, encoded: string) {
  const match = /^pbkdf2:sha256:(\d+)\$([^$]{1,128})\$([a-f0-9]{64})$/.exec(encoded);
  if (!match) return false;
  const iterations=Number(match[1]);
  if (!Number.isSafeInteger(iterations) || iterations<100000 || iterations>1000000) return false;
  // Durable Objects have a separate 30s CPU budget even on Free; the HTTP Worker waits on I/O.
  // Pure JS supports legacy 260k and new 600k hashes without WebCrypto's 100k ceiling.
  const actual=pbkdf2(sha256,new TextEncoder().encode(password),new TextEncoder().encode(match[2]),{c:iterations,dkLen:32});
  let difference=0;
  for(let i=0;i<actual.length;i++)difference|=actual[i]^parseInt(match[3].slice(i*2,i*2+2),16);
  return difference===0;
}
export class PasswordVerifier extends DurableObject<Env> {
  async fetch(request: Request): Promise<Response> {
    const {password,encoded}=await request.json() as {password:unknown;encoded:unknown};
    if(typeof password!=='string'||password.length>512||typeof encoded!=='string'||encoded.length>512) return Response.json({valid:false},{status:400});
    return Response.json({valid:await verifyPassword(password,encoded)});
  }
}

const json = (data: unknown, status = 200, headers: HeadersInit = {}) => Response.json(data, {status, headers});
async function body(request: Request): Promise<Record<string, unknown>> {
  if (!request.headers.get('content-type')?.startsWith('application/json')) throw new HttpError(415, '需要 JSON');
  const text = await request.text();
  if (text.length > 8192) throw new HttpError(413, '请求过大');
  try { const value = JSON.parse(text); if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(); return value; }
  catch { throw new HttpError(400, '无效 JSON'); }
}
function sameOrigin(request: Request) {
  if (request.headers.get('Origin') !== new URL(request.url).origin) throw new HttpError(403, '请求来源无效');
}
type Identity = {user_id: string; username: string; role: string; csrf: string; token_hash: string};
async function identity(request: Request, env: Env): Promise<Identity> {
  const token = request.headers.get('Cookie')?.match(/(?:^|;\s*)md_session=([a-f0-9]{64})(?:;|$)/)?.[1];
  if (!token) throw new HttpError(401, '请登录');
  const user = await env.DB.prepare('SELECT s.user_id, u.username, u.role, s.csrf, s.token_hash FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?').bind(await hash(token), now()).first<Identity>();
  if (!user) throw new HttpError(401, '请重新登录');
  if (!['GET','HEAD'].includes(request.method)) {
    sameOrigin(request);
    if (request.headers.get('X-CSRFToken') !== user.csrf) throw new HttpError(403, 'CSRF 校验失败');
  }
  return user;
}
export async function source(env: Env, path: string, init: RequestInit = {}) {
  if (!env.FANQIE_API_TOKEN || !env.FANQIE_INSTANCE_ID) throw new HttpError(503, '番茄服务凭据尚未配置');
  // Paths come only from this adapter. Never forward credentials to a redirect or an artifact-supplied host.
  if (!path.startsWith('/v1/') || path.includes('..') || path.includes('\\') || path.includes('#')) throw new HttpError(502, '来源路径无效');
  const base = new URL(env.FANQIE_API_URL);
  if (base.protocol !== 'https:') throw new HttpError(503, '来源必须使用 HTTPS');
  const response = await fetch(new URL(path, base), {...init, redirect:'manual', signal:AbortSignal.timeout(25000), headers:{'Authorization':`Bearer ${env.FANQIE_API_TOKEN}`, 'X-MangaDock-Instance-ID':env.FANQIE_INSTANCE_ID, 'X-MangaDock-Version':'5.0', 'X-MangaDock-System':'Cloudflare/Workers', 'User-Agent':'MangaDock/5.0 Workers/0.1', 'Content-Type':'application/json'}});
  if (!response.ok) { await response.body?.cancel(); throw new HttpError(502, `番茄服务 HTTP ${response.status}`); }
  return response;
}
async function sourceJson(env: Env, path: string, init: RequestInit = {}) {
  const response = await source(env, path, init);
  // Metadata is small; EPUB bytes never enter JS heap.
  const payload = await response.json() as {ok: boolean; data: Record<string, any>};
  if (payload.ok !== true || !payload.data || typeof payload.data !== 'object') throw new HttpError(502, '来源响应无效');
  return payload.data;
}
type Job = {id:string; source_id:string; chapter_ids:string; remote_id:string|null; state:string; attempts:number; created_at:number; reserved_bytes:number};
export async function processJob(env: Env, id: string) {
  const lease = await env.DB.prepare("UPDATE jobs SET lease_until=?, updated_at=? WHERE id=? AND state IN ('queued','running') AND lease_until<? RETURNING *").bind(now()+300, now(), id, now()).first<Job>();
  if (!lease) return;
  let job = lease;
  try {
    if (now() - job.created_at > 10800 || job.attempts >= 180) throw new HttpError(502, '远端任务超时，请检查来源服务后重试');
    if (!job.remote_id) {
      // A crash after POST acceptance cannot be safely retried without upstream idempotency support.
      await env.DB.prepare("UPDATE jobs SET state='submitting', updated_at=? WHERE id=?").bind(now(), id).run();
      const remote = await sourceJson(env, '/v1/jobs', {method:'POST', body:JSON.stringify({target:job.source_id,kind:'novel',format:'epub',chapter_ids:JSON.parse(job.chapter_ids)})});
      if (typeof remote.id !== 'string' || !/^[a-zA-Z0-9_-]{1,100}$/.test(remote.id)) throw new HttpError(502, '来源任务 ID 无效');
      job.remote_id = remote.id;
      await env.DB.prepare("UPDATE jobs SET state='running', remote_id=?, updated_at=? WHERE id=?").bind(remote.id, now(), id).run();
    }
    const remote = await sourceJson(env, `/v1/jobs/${job.remote_id}`);
    if (['queued','running'].includes(remote.status)) {
      await env.DB.prepare("UPDATE jobs SET attempts=attempts+1, state='running', lease_until=0, updated_at=?, message='来源服务生成 EPUB 中' WHERE id=?").bind(now(), id).run();
      await env.DOWNLOADS.send({id}, {delaySeconds:60});
      return;
    }
    if (remote.status !== 'completed') throw new HttpError(502, '来源任务未成功完成');
    const artifact = remote.artifact;
    const max = Math.min(Number(env.MAX_ARTIFACT_BYTES) || 33554432, 33554432);
    if (!artifact || !Number.isSafeInteger(artifact.size) || artifact.size <= 0 || artifact.size > max || !/^[a-f0-9]{64}$/i.test(artifact.sha256) || artifact.url !== `/v1/jobs/${job.remote_id}/artifact`) throw new HttpError(502, '产物格式或大小不符合首阶段限制（32 MB）');
    const key = `novels/${job.source_id}/${artifact.sha256.toLowerCase()}.epub`;
    if (!job.reserved_bytes) {
      const reserved = await env.DB.prepare('UPDATE jobs SET reserved_bytes=? WHERE id=? AND reserved_bytes=0 AND (SELECT COALESCE(SUM(size),0) FROM books)+(SELECT COALESCE(SUM(reserved_bytes),0) FROM jobs)+?<=? RETURNING id').bind(artifact.size,id,artifact.size,Number(env.MAX_LIBRARY_BYTES)||256000000).first();
      if (!reserved) throw new HttpError(507, '书库已达到免费预算上限；此次下载未保存');
    }
    const existing = await env.LIBRARY.head(key);
    if (!existing) {
      const response = await source(env, artifact.url);
      if (!response.body) throw new HttpError(502, '产物为空');
      if (response.headers.has('content-length') && Number(response.headers.get('content-length')) !== artifact.size) { await response.body.cancel(); throw new HttpError(502, '产物长度不一致'); }
      const fixed = new FixedLengthStream(artifact.size);
      // R2 checks SHA-256; FixedLengthStream rejects under/overflow. No unzip/hash loop in Workers CPU budget.
      const transfer = response.body.pipeTo(fixed.writable);
      const results = await Promise.allSettled([transfer, env.LIBRARY.put(key, fixed.readable, {sha256:artifact.sha256.toLowerCase(), httpMetadata:{contentType:'application/epub+zip'}})]);
      if (results.some(r => r.status === 'rejected')) throw new HttpError(502, '产物长度或校验和验证失败');
    } else if (existing.size !== artifact.size) throw new HttpError(502, '存储对象大小不一致');
    const metadata = remote.metadata ?? {};
    await env.DB.batch([
      env.DB.prepare('INSERT INTO books (id,source_id,title,author,object_key,size,sha256,created_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO NOTHING').bind(job.source_id, job.source_id, String(metadata.title || `番茄小说 ${job.source_id}`).slice(0,500), String(metadata.author || '未知作者').slice(0,200), key, artifact.size, artifact.sha256.toLowerCase(), now()),
      env.DB.prepare("UPDATE jobs SET state='completed', message='已保存至 R2', lease_until=0, reserved_bytes=0, updated_at=? WHERE id=?").bind(now(), id)
    ]);
  } catch (error) {
    // Never expose source response bodies, URLs, tokens or runtime stack traces to library users.
    console.error(JSON.stringify({job_id:id, error_type:error instanceof Error ? error.name : 'unknown'}));
    const message = error instanceof HttpError ? error.message : '下载遇到网络或存储错误，请检查日志后重试';
    await env.DB.prepare("UPDATE jobs SET state='error', message=?, lease_until=0, updated_at=? WHERE id=?").bind(message, now(), id).run();
  }
}
async function route(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url), path = url.pathname, method = request.method;
  if (path === '/api/health') return json({ok:true, version:'workers-0.1'});
  if (path === '/api/v1/auth/login' && method === 'POST') {
    sameOrigin(request);
    const data = await body(request);
    if (typeof data.username !== 'string' || typeof data.password !== 'string' || data.username.length > 80 || data.password.length > 512) throw new HttpError(400, '用户名或密码无效');
    // Count every attempt before expensive password work; D1 makes the budget shared across isolates.
    const key = await hash(`${request.headers.get('CF-Connecting-IP') || 'local'}:${data.username}`), window = Math.floor(now()/900);
    const attempt = await env.DB.prepare('INSERT INTO login_attempts(key, attempts, window) VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET attempts=CASE WHEN window=excluded.window THEN attempts+1 ELSE 1 END, window=excluded.window RETURNING attempts').bind(key, window).first<{attempts:number}>();
    if (!attempt || attempt.attempts > 5) throw new HttpError(429, '登录尝试过多，请 15 分钟后再试');
    const user = await env.DB.prepare('SELECT * FROM users WHERE username=?').bind(data.username).first<{id:string;password_hash:string;username:string;role:string}>();
    if (!user) throw new HttpError(401, '用户名或密码错误');
    const verifier=env.AUTH_SERVICE.get(env.AUTH_SERVICE.idFromName(user.id));
    const verification=await verifier.fetch('https://auth.internal/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:data.password,encoded:user.password_hash})});
    if (!verification.ok || !(await verification.json() as {valid:boolean}).valid) throw new HttpError(401, '用户名或密码错误');
    const token = hex(crypto.getRandomValues(new Uint8Array(32)).buffer), csrf = crypto.randomUUID();
    await env.DB.batch([
      env.DB.prepare('INSERT INTO sessions VALUES (?,?,?,?)').bind(await hash(token), user.id, csrf, now()+604800),
      env.DB.prepare('DELETE FROM login_attempts WHERE key=?').bind(key)
    ]);
    return json({ok:true, data:{username:user.username, csrf_token:csrf, next_url:'/'}}, 200, {'Set-Cookie':`md_session=${token}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=604800`});
  }
  if (path.startsWith('/api/')) {
    const user = await identity(request, env);
    if (path === '/api/v1/auth/me' && method === 'GET') return json({ok:true,data:{id:user.user_id,username:user.username,role:user.role,csrf_token:user.csrf}});
    if (path === '/api/v1/auth/logout' && method === 'POST') { await env.DB.prepare('DELETE FROM sessions WHERE token_hash=?').bind(user.token_hash).run(); return json({ok:true},200,{'Set-Cookie':'md_session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0'}); }
    if (path === '/api/books' && method === 'GET') return json({ok:true,data:(await env.DB.prepare('SELECT id,title,author,size,created_at FROM books ORDER BY created_at DESC LIMIT 200').all()).results});
    if (path === '/api/search' && method === 'GET') {
      const q = url.searchParams.get('q')?.trim() ?? '';
      if (!q || q.length > 100) throw new HttpError(400, '请输入书名（最多 100 字）');
      return json({ok:true,data:await sourceJson(env, `/v1/novels/search?q=${encodeURIComponent(q)}&page=1`)});
    }
    if (path === '/api/jobs' && method === 'POST') {
      if (user.role !== 'admin') throw new HttpError(403, '当前下载仅开放给管理员');
      const data = await body(request), sourceId = String(data.book_id ?? '');
      if (!/^\d{8,24}$/.test(sourceId)) throw new HttpError(400, '无效番茄书籍 ID');
      const ids = data.chapter_ids ?? [];
      if (!Array.isArray(ids) || ids.length > 20 || ids.some(v => typeof v !== 'string' || !/^\d{8,24}$/.test(v))) throw new HttpError(400, '章节选择无效（最多 20 章）');
      if (await env.DB.prepare('SELECT id FROM books WHERE source_id=?').bind(sourceId).first()) throw new HttpError(409, '作品已入库；更新下载待下一阶段迁移');
      if (await env.DB.prepare("SELECT id FROM jobs WHERE source_id=? AND state IN ('queued','submitting','running')").bind(sourceId).first()) throw new HttpError(409, '已有进行中的下载');
      const id = crypto.randomUUID();
      try { await env.DB.prepare('INSERT INTO jobs (id,user_id,source_id,chapter_ids,created_at,updated_at) VALUES (?,?,?,?,?,?)').bind(id,user.user_id,sourceId,JSON.stringify(ids),now(),now()).run(); } catch { throw new HttpError(409, '下载已存在或暂时无法创建，请刷新任务列表'); }
      // D1 row is the outbox. Cron recovers if publishing to Queue fails.
      try { await env.DOWNLOADS.send({id}); } catch { /* durable outbox remains */ }
      return json({ok:true,data:{id}},202);
    }
    if (path === '/api/jobs' && method === 'GET') return json({ok:true,data:(await env.DB.prepare('SELECT id,source_id,state,message,created_at FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 30').bind(user.user_id).all()).results});
    const match = path.match(/^\/api\/books\/([\d]{8,24})\/(file|progress)$/);
    if (match) {
      const book = await env.DB.prepare('SELECT * FROM books WHERE id=?').bind(match[1]).first<{object_key:string;id:string}>();
      if (!book) throw new HttpError(404, '作品不存在');
      if (match[2] === 'file' && ['GET','HEAD'].includes(method)) {
        const obj = method === 'HEAD' ? await env.LIBRARY.head(book.object_key) : await env.LIBRARY.get(book.object_key, request.headers.has('Range') ? {range:request.headers} : undefined);
        if (!obj) throw new HttpError(404, 'R2 文件不存在');
        const headers = new Headers({'Content-Type':'application/epub+zip','Cache-Control':'private, no-store','ETag':obj.httpEtag,'Accept-Ranges':'bytes','Content-Disposition':'attachment; filename="novel.epub"'});
        let status = 200;
        if (request.headers.has('Range') && obj.range && 'offset' in obj.range && obj.range.offset !== undefined && obj.range.length !== undefined) { headers.set('Content-Range',`bytes ${obj.range.offset}-${obj.range.offset+obj.range.length-1}/${obj.size}`); headers.set('Content-Length', String(obj.range.length)); status=206; } else headers.set('Content-Length',String(obj.size));
        return new Response('body' in obj ? (obj as R2ObjectBody).body : null,{status,headers});
      }
      if (match[2] === 'progress' && method === 'GET') return json({ok:true,data:await env.DB.prepare('SELECT chapter,page,scroll,client_ms FROM progress WHERE user_id=? AND book_id=?').bind(user.user_id,book.id).first()});
      if (match[2] === 'progress' && method === 'PUT') {
        const data = await body(request);
        for (const key of ['chapter','page','scroll','client_ms']) if (!Number.isSafeInteger(data[key]) || Number(data[key]) < 0) throw new HttpError(400, '阅读进度无效');
        if (Number(data.chapter)>100000 || Number(data.page)>100000 || Number(data.scroll)>100000000 || Number(data.client_ms)>Date.now()+300000) throw new HttpError(400, '阅读进度超出范围');
        await env.DB.prepare('INSERT INTO progress VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id,book_id) DO UPDATE SET chapter=excluded.chapter,page=excluded.page,scroll=excluded.scroll,client_ms=excluded.client_ms,updated_at=excluded.updated_at WHERE excluded.client_ms>progress.client_ms').bind(user.user_id,book.id,data.chapter,data.page,data.scroll,data.client_ms,now()).run();
        return json({ok:true});
      }
    }
    throw new HttpError(404, '接口尚未迁移');
  }
  if (path === '/login') return env.ASSETS.fetch(new Request(new URL('/login.html',url), request));
  if (['/','/reader'].includes(path)) {
    try { await identity(request,env); } catch { return Response.redirect(`${url.origin}/login`,302); }
    return env.ASSETS.fetch(new Request(new URL(path === '/' ? '/index.html' : '/reader.html', url), request));
  }
  return env.ASSETS.fetch(request);
}
export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    let response: Response;
    try { response = await route(request,env); }
    catch (error) { if (!(error instanceof HttpError)) console.error(JSON.stringify({error_type:error instanceof Error ? error.name : 'unknown', frames:error instanceof Error ? error.stack?.split('\n').slice(1,5) : []})); response=json({ok:false,error:{message:error instanceof HttpError ? error.message : '服务暂时不可用'}},error instanceof HttpError ? error.status : 500); }
    const headers = new Headers(response.headers);
    headers.set('X-Content-Type-Options','nosniff'); headers.set('Referrer-Policy','same-origin'); headers.set('X-Frame-Options','DENY');
    if (new URL(request.url).pathname.startsWith('/api/') || response.headers.get('Content-Type')?.includes('text/html')) headers.set('Cache-Control','private, no-store');
    return new Response(response.body,{status:response.status,statusText:response.statusText,headers});
  },
  async queue(batch: MessageBatch<{id:string}>, env: Env) { for (const message of batch.messages) { await processJob(env,message.body.id); message.ack(); } },
  async scheduled(_controller: ScheduledController, env: Env) {
    await env.DB.batch([env.DB.prepare('DELETE FROM sessions WHERE expires_at<?').bind(now()), env.DB.prepare('DELETE FROM login_attempts WHERE window<?').bind(Math.floor(now()/900)-1), env.DB.prepare("UPDATE jobs SET state='error',message='提交状态未知，检查来源服务后重试',lease_until=0 WHERE state='submitting' AND updated_at<?").bind(now()-300)]);
    const pending=await env.DB.prepare("SELECT id FROM jobs WHERE state IN ('queued','running') AND lease_until<? AND updated_at<? LIMIT 20").bind(now(),now()-120).all<{id:string}>();
    if (pending.results.length) await env.DOWNLOADS.sendBatch(pending.results.map(body=>({body})));
  }
};
