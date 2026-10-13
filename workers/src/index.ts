import { DurableObject } from "cloudflare:workers";
import { pbkdf2 } from "@noble/hashes/pbkdf2.js";
import { sha256 } from "@noble/hashes/sha2.js";
import { scheduleUpdates } from "./updates";
import { publicUser } from "./public-user";
export { ApplicationService } from "./application";
export interface Env {
  AUTH_SERVICE: DurableObjectNamespace;
  APPLICATION: DurableObjectNamespace;
  DB: D1Database;
  LIBRARY: R2Bucket;
  DOWNLOADS: Queue<{ id: string; kind?: string }>;
  ASSETS: Fetcher;
  FANQIE_API_URL: string;
  FANQIE_API_TOKEN?: string;
  FANQIE_INSTANCE_ID?: string;
  MAX_ARTIFACT_BYTES: string;
  MAX_LIBRARY_BYTES?: string;
}
export class HttpError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
export const now = () => Math.floor(Date.now() / 1000);
export const hex = (v: ArrayBuffer) =>
  [...new Uint8Array(v)].map((x) => x.toString(16).padStart(2, "0")).join("");
export const hash = async (value: string) =>
  hex(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)));
export async function verifyPassword(password: string, encoded: string) {
  const match = /^pbkdf2:sha256:(\d+)\$([^$]{1,128})\$([a-f0-9]{64})$/.exec(
    encoded,
  );
  if (!match) return false;
  const iterations = Number(match[1]);
  if (
    !Number.isSafeInteger(iterations) ||
    iterations < 100000 ||
    iterations > 1000000
  )
    return false;
  // Durable Objects have a separate 30s CPU budget even on Free; the HTTP Worker waits on I/O.
  // Pure JS supports legacy 260k and new 600k hashes without WebCrypto's 100k ceiling.
  const actual = pbkdf2(
    sha256,
    new TextEncoder().encode(password),
    new TextEncoder().encode(match[2]),
    { c: iterations, dkLen: 32 },
  );
  let difference = 0;
  for (let i = 0; i < actual.length; i++)
    difference |= actual[i] ^ parseInt(match[3].slice(i * 2, i * 2 + 2), 16);
  return difference === 0;
}
export class PasswordVerifier extends DurableObject<Env> {
  private verified = new Map<string, number>();
  async fetch(request: Request): Promise<Response> {
    const { password, encoded, action } = (await request.json()) as {
      password: unknown;
      encoded: unknown;
      action?: string;
    };
    if (action === "hash") {
      if (
        typeof password !== "string" ||
        password.length < 6 ||
        password.length > 512
      )
        return Response.json(
          { error: "密码长度必须为 6–512 位" },
          { status: 400 },
        );
      const salt = hex(crypto.getRandomValues(new Uint8Array(16)).buffer);
      const derived = pbkdf2(
        sha256,
        new TextEncoder().encode(password),
        new TextEncoder().encode(salt),
        { c: 600000, dkLen: 32 },
      );
      return Response.json({
        encoded: `pbkdf2:sha256:600000$${salt}$${hex(derived.buffer as ArrayBuffer)}`,
      });
    }
    if (
      typeof password !== "string" ||
      password.length > 512 ||
      typeof encoded !== "string" ||
      encoded.length > 512
    )
      return Response.json({ valid: false }, { status: 400 });
    const key = await hash(encoded + "|" + password);
    if ((this.verified.get(key) || 0) > Date.now())
      return Response.json({ valid: true });
    const valid = await verifyPassword(password, encoded);
    if (valid) {
      if (this.verified.size > 500) this.verified.clear();
      this.verified.set(key, Date.now() + 300000);
    }
    return Response.json({ valid });
  }
}

export const json = (data: unknown, status = 200, headers: HeadersInit = {}) =>
  Response.json(data, { status, headers });
export async function body(request: Request): Promise<Record<string, unknown>> {
  if (!request.headers.get("content-type")?.startsWith("application/json"))
    throw new HttpError(415, "需要 JSON");
  const text = await request.text();
  if (text.length > 8192) throw new HttpError(413, "请求过大");
  try {
    const value = JSON.parse(text);
    if (!value || typeof value !== "object" || Array.isArray(value))
      throw new Error();
    return value;
  } catch {
    throw new HttpError(400, "无效 JSON");
  }
}
export function sameOrigin(request: Request) {
  if (request.headers.get("Origin") !== new URL(request.url).origin)
    throw new HttpError(403, "请求来源无效");
}
export type Identity = {
  user_id: string;
  username: string;
  role: string;
  can_download: number;
  can_view_adult: number;
  csrf: string;
  token_hash: string;
};
export async function identity(request: Request, env: Env): Promise<Identity> {
  const token = request.headers
    .get("Cookie")
    ?.match(/(?:^|;\s*)md_session=([a-f0-9]{64})(?:;|$)/)?.[1];
  const basic = request.headers.get("Authorization");
  if (basic?.startsWith("Basic ")) {
    let credentials: string;
    try {
      credentials = new TextDecoder().decode(
        Uint8Array.from(atob(basic.slice(6)), (c) => c.charCodeAt(0)),
      );
    } catch {
      throw new HttpError(401, "认证信息无效");
    }
    const split = credentials.indexOf(":");
    if (split < 1 || credentials.length > 600)
      throw new HttpError(401, "认证信息无效");
    const name = credentials.slice(0, split),
      password = credentials.slice(split + 1);
    const key = await hash("user:" + name),
      window = Math.floor(now() / 900);
    const attempt = await env.DB.prepare(
      "INSERT INTO login_attempts(key,attempts,window) VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET attempts=CASE WHEN window=excluded.window THEN attempts+1 ELSE 1 END,window=excluded.window RETURNING attempts",
    )
      .bind(key, window)
      .first<{ attempts: number }>();
    if (!attempt || attempt.attempts > 5)
      throw new HttpError(429, "登录尝试过多，请 15 分钟后重试");
    const row = await env.DB.prepare(
      "SELECT id AS user_id,username,role,can_download,can_view_adult,password_hash FROM users WHERE username=?",
    )
      .bind(name)
      .first<Identity & { password_hash: string }>();
    if (!row) throw new HttpError(401, "用户名或密码错误");
    const response = await env.AUTH_SERVICE.get(
      env.AUTH_SERVICE.idFromName(row.user_id),
    ).fetch("https://auth.internal/verify", {
      method: "POST",
      body: JSON.stringify({ password, encoded: row.password_hash }),
    });
    if (!response.ok || !((await response.json()) as { valid: boolean }).valid)
      throw new HttpError(401, "用户名或密码错误");
    await env.DB.prepare("DELETE FROM login_attempts WHERE key=?")
      .bind(key)
      .run();
    return {
      user_id: row.user_id,
      username: row.username,
      role: row.role,
      can_download: row.can_download,
      can_view_adult: row.can_view_adult,
      csrf: "",
      token_hash: "",
    };
  }
  if (!token) throw new HttpError(401, "请登录");
  const user = await env.DB.prepare(
    "SELECT s.user_id, u.username, u.role, u.can_download, u.can_view_adult, s.csrf, s.token_hash FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
  )
    .bind(await hash(token), now())
    .first<Identity>();
  if (!user) throw new HttpError(401, "请重新登录");
  if (!["GET", "HEAD"].includes(request.method)) {
    sameOrigin(request);
    if (request.headers.get("X-CSRFToken") !== user.csrf)
      throw new HttpError(403, "CSRF 校验失败");
  }
  return user;
}
export async function source(env: Env, path: string, init: RequestInit = {}) {
  if (!env.FANQIE_API_TOKEN || !env.FANQIE_INSTANCE_ID)
    throw new HttpError(503, "番茄服务凭据尚未配置");
  // Paths come only from this adapter. Never forward credentials to a redirect or an artifact-supplied host.
  if (
    !path.startsWith("/v1/") ||
    path.includes("..") ||
    path.includes("\\") ||
    path.includes("#")
  )
    throw new HttpError(502, "来源路径无效");
  const base = new URL(env.FANQIE_API_URL);
  if (base.protocol !== "https:")
    throw new HttpError(503, "来源必须使用 HTTPS");
  const response = await fetch(new URL(path, base), {
    ...init,
    redirect: "manual",
    signal: AbortSignal.timeout(path.endsWith("/artifact") ? 300000 : 25000),
    headers: {
      Authorization: `Bearer ${env.FANQIE_API_TOKEN}`,
      "X-MangaDock-Instance-ID": env.FANQIE_INSTANCE_ID,
      "X-MangaDock-Version": "5.0",
      "X-MangaDock-System": "Cloudflare/Workers",
      "User-Agent": "MangaDock/5.0 Workers/0.1",
      "Content-Type": "application/json",
    },
  });
  if (!response.ok) {
    await response.body?.cancel();
    throw new HttpError(502, `番茄服务 HTTP ${response.status}`);
  }
  return response;
}
export async function sourceJson(
  env: Env,
  path: string,
  init: RequestInit = {},
) {
  const response = await source(env, path, init);
  // Metadata is small; EPUB bytes never enter JS heap.
  const payload = (await response.json()) as {
    ok: boolean;
    data: Record<string, any>;
  };
  if (payload.ok !== true || !payload.data || typeof payload.data !== "object")
    throw new HttpError(502, "来源响应无效");
  return payload.data;
}
type Job = {
  id: string;
  source_id: string;
  chapter_ids: string;
  remote_id: string | null;
  state: string;
  attempts: number;
  created_at: number;
  reserved_bytes: number;
};
export async function processJob(env: Env, id: string) {
  const lease = await env.DB.prepare(
    "UPDATE jobs SET lease_until=?, updated_at=? WHERE id=? AND state IN ('queued','running') AND lease_until<? RETURNING *",
  )
    .bind(now() + 300, now(), id, now())
    .first<Job>();
  if (!lease) return;
  let job = lease;
  try {
    if (now() - job.created_at > 10800 || job.attempts >= 180)
      throw new HttpError(502, "远端任务超时，请检查来源服务后重试");
    if (!job.remote_id) {
      // A crash after POST acceptance cannot be safely retried without upstream idempotency support.
      await env.DB.prepare(
        "UPDATE jobs SET state='submitting', updated_at=? WHERE id=?",
      )
        .bind(now(), id)
        .run();
      const remote = await sourceJson(env, "/v1/jobs", {
        method: "POST",
        body: JSON.stringify({
          target: job.source_id,
          kind: "novel",
          format: "epub",
          chapter_ids: JSON.parse(job.chapter_ids),
        }),
      });
      if (
        typeof remote.id !== "string" ||
        !/^[a-zA-Z0-9_-]{1,100}$/.test(remote.id)
      )
        throw new HttpError(502, "来源任务 ID 无效");
      job.remote_id = remote.id;
      await env.DB.prepare(
        "UPDATE jobs SET state='running', remote_id=?, updated_at=? WHERE id=?",
      )
        .bind(remote.id, now(), id)
        .run();
    }
    const remote = await sourceJson(env, `/v1/jobs/${job.remote_id}`);
    if (["queued", "running"].includes(remote.status)) {
      await env.DB.prepare(
        "UPDATE jobs SET attempts=attempts+1, state='running', lease_until=0, updated_at=?, message='来源服务生成 EPUB 中' WHERE id=?",
      )
        .bind(now(), id)
        .run();
      await env.DOWNLOADS.send({ id }, { delaySeconds: 60 });
      return;
    }
    if (remote.status !== "completed")
      throw new HttpError(502, "来源任务未成功完成");
    const artifact = remote.artifact;
    const max = Math.min(
      Number(env.MAX_ARTIFACT_BYTES) || 2147483648,
      2147483648,
    );
    if (
      !artifact ||
      !Number.isSafeInteger(artifact.size) ||
      artifact.size <= 0 ||
      artifact.size > max ||
      !/^[a-f0-9]{64}$/i.test(artifact.sha256) ||
      artifact.url !== `/v1/jobs/${job.remote_id}/artifact`
    )
      throw new HttpError(502, "产物格式或大小超出限制（2 GB）");
    const key = `novels/${job.source_id}/${artifact.sha256.toLowerCase()}.epub`;
    if (!job.reserved_bytes) {
      const reserved = await env.DB.prepare(
        "UPDATE jobs SET reserved_bytes=? WHERE id=? AND reserved_bytes=0 AND (SELECT COALESCE(SUM(size),0) FROM books WHERE object_key NOT LIKE 'webdav://%')+(SELECT COALESCE(SUM(size),0) FROM storage_ledger WHERE key NOT IN (SELECT object_key FROM books))+(SELECT COALESCE(SUM(reserved_bytes),0) FROM jobs)+?<=? RETURNING id",
      )
        .bind(
          artifact.size,
          id,
          artifact.size,
          Number(env.MAX_LIBRARY_BYTES) || 256000000,
        )
        .first();
      if (!reserved)
        throw new HttpError(507, "书库已达到免费预算上限；此次下载未保存");
    }
    const existing = await env.LIBRARY.head(key);
    if (!existing) {
      const response = await source(env, artifact.url);
      if (!response.body) throw new HttpError(502, "产物为空");
      if (
        response.headers.has("content-length") &&
        Number(response.headers.get("content-length")) !== artifact.size
      ) {
        await response.body.cancel();
        throw new HttpError(502, "产物长度不一致");
      }
      const fixed = new FixedLengthStream(artifact.size);
      // R2 checks SHA-256; FixedLengthStream rejects under/overflow. No unzip/hash loop in Workers CPU budget.
      const transfer = response.body.pipeTo(fixed.writable);
      const results = await Promise.allSettled([
        transfer,
        env.LIBRARY.put(key, fixed.readable, {
          sha256: artifact.sha256.toLowerCase(),
          httpMetadata: { contentType: "application/epub+zip" },
        }),
      ]);
      if (results.some((r) => r.status === "rejected"))
        throw new HttpError(502, "产物长度或校验和验证失败");
    } else if (existing.size !== artifact.size)
      throw new HttpError(502, "存储对象大小不一致");
    const metadata = remote.metadata ?? {};
    await env.DB.batch([
      env.DB.prepare(
        "INSERT INTO books (id,source_id,title,author,object_key,size,sha256,created_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET title=excluded.title,author=excluded.author,object_key=excluded.object_key,size=excluded.size,sha256=excluded.sha256,index_key=NULL,chapter_count=0",
      ).bind(
        job.source_id,
        job.source_id,
        String(metadata.title || `番茄小说 ${job.source_id}`).slice(0, 500),
        String(metadata.author || "未知作者").slice(0, 200),
        key,
        artifact.size,
        artifact.sha256.toLowerCase(),
        now(),
      ),
      env.DB.prepare(
        "UPDATE jobs SET state='completed', message='已保存至 R2', lease_until=0, reserved_bytes=0, updated_at=? WHERE id=?",
      ).bind(now(), id),
    ]);
  } catch (error) {
    // Never expose source response bodies, URLs, tokens or runtime stack traces to library users.
    console.error(
      JSON.stringify({
        job_id: id,
        error_type: error instanceof Error ? error.name : "unknown",
      }),
    );
    const message =
      error instanceof HttpError
        ? error.message
        : "下载遇到网络或存储错误，请检查日志后重试";
    await env.DB.prepare(
      "UPDATE jobs SET state='error', message=?, lease_until=0, updated_at=? WHERE id=?",
    )
      .bind(message, now(), id)
      .run();
  }
}
async function pilotRouteUnsafe(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url),
    path = url.pathname,
    method = request.method;
  if (path === "/static/cover/cover.png") return env.ASSETS.fetch(request);
  if (path === "/api/info")
    return json({
      name: "MangaDock",
      version: "1",
      supports_basic_auth: true,
      supports_page_images: true,
      page_formats: ["cbz", "pdf"],
      api_v1: "/api/v1",
    });
  if (path === "/api/v1" || path === "/api/v1/")
    return json({ ok: true, data: { name: "MangaDock", version: "1" } });
  if (path === "/login" && method === "GET")
    return env.APPLICATION.get(env.APPLICATION.idFromName("web")).fetch(
      request,
    );
  if ((path === "/login" || path === "/api/auth/login") && method === "POST") {
    const data =
      path === "/login"
        ? Object.fromEntries(await request.formData())
        : await body(request);
    const headers = new Headers(request.headers);
    headers.set("Content-Type", "application/json");
    return pilotRoute(
      new Request(new URL("/api/v1/auth/login", url), {
        method: "POST",
        headers,
        body: JSON.stringify(data),
      }),
      env,
    );
  }
  if (path === "/api/health") return json({ ok: true, version: "workers-0.1" });
  if (path === "/api/v1/auth/login" && method === "POST") {
    sameOrigin(request);
    const data = await body(request);
    if (
      typeof data.username !== "string" ||
      typeof data.password !== "string" ||
      data.username.length > 80 ||
      data.password.length > 512
    )
      throw new HttpError(400, "用户名或密码无效");
    const loginLog = await env.DB.prepare(
      "INSERT INTO login_logs(username,ip_address,user_agent,success,created_at) VALUES (?,?,?,0,?) RETURNING id",
    )
      .bind(
        data.username,
        (request.headers.get("CF-Connecting-IP") || "").slice(0, 45),
        (request.headers.get("User-Agent") || "").slice(0, 500),
        now(),
      )
      .first<{ id: number }>();
    // Count every attempt before expensive password work; D1 makes the budget shared across isolates.
    const key = await hash(`user:${data.username}`),
      ipKey = await hash(
        `ip:${request.headers.get("CF-Connecting-IP") || "local"}`,
      ),
      window = Math.floor(now() / 900);
    for (const [budgetKey, limit] of [
      [key, 5],
      [ipKey, 30],
    ] as const) {
      const attempt = await env.DB.prepare(
        "INSERT INTO login_attempts(key, attempts, window) VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET attempts=CASE WHEN window=excluded.window THEN attempts+1 ELSE 1 END, window=excluded.window RETURNING attempts",
      )
        .bind(budgetKey, window)
        .first<{ attempts: number }>();
      if (!attempt || attempt.attempts > limit)
        throw new HttpError(429, "登录尝试过多，请 15 分钟后再试");
    }
    const user = await env.DB.prepare("SELECT * FROM users WHERE username=?")
      .bind(data.username)
      .first<{
        id: string;
        password_hash: string;
        username: string;
        role: string;
      }>();
    if (!user) throw new HttpError(401, "用户名或密码错误");
    const verifier = env.AUTH_SERVICE.get(env.AUTH_SERVICE.idFromName(user.id));
    const verification = await verifier.fetch("https://auth.internal/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        password: data.password,
        encoded: user.password_hash,
      }),
    });
    if (
      !verification.ok ||
      !((await verification.json()) as { valid: boolean }).valid
    )
      throw new HttpError(401, "用户名或密码错误");
    if (loginLog)
      await env.DB.prepare("UPDATE login_logs SET success=1 WHERE id=?")
        .bind(loginLog.id)
        .run();
    const token = hex(crypto.getRandomValues(new Uint8Array(32)).buffer),
      csrf = crypto.randomUUID();
    await env.DB.batch([
      env.DB.prepare("INSERT INTO sessions VALUES (?,?,?,?)").bind(
        await hash(token),
        user.id,
        csrf,
        now() + 604800,
      ),
      env.DB.prepare("DELETE FROM login_attempts WHERE key IN (?,?)").bind(
        key,
        ipKey,
      ),
    ]);
    const next =
      typeof data.next === "string" &&
      data.next.startsWith("/") &&
      !data.next.startsWith("//") &&
      !/[\\\r\n]/.test(data.next)
        ? data.next
        : "/";
    return json(
      {
        ok: true,
        user: publicUser(user),
        data: {
          next,
          username: user.username,
          user: publicUser(user),
          csrf_token: csrf,
          next_url:
            typeof data.next === "string" &&
            data.next.startsWith("/") &&
            !data.next.startsWith("//") &&
            !/[\\\r\n]/.test(data.next)
              ? data.next
              : "/",
        },
      },
      200,
      {
        "Set-Cookie": `md_session=${token}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=604800`,
      },
    );
  }
  if (path.startsWith("/api/")) {
    const user = await identity(request, env);
    if (path === "/api/v1/auth/me" && method === "GET")
      return json({
        ok: true,
        data: {
          id: publicUser(user).id,
          username: user.username,
          role: user.role,
          csrf_token: user.csrf,
          user: publicUser(user),
        },
      });
    if (path === "/api/v1/auth/logout" && method === "POST") {
      await env.DB.prepare("DELETE FROM sessions WHERE token_hash=?")
        .bind(user.token_hash)
        .run();
      return json({ ok: true }, 200, {
        "Set-Cookie":
          "md_session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0",
      });
    }
    if (path === "/api/source/register" && method === "POST") {
      if (user.role !== "admin")
        throw new HttpError(403, "仅管理员可初始化来源");
      if (env.FANQIE_API_TOKEN) throw new HttpError(409, "来源已配置");
      const data = await body(request);
      if (
        typeof data.instance !== "string" ||
        !/^[a-zA-Z0-9_-]{8,64}$/.test(data.instance) ||
        typeof data.registration !== "string" ||
        !/^fqr_[a-zA-Z0-9_-]{32,128}$/.test(data.registration)
      )
        throw new HttpError(400, "来源初始化凭据格式无效");
      const base = new URL(env.FANQIE_API_URL);
      if (base.protocol !== "https:")
        throw new HttpError(503, "来源必须使用 HTTPS");
      const response = await fetch(new URL("/v1/register", base), {
        method: "POST",
        redirect: "manual",
        signal: AbortSignal.timeout(25000),
        headers: {
          "X-MangaDock-Instance-ID": data.instance,
          "X-MangaDock-Registration-Key": data.registration,
          "X-MangaDock-Version": "5.0",
          "X-MangaDock-System": "Cloudflare/Workers",
          Accept: "application/json",
          "User-Agent": "MangaDock/5.0 Fanqie-Client/1.1",
        },
      });
      if (!response.ok) {
        await response.body?.cancel();
        throw new HttpError(502, `来源边缘注册 HTTP ${response.status}`);
      }
      const payload = (await response.json()) as {
        ok: boolean;
        data?: { token?: string };
      };
      if (
        !payload.ok ||
        !/^fqk_[a-zA-Z0-9_-]{32,128}$/.test(payload.data?.token || "")
      )
        throw new HttpError(502, "来源注册响应无效");
      return json({ ok: true, data: { token: payload.data!.token } });
    }
    if (path === "/api/books" && method === "GET")
      return json({
        ok: true,
        data: (
          await env.DB.prepare(
            "SELECT id,title,author,size,created_at FROM books ORDER BY created_at DESC LIMIT 200",
          ).all()
        ).results,
      });
    if (path === "/api/search" && method === "GET") {
      const q = url.searchParams.get("q")?.trim() ?? "";
      if (!q || q.length > 100)
        throw new HttpError(400, "请输入书名（最多 100 字）");
      return json({
        ok: true,
        data: await sourceJson(
          env,
          `/v1/novels/search?q=${encodeURIComponent(q)}&page=1`,
        ),
      });
    }
    if (path === "/api/jobs" && method === "POST") {
      if (user.role !== "admin" && !user.can_download)
        throw new HttpError(403, "当前账号没有下载权限");
      const data = await body(request),
        sourceId = String(data.book_id ?? "");
      if (!/^\d{8,24}$/.test(sourceId))
        throw new HttpError(400, "无效番茄书籍 ID");
      const ids = data.chapter_ids ?? [];
      if (
        !Array.isArray(ids) ||
        ids.length > 20 ||
        ids.some((v) => typeof v !== "string" || !/^\d{8,24}$/.test(v))
      )
        throw new HttpError(400, "章节选择无效（最多 20 章）");
      if (
        !data.update &&
        (await env.DB.prepare("SELECT id FROM books WHERE source_id=?")
          .bind(sourceId)
          .first())
      )
        throw new HttpError(409, "作品已入库；更新下载待下一阶段迁移");
      if (
        await env.DB.prepare(
          "SELECT id FROM jobs WHERE source_id=? AND state IN ('queued','submitting','running')",
        )
          .bind(sourceId)
          .first()
      )
        throw new HttpError(409, "已有进行中的下载");
      const id = crypto.randomUUID();
      try {
        const created = await env.DB.prepare(
          "INSERT INTO jobs (id,user_id,source_id,chapter_ids,created_at,updated_at) SELECT ?,?,?,?,?,? WHERE (?=1 OR NOT EXISTS (SELECT 1 FROM books WHERE source_id=?)) RETURNING id",
        )
          .bind(
            id,
            user.user_id,
            sourceId,
            JSON.stringify(ids),
            now(),
            now(),
            data.update ? 1 : 0,
            sourceId,
          )
          .first();
        if (!created) throw new Error("Already in library");
      } catch {
        throw new HttpError(409, "下载已存在或暂时无法创建，请刷新任务列表");
      }
      // D1 row is the outbox. Cron recovers if publishing to Queue fails.
      try {
        await env.DOWNLOADS.send({ id });
      } catch {
        /* durable outbox remains */
      }
      return json({ ok: true, data: { id } }, 202);
    }
    if (path === "/api/jobs" && method === "GET")
      return json({
        ok: true,
        data: (
          await env.DB.prepare(
            "SELECT id,source_id,state,message,created_at FROM jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 30",
          )
            .bind(user.user_id)
            .all()
        ).results,
      });
    const match = path.match(/^\/api\/books\/([^/]+)\/(file|progress)$/);
    if (match) {
      const book = await env.DB.prepare("SELECT * FROM books WHERE id=?")
        .bind(decodeURIComponent(match[1]))
        .first<{ object_key: string; id: string }>();
      if (!book) throw new HttpError(404, "作品不存在");
      if (match[2] === "file" && ["GET", "HEAD"].includes(method)) {
        const obj =
          method === "HEAD"
            ? await env.LIBRARY.head(book.object_key)
            : await env.LIBRARY.get(
                book.object_key,
                request.headers.has("Range")
                  ? { range: request.headers }
                  : undefined,
              );
        if (!obj) throw new HttpError(404, "R2 文件不存在");
        const headers = new Headers({
          "Content-Type": "application/epub+zip",
          "Cache-Control": "private, no-store",
          ETag: obj.httpEtag,
          "Accept-Ranges": "bytes",
          "Content-Disposition": 'attachment; filename="novel.epub"',
        });
        let status = 200;
        if (
          request.headers.has("Range") &&
          obj.range &&
          "offset" in obj.range &&
          obj.range.offset !== undefined &&
          obj.range.length !== undefined
        ) {
          headers.set(
            "Content-Range",
            `bytes ${obj.range.offset}-${obj.range.offset + obj.range.length - 1}/${obj.size}`,
          );
          headers.set("Content-Length", String(obj.range.length));
          status = 206;
        } else headers.set("Content-Length", String(obj.size));
        return new Response("body" in obj ? (obj as R2ObjectBody).body : null, {
          status,
          headers,
        });
      }
      if (match[2] === "progress" && method === "GET")
        return json({
          ok: true,
          data: await env.DB.prepare(
            "SELECT chapter,page,scroll,client_ms FROM progress WHERE user_id=? AND book_id=?",
          )
            .bind(user.user_id, book.id)
            .first(),
        });
      if (match[2] === "progress" && method === "PUT") {
        const data = await body(request);
        for (const key of ["chapter", "page", "scroll", "client_ms"])
          if (!Number.isSafeInteger(data[key]) || Number(data[key]) < 0)
            throw new HttpError(400, "阅读进度无效");
        if (
          Number(data.chapter) > 100000 ||
          Number(data.page) > 100000 ||
          Number(data.scroll) > 100000000 ||
          Number(data.client_ms) > Date.now() + 300000
        )
          throw new HttpError(400, "阅读进度超出范围");
        await env.DB.prepare(
          "INSERT INTO progress(user_id,book_id,chapter,page,scroll,client_ms,updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id,book_id) DO UPDATE SET chapter=excluded.chapter,page=excluded.page,scroll=excluded.scroll,client_ms=excluded.client_ms,updated_at=excluded.updated_at WHERE excluded.client_ms>progress.client_ms",
        )
          .bind(
            user.user_id,
            book.id,
            data.chapter,
            data.page,
            data.scroll,
            data.client_ms,
            now(),
          )
          .run();
        return json({ ok: true });
      }
    }
    return env.APPLICATION.get(env.APPLICATION.idFromName("web")).fetch(
      request,
    );
  }
  if (path === "/login")
    return env.ASSETS.fetch(new Request(new URL("/login.html", url), request));
  if (path === "/reader") {
    try {
      await identity(request, env);
    } catch {
      return Response.redirect(`${url.origin}/login`, 302);
    }
    return env.ASSETS.fetch(new Request(new URL("/reader.html", url), request));
  }
  if (
    (path.startsWith("/static/") &&
      !path.startsWith("/static/comic/") &&
      !path.startsWith("/static/cover/")) ||
    /^\/(?:login|reader|api|index)\.js$/.test(path) ||
    path === "/reader-runtime.js"
  )
    return env.ASSETS.fetch(request);
  return env.APPLICATION.get(env.APPLICATION.idFromName("web")).fetch(request);
}
export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    let response: Response;
    try {
      const path = new URL(request.url).pathname;
      if (path === "/api/health")
        response = json({
          ok: true,
          version: "workers-0.1",
          phase: "full-migration",
        });
      else if (
        (path.startsWith("/static/") &&
          !path.startsWith("/static/comic/") &&
          !path.startsWith("/static/cover/")) ||
        path === "/static/cover/cover.png" ||
        ["/api.js", "/login.js", "/reader.js", "/reader-runtime.js"].includes(
          path,
        )
      )
        response = await env.ASSETS.fetch(request);
      else
        response = await env.APPLICATION.get(
          env.APPLICATION.idFromName("web"),
        ).fetch(request);
    } catch (error) {
      if (!(error instanceof HttpError))
        console.error(
          JSON.stringify({
            error_type: error instanceof Error ? error.name : "unknown",
            frames:
              error instanceof Error
                ? error.stack?.split("\n").slice(1, 5)
                : [],
          }),
        );
      response = json(
        {
          ok: false,
          error: {
            message:
              error instanceof HttpError ? error.message : "服务暂时不可用",
          },
        },
        error instanceof HttpError ? error.status : 500,
      );
    }
    const headers = new Headers(response.headers);
    headers.set("X-Content-Type-Options", "nosniff");
    headers.set("Referrer-Policy", "same-origin");
    headers.set("X-Frame-Options", "DENY");
    if (
      new URL(request.url).pathname.startsWith("/api/") ||
      response.headers.get("Content-Type")?.includes("text/html")
    )
      headers.set("Cache-Control", "private, no-store");
    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers,
    });
  },
  async queue(batch: MessageBatch<{ id: string; kind?: string }>, env: Env) {
    for (const message of batch.messages) {
      if (message.body.kind === "comic") {
        const result = await env.APPLICATION.get(
          env.APPLICATION.idFromName("jobs"),
        ).fetch("https://jobs.internal/run", {
          method: "POST",
          body: JSON.stringify({ id: message.body.id }),
        });
        if (!result.ok) {
          message.retry({ delaySeconds: 60 });
          continue;
        }
      } else {
        const result = await env.APPLICATION.get(
          env.APPLICATION.idFromName("jobs"),
        ).fetch("https://jobs.internal/run-novel", {
          method: "POST",
          body: JSON.stringify({ id: message.body.id }),
        });
        if (!result.ok) {
          message.retry({ delaySeconds: 60 });
          continue;
        }
      }
      message.ack();
    }
  },
  async scheduled(_controller: ScheduledController, env: Env) {
    const result = await env.APPLICATION.get(
      env.APPLICATION.idFromName("jobs"),
    ).fetch("https://jobs.internal/maintenance", { method: "POST" });
    if (!result.ok) throw new Error("Maintenance failed");
  },
};
export async function maintenance(env: Env) {
  await scheduleUpdates(env);
  await env.DB.batch([
    env.DB.prepare("DELETE FROM sessions WHERE expires_at<?").bind(now()),
    env.DB.prepare("DELETE FROM login_attempts WHERE window<?").bind(
      Math.floor(now() / 900) - 1,
    ),
    env.DB.prepare(
      "UPDATE jobs SET state='error',message='提交状态未知，检查来源服务后重试',lease_until=0 WHERE state='submitting' AND updated_at<?",
    ).bind(now() - 300),
  ]);
  const pending = await env.DB.prepare(
    "SELECT id FROM jobs WHERE state IN ('queued','running') AND lease_until<? AND updated_at<? LIMIT 20",
  )
    .bind(now(), now() - 120)
    .all<{ id: string }>();
  await env.DB.prepare(
    "UPDATE comic_jobs SET state='error',lease_until=0,message='提交状态未知，请检查来源后重试' WHERE state='submitting' AND updated_at<?",
  )
    .bind(now() - 300)
    .run();
  const comicPending = await env.DB.prepare(
    "SELECT id FROM comic_jobs WHERE state IN ('queued','running') AND lease_until<? AND updated_at<? LIMIT 20",
  )
    .bind(now(), now() - 60)
    .all<{ id: string }>();
  if (comicPending.results.length)
    await env.DOWNLOADS.sendBatch(
      comicPending.results.map((job) => ({
        body: { id: job.id, kind: "comic" },
      })),
    );
  if (pending.results.length)
    await env.DOWNLOADS.sendBatch(pending.results.map((body) => ({ body })));
}

export async function pilotRoute(
  request: Request,
  env: Env,
): Promise<Response> {
  try {
    const path = new URL(request.url).pathname;
    const methods: Record<string, string[]> = {
      "/api/v1/auth/login": ["POST"],
      "/api/auth/login": ["POST"],
      "/login": ["POST"],
      "/api/v1/auth/logout": ["POST"],
      "/api/v1/auth/me": ["GET"],
      "/api/source/register": ["POST"],
      "/api/books": ["GET"],
      "/api/search": ["GET"],
      "/api/jobs": ["GET", "POST"],
      "/api/info": ["GET"],
      "/api/v1": ["GET"],
      "/api/v1/": ["GET"],
    };
    const allowed =
      methods[path] ||
      (/\/file$/.test(path)
        ? ["GET", "HEAD"]
        : /\/progress$/.test(path)
          ? ["GET", "PUT"]
          : []);
    if (!allowed.includes(request.method))
      throw new HttpError(405, "不支持的请求方法");
    return await pilotRouteUnsafe(request, env);
  } catch (error) {
    if (!(error instanceof HttpError))
      console.error(
        JSON.stringify({
          component: "pilot-api",
          error_type: error instanceof Error ? error.name : "unknown",
        }),
      );
    return json(
      {
        ok: false,
        error: {
          message:
            error instanceof HttpError ? error.message : "服务暂时不可用",
        },
      },
      error instanceof HttpError ? error.status : 500,
    );
  }
}
