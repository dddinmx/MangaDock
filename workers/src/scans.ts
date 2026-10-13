import { HttpError, hash, now, type Env, type Identity } from "./index";
import { admin, all, one, run, reserve, type Row } from "./library";
import { enqueue } from "./downloads";
import { scanWebdav } from "./integrations";
export const LIBRARY_R2_PREFIX = "r2://library/";
export const IMPORT_OBJECT_PREFIX = "imports/";
const SCAN_PATH_ERROR = "请输入 r2://library/ 加本应用 R2 内的目录前缀";
const KEY_ERROR = "对象键无效";
export function pathEscapes(value: string) {
  return (
    value.includes("\\") ||
    value.includes("\0") ||
    /[\r\n]/.test(value) ||
    value.split("/").includes("..")
  );
}
function decodedPath(value: string, message: string) {
  let current = value;
  for (let i = 0; i < 3 && current.includes("%"); i++) {
    let next = current;
    try {
      next = decodeURIComponent(current);
    } catch {
      throw new HttpError(400, message);
    }
    if (next === current) break;
    current = next;
  }
  return current;
}
function assertSafePath(value: string, message: string) {
  if (pathEscapes(value) || pathEscapes(decodedPath(value, message)))
    throw new HttpError(400, message);
}
export function r2Prefix(path: string) {
  if (typeof path !== "string" || !path.startsWith(LIBRARY_R2_PREFIX))
    throw new HttpError(400, SCAN_PATH_ERROR);
  const rest = path.slice(LIBRARY_R2_PREFIX.length);
  assertSafePath(rest, SCAN_PATH_ERROR);
  const trimmed = rest.replace(/^\/+/, "").replace(/\/+$/, "");
  if (!trimmed || trimmed.includes("//"))
    throw new HttpError(400, SCAN_PATH_ERROR);
  assertSafePath(trimmed, SCAN_PATH_ERROR);
  return trimmed + "/";
}
export function scanPrefix(prefix: unknown) {
  if (
    typeof prefix !== "string" ||
    !prefix.endsWith("/") ||
    prefix.startsWith("/") ||
    prefix.includes("//")
  )
    throw new HttpError(400, SCAN_PATH_ERROR);
  assertSafePath(prefix, SCAN_PATH_ERROR);
  if (!prefix.slice(0, -1)) throw new HttpError(400, SCAN_PATH_ERROR);
  return prefix;
}
export function libraryObjectKey(value: unknown, prefix = "") {
  if (typeof value !== "string" || !value || value.length > 4096)
    throw new HttpError(400, KEY_ERROR);
  let key = value;
  if (key.startsWith(LIBRARY_R2_PREFIX))
    key = key.slice(LIBRARY_R2_PREFIX.length);
  else if (key.startsWith("r2://") || key.includes("://") || key.startsWith("/"))
    throw new HttpError(400, KEY_ERROR);
  if (!key) throw new HttpError(400, KEY_ERROR);
  assertSafePath(key, KEY_ERROR);
  const canonical = LIBRARY_R2_PREFIX + key;
  if (!canonical.startsWith(LIBRARY_R2_PREFIX))
    throw new HttpError(400, KEY_ERROR);
  if (prefix && !key.startsWith(prefix)) throw new HttpError(400, KEY_ERROR);
  return key;
}
export function importObjectKey(value: unknown) {
  const key = libraryObjectKey(value);
  if (key.includes("/") && !key.startsWith(IMPORT_OBJECT_PREFIX))
    throw new HttpError(400, KEY_ERROR);
  return key;
}
export async function scanLibrary(env: Env, user: Identity) {
  admin(user);
  const ids = [];
  for (const row of await all(
    env,
    "SELECT value FROM settings WHERE key LIKE 'scan_path:%'",
  ))
    if (row.value.startsWith("r2://"))
      ids.push(
        await enqueue(env, user.user_id, "r2scan", {
          prefix: r2Prefix(row.value),
        }),
      );
  if (await one(env, "SELECT id FROM remote_mounts WHERE enabled=1 LIMIT 1"))
    ids.push((await scanWebdav(env, user)).task_id);
  return {
    task_ids: ids,
    message: ids.length
      ? "云端目录扫描已排队"
      : "请先添加 R2 目录或连接 WebDAV",
  };
}
export async function runR2Scan(env: Env, job: Row, data: Row) {
  const prefix = scanPrefix(data.prefix);
  const page = await env.LIBRARY.list({
    prefix,
    cursor: data.cursor,
    limit: 100,
  });
  for (const object of page.objects) {
    if (!/\.(cbz|pdf|epub)$/i.test(object.key)) continue;
    let key: string;
    try {
      key = libraryObjectKey(object.key, prefix);
    } catch (error) {
      if (error instanceof HttpError && error.status === 400) continue;
      throw error;
    }
    const relative = key.slice(prefix.length),
      parts = relative.split("/").filter(Boolean);
    if (!parts.length) continue;
    await reserve(env, key, object.size);
    await run(
      env,
      "UPDATE storage_ledger SET state='stored' WHERE key=?",
      key,
    );
    const stem = parts.at(-1)!.replace(/\.(cbz|pdf|epub)$/i, ""),
      version = object.etag;
    if (/\.epub$/i.test(object.key)) {
      const id = "n_" + (await hash(object.key)).slice(0, 24);
      await run(
        env,
        "INSERT INTO books(id,source_id,title,author,object_key,size,sha256,created_at) VALUES (?,?,?,'未知作者',?,?,?,?) ON CONFLICT(id) DO UPDATE SET size=excluded.size,sha256=excluded.sha256,index_key=CASE WHEN sha256=excluded.sha256 THEN index_key ELSE NULL END",
        id,
        id,
        stem,
        key,
        object.size,
        await hash(version),
        now(),
      );
      continue;
    }
    const name = parts.length > 1 ? parts[0] : stem,
      format = /\.pdf$/i.test(object.key) ? "pdf" : "cbz";
    const known = await one(
      env,
      "SELECT id,provider FROM comics WHERE name=?",
      name,
    );
    if (known && known.provider !== "r2") continue;
    const comicId =
        known?.id || "c_" + (await hash(prefix + "|" + name)).slice(0, 24),
      chapterId = "ch_" + (await hash(object.key)).slice(0, 24);
    await run(
      env,
      "INSERT INTO comics(id,name,provider,source_url,comic_format,created_at,updated_at) VALUES (?,?,'r2',?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at",
      comicId,
      name,
      LIBRARY_R2_PREFIX + key,
      format === "pdf" ? 1 : 2,
      now(),
      now(),
    );
    await env.DB.batch([
      env.DB.prepare(
        "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,format,object_key,size,source_version,state,legacy_filename) VALUES (?,?,?,?,?,?,?,?,?,'ready',?) ON CONFLICT(id) DO UPDATE SET manifest_key=CASE WHEN source_version=excluded.source_version THEN manifest_key ELSE NULL END,size=excluded.size,source_version=excluded.source_version",
      ).bind(
        chapterId,
        comicId,
        Number(stem.match(/^\d+/)?.[0] || 0),
        stem,
        key,
        format,
        key,
        object.size,
        version,
        parts.at(-1),
      ),
      env.DB.prepare(
        "UPDATE comics SET chapter_count=(SELECT COUNT(*) FROM chapters WHERE comic_id=? AND state='ready') WHERE id=?",
      ).bind(comicId, comicId),
    ]);
  }
  if (page.truncated)
    await enqueue(
      env,
      job.user_id,
      "r2scan",
      { ...data, cursor: page.cursor },
      job.parent_id || job.id,
    );
}
