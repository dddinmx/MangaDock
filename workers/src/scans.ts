import { HttpError, hash, now, type Env, type Identity } from "./index";
import { admin, all, one, run, reserve, type Row } from "./library";
import { enqueue } from "./downloads";
import { scanWebdav } from "./integrations";
export function r2Prefix(path: string) {
  const match = path.match(/^r2:\/\/library\/(.*)$/);
  if (!match || /[\\\r\n]/.test(match[1]) || match[1].split("/").includes(".."))
    throw new HttpError(400, "请输入 r2://library/ 加本应用 R2 内的目录前缀");
  return match[1].replace(/^\/+/, "").replace(/\/?$/, match[1] ? "/" : "");
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
  const page = await env.LIBRARY.list({
    prefix: data.prefix,
    cursor: data.cursor,
    limit: 100,
  });
  for (const object of page.objects) {
    if (!/\.(cbz|pdf|epub)$/i.test(object.key)) continue;
    const relative = object.key.slice(data.prefix.length),
      parts = relative.split("/").filter(Boolean);
    if (!parts.length) continue;
    await reserve(env, object.key, object.size);
    await run(
      env,
      "UPDATE storage_ledger SET state='stored' WHERE key=?",
      object.key,
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
        object.key,
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
        known?.id || "c_" + (await hash(data.prefix + "|" + name)).slice(0, 24),
      chapterId = "ch_" + (await hash(object.key)).slice(0, 24);
    await run(
      env,
      "INSERT INTO comics(id,name,provider,source_url,comic_format,created_at,updated_at) VALUES (?,?,'r2',?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at",
      comicId,
      name,
      "r2://library/" + object.key,
      format === "pdf" ? 1 : 2,
      now(),
      now(),
    );
    await run(
      env,
      "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,format,object_key,size,source_version,state,legacy_filename) VALUES (?,?,?,?,?,?,?,?,?,'ready',?) ON CONFLICT(id) DO UPDATE SET manifest_key=CASE WHEN source_version=excluded.source_version THEN manifest_key ELSE NULL END,size=excluded.size,source_version=excluded.source_version",
      chapterId,
      comicId,
      Number(stem.match(/^\d+/)?.[0] || 0),
      stem,
      object.key,
      format,
      object.key,
      object.size,
      version,
      parts.at(-1),
    );
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
