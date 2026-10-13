import { HttpError, now, identity, type Env, type Identity } from "./index";
import { releaseReservation } from "./storage";
import { date } from "./render";
export type Row = Record<string, any>;
export const all = async (env: Env, sql: string, ...args: any[]) =>
  (
    await env.DB.prepare(sql)
      .bind(...args)
      .all<Row>()
  ).results;
export const one = async (env: Env, sql: string, ...args: any[]) =>
  env.DB.prepare(sql)
    .bind(...args)
    .first<Row>();
export const run = async (env: Env, sql: string, ...args: any[]) =>
  env.DB.prepare(sql)
    .bind(...args)
    .run();
export function admin(user: Identity) {
  if (user.role !== "admin") throw new HttpError(403, "仅管理员可操作");
}
export function downloader(user: Identity) {
  if (user.role !== "admin" && !user.can_download)
    throw new HttpError(403, "当前账号没有下载/更新权限");
}
export const groupName = (value: unknown) =>
  String(value || "")
    .replace(/[\x00-\x1f\x7f]/g, "")
    .trim()
    .replace(/\s+/g, " ")
    .slice(0, 50);
export async function setting(env: Env, key: string, fallback = "") {
  return (
    (await one(env, "SELECT value FROM settings WHERE key=?", key))?.value ??
    fallback
  );
}
export async function setSetting(env: Env, key: string, value: string) {
  await run(
    env,
    "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
    key,
    value,
  );
}
export async function canAccess(env: Env, user: Identity, comic: Row) {
  if (user.role === "admin") return true;
  return !!(await one(
    env,
    "SELECT 1 FROM group_permissions WHERE user_id=? AND group_name=?",
    user.user_id,
    comic.group_name,
  ));
}
/** Default on. NOVEL_GROUP_PERMISSIONS=off restores an open novel shelf. */
export function novelPermissionsEnforced(env: Env) {
  return (
    String(env.NOVEL_GROUP_PERMISSIONS ?? "on")
      .trim()
      .toLowerCase() !== "off"
  );
}
export async function bookById(env: Env, user: Identity, id: string) {
  const book = await one(env, "SELECT * FROM books WHERE id=?", id);
  if (!book) throw new HttpError(404, "小说不存在");
  if (
    novelPermissionsEnforced(env) &&
    user.role !== "admin" &&
    !(await one(
      env,
      "SELECT 1 FROM novel_group_permissions WHERE user_id=? AND group_name=?",
      user.user_id,
      book.group_name,
    ))
  )
    throw new HttpError(403, "当前账号未被授权访问该分组小说");
  return book;
}
export async function comicById(env: Env, user: Identity, id: string) {
  const comic = await one(
    env,
    "SELECT * FROM comics WHERE id=? OR name=?",
    id,
    id,
  );
  if (!comic) throw new HttpError(404, "漫画不存在");
  if (!(await canAccess(env, user, comic)))
    throw new HttpError(403, "当前账号未被授权访问该分组漫画");
  return comic;
}
const LIBRARY_CAP = 10000;
const LIBRARY_PAGE_DEFAULT = 60;
const LIBRARY_PAGE_MAX = 200;
export type LibraryRange = {
  limit?: number;
  offset?: number;
  cursor?: string | null;
  query?: string;
  group?: string | null;
};
const clamp = (value: number, min: number, max: number) =>
  Math.min(
    max,
    Math.max(min, Number.isFinite(value) ? Math.trunc(value) : min),
  );
export function encodeLibraryCursor(row: Row) {
  return btoa(JSON.stringify([row.updated_at, row.id]))
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/, "");
}
export function decodeLibraryCursor(value: string) {
  try {
    const pad =
      value.length % 4 === 0 ? "" : "=".repeat(4 - (value.length % 4));
    const parsed = JSON.parse(
      atob(value.replaceAll("-", "+").replaceAll("_", "/") + pad),
    );
    if (
      !Array.isArray(parsed) ||
      !Number.isSafeInteger(Number(parsed[0])) ||
      typeof parsed[1] !== "string" ||
      parsed[1].length > 200
    )
      return null;
    return { updated_at: Number(parsed[0]), id: parsed[1] };
  } catch {
    return null;
  }
}
export async function library(
  env: Env,
  user: Identity,
  includeHidden = false,
  range?: LibraryRange,
): Promise<{
  comics: Row[];
  all: Row[];
  hidden: Row[];
  total: number;
  next_cursor: string | null;
}> {
  const paging = range != null;
  const limit = paging
    ? clamp(range.limit ?? LIBRARY_PAGE_DEFAULT, 1, LIBRARY_PAGE_MAX)
    : LIBRARY_CAP;
  const cursor =
    paging && range.cursor ? decodeLibraryCursor(range.cursor) : null;
  if (paging && range.cursor && !cursor)
    throw new HttpError(400, "分页游标无效");
  const offset = cursor
    ? 0
    : paging
      ? clamp(range.offset ?? 0, 0, 1_000_000_000)
      : 0;
  const query = (range?.query || "").trim().toLowerCase().slice(0, 100);
  const group =
    range?.group && range.group !== "全部"
      ? String(range.group).slice(0, 50)
      : "";
  let where = `WHERE (?='admin' OR EXISTS(SELECT 1 FROM group_permissions gp WHERE gp.user_id=? AND gp.group_name=c.group_name))`;
  const args: unknown[] = [user.role, user.user_id];
  if (group) {
    where += " AND c.group_name=?";
    args.push(group);
  }
  if (query) {
    where += " AND instr(lower(c.name),?)";
    args.push(query);
  }
  const cursorSql = cursor
    ? " AND (c.updated_at<? OR (c.updated_at=? AND c.id<?))"
    : "";
  const cursorArgs = cursor
    ? [cursor.updated_at, cursor.updated_at, cursor.id]
    : [];
  const fetched = await all(
    env,
    `SELECT c.* FROM comics c ${where}${cursorSql} ORDER BY c.updated_at DESC, c.id DESC LIMIT ? OFFSET ?`,
    ...args,
    ...cursorArgs,
    cursor ? limit + 1 : limit,
    offset,
  );
  let comics = fetched;
  const hasExtra = Boolean(cursor && comics.length > limit);
  if (hasExtra) comics = comics.slice(0, limit);
  const total = paging
    ? Number(
        (await one(env, `SELECT COUNT(*) AS n FROM comics c ${where}`, ...args))
          ?.n || 0,
      )
    : comics.length;
  const hidden = await all(
    env,
    "SELECT kind,target FROM hidden_items WHERE user_id=?",
    user.user_id,
  );
  const filtered =
    user.role === "admin" && !includeHidden
      ? comics.filter(
          (c) =>
            !hidden.some(
              (h) =>
                (h.kind === "comic" && h.target === c.name) ||
                (h.kind === "group" && h.target === c.group_name),
            ),
        )
      : comics;
  return {
    comics: filtered.map((c): Row => ({
      ...c,
      comic_name: c.name,
      banner_url: c.banner_key ? "/comic/" + c.id + "/banner" : null,
      group: c.group_name,
      comic_format: c.comic_format || 2,
      status: "completed",
      available_chapters: c.chapter_count || 0,
      total_chapters: c.chapter_count || 0,
      completed_chapters: c.chapter_count || 0,
      cover_version: c.updated_at,
      created_at: date(c.created_at),
    })),
    all: comics,
    hidden,
    total,
    next_cursor:
      paging &&
      (cursor ? hasExtra : offset + comics.length < total) &&
      comics.at(-1)
        ? encodeLibraryCursor(comics.at(-1)!)
        : null,
  };
}
export async function groups(env: Env, user: Identity, kind = "comic") {
  return (
    await all(
      env,
      `SELECT name FROM library_groups WHERE kind=? AND (?='admin' OR ?='novel' OR EXISTS(SELECT 1 FROM ${kind === "novel" ? "novel_group_permissions" : "group_permissions"} p WHERE p.user_id=? AND p.group_name=name)) ORDER BY name!='默认分组',created_at,name`,
      kind,
      user.role,
      !novelPermissionsEnforced(env) && kind === "novel" ? kind : "",
      user.user_id,
    )
  ).map((r) => r.name as string);
}
export async function findChapter(env: Env, comicId: string, value: string) {
  const id = value.replace(/\.(cbz|pdf)$/i, "");
  const own = await one(
    env,
    "SELECT * FROM chapters WHERE id=? AND comic_id=? AND state='ready'",
    id,
    comicId,
  );
  if (own) return own;
  const alias = await one(
    env,
    "SELECT * FROM chapters WHERE comic_id=? AND public_id=? AND state='ready'",
    comicId,
    value,
  );
  return (
    alias ||
    (await one(
      env,
      "SELECT * FROM chapters WHERE comic_id=? AND legacy_filename=? AND state='ready'",
      comicId,
      value,
    ))
  );
}
export async function novels(env: Env, user: Identity): Promise<Row[]> {
  return (
    await all(
      env,
      `SELECT b.*,p.chapter,p.page,p.scroll,p.client_ms,p.updated_at AS last_read FROM books b LEFT JOIN progress p ON p.book_id=b.id AND p.user_id=? WHERE (?='admin' OR ?=0 OR EXISTS(SELECT 1 FROM novel_group_permissions gp WHERE gp.user_id=? AND gp.group_name=b.group_name)) ORDER BY p.updated_at DESC,b.created_at DESC LIMIT 10000`,
      user.user_id,
      user.role,
      novelPermissionsEnforced(env) ? 1 : 0,
      user.user_id,
    )
  ).map((b): Row => ({
    ...b,
    novel_id: b.id,
    source_book_id: b.source_id,
    title: b.title,
    group: b.group_name,
    cover_version: b.created_at,
    created_at: date(b.created_at),
    last_read_at: date(b.last_read),
    progress: b.last_read
      ? { last_chapter: b.chapter, last_page: b.page }
      : null,
    total_chapters: b.chapter_count,
    chapter_count: b.chapter_count,
    last_chapter: b.chapter || 0,
    fanqie_book_id: /^\d{8,24}$/.test(b.source_id) ? b.source_id : "",
  }));
}
export async function reserve(
  env: Env,
  key: string,
  size: number,
  owner = crypto.randomUUID(),
) {
  if (!Number.isSafeInteger(size) || size < 0)
    throw new HttpError(502, "文件长度无效");
  const existing = await one(
    env,
    "SELECT * FROM storage_ledger WHERE key=?",
    key,
  );
  if (existing) {
    if (existing.size !== size) throw new HttpError(409, "存储预留冲突");
    if (existing.state === "stored") return null;
    if (existing.owner && existing.owner !== owner)
      throw new HttpError(409, "文件正在上传，请稍后重试");
    const claimed = await env.DB.prepare(
      "UPDATE storage_ledger SET owner=? WHERE key=? AND state='reserved' AND (owner IS NULL OR owner=?) RETURNING key",
    )
      .bind(owner, key, owner)
      .first();
    if (!claimed) throw new HttpError(409, "存储预留冲突");
    return owner;
  }
  const row = await env.DB.prepare(
    `INSERT INTO storage_ledger(key,size,created_at,owner) SELECT ?,?,?,? FROM storage_totals WHERE id=1 AND book_bytes+ledger_bytes-overlap_bytes+reservation_bytes+?<=? RETURNING key`,
  )
    .bind(
      key,
      size,
      now(),
      owner,
      size,
      Number(env.MAX_LIBRARY_BYTES) || 256000000,
    )
    .first();
  if (!row) throw new HttpError(507, "书库已达到存储预算上限，未写入文件");
  return owner;
}
export async function putBytes(
  env: Env,
  key: string,
  bytes: Uint8Array,
  contentType: string,
) {
  const owner = await reserve(env, key, bytes.byteLength);
  if (!owner) return;
  try {
    await env.LIBRARY.put(key, bytes, { httpMetadata: { contentType } });
    await run(
      env,
      "UPDATE storage_ledger SET state='stored',owner=NULL WHERE key=? AND owner=?",
      key,
      owner,
    );
  } catch (error) {
    await releaseReservation(env, key, owner).catch(() => {});
    throw error;
  }
}
export async function stored(
  env: Env,
  key: string,
  request: Request,
  mime?: string,
) {
  const object =
    request.method === "HEAD"
      ? await env.LIBRARY.head(key)
      : await env.LIBRARY.get(
          key,
          request.headers.has("Range") ? { range: request.headers } : undefined,
        );
  if (!object) throw new HttpError(404, "文件不存在");
  const headers = new Headers({
    "Content-Type":
      mime || object.httpMetadata?.contentType || "application/octet-stream",
    "Cache-Control": "private, no-store",
    ETag: object.httpEtag,
    "Accept-Ranges": "bytes",
  });
  let status = 200;
  if (
    request.headers.has("Range") &&
    object.range &&
    "offset" in object.range &&
    object.range.offset !== undefined &&
    object.range.length !== undefined
  ) {
    headers.set(
      "Content-Range",
      `bytes ${object.range.offset}-${object.range.offset + object.range.length - 1}/${object.size}`,
    );
    headers.set("Content-Length", String(object.range.length));
    status = 206;
  } else headers.set("Content-Length", String(object.size));
  return new Response("body" in object ? (object as R2ObjectBody).body : null, {
    status,
    headers,
  });
}
export async function csrfIdentity(request: Request, env: Env) {
  if (
    ["GET", "HEAD"].includes(request.method) ||
    request.headers.has("X-CSRFToken") ||
    request.headers.has("Authorization")
  )
    return identity(request, env);
  const type = request.headers.get("Content-Type") || "";
  if (
    type.startsWith("application/x-www-form-urlencoded") ||
    type.startsWith("multipart/form-data")
  ) {
    const form = await request.clone().formData();
    const headers = new Headers(request.headers);
    headers.set("X-CSRFToken", String(form.get("csrf_token") || ""));
    return identity(new Request(request, { headers }), env);
  }
  return identity(request, env);
}
export async function input(request: Request) {
  const type = request.headers.get("Content-Type") || "";
  if (type.startsWith("application/json")) {
    const text = await request.text();
    if (text.length > 16384) throw new HttpError(413, "请求过大");
    try {
      const data = JSON.parse(text);
      if (!data || Array.isArray(data) || typeof data !== "object")
        throw Error();
      return data as Row;
    } catch {
      throw new HttpError(400, "无效 JSON");
    }
  }
  const form = await request.formData(),
    data: Row = {};
  for (const key of new Set([...form.keys()])) {
    const values = form.getAll(key);
    data[key] = values.length > 1 ? values : values[0];
  }
  return data;
}
export const list = (value: any) =>
  Array.isArray(value) ? value : value == null ? [] : [value];
export const safeReturn = (value: any, fallback: string) =>
  typeof value === "string" &&
  value.startsWith("/") &&
  !value.startsWith("//") &&
  !/[\\\r\n]/.test(value)
    ? value
    : fallback;
