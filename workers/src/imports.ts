import { HttpError, now, json, type Env, type Identity } from "./index";
import { admin, one, run, input, groupName, type Row } from "./library";
import { importObjectKey } from "./scans";
export async function importApi(
  request: Request,
  env: Env,
  user: Identity,
): Promise<Response | null> {
  const url = new URL(request.url);
  if (url.pathname !== "/api/admin/import/metadata") return null;
  admin(user);
  if (request.method !== "POST") throw new HttpError(405, "请使用 POST");
  const data = await input(request);
  if (!Array.isArray(data.records) || data.records.length > 100)
    throw new HttpError(400, "迁移批次不能超过 100 行");
  const kind = String(data.kind || ""),
    results: Row[] = [];
  for (const record of data.records) {
    if (!record || typeof record !== "object")
      throw new HttpError(400, "迁移记录无效");
    if (kind === "users") {
      if (
        typeof record.username !== "string" ||
        record.username.length > 80 ||
        !/^pbkdf2:sha256:\d+\$[^$]+\$[a-f0-9]{64}$/.test(
          record.password_hash,
        ) ||
        !["admin", "user"].includes(record.role)
      )
        throw new HttpError(400, "旧用户记录无效");
      const existing = await one(
          env,
          "SELECT id,role FROM users WHERE username=?",
          record.username,
        ),
        id = existing?.id || String(record.id);
      if (existing?.role === "admin" && !data.restore_admin_password) {
        results.push({ old_id: String(record.id), id, existing: true });
        continue;
      }
      await run(
        env,
        "INSERT INTO users(id,username,password_hash,role,can_download,can_view_adult) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET password_hash=excluded.password_hash,role=excluded.role,can_download=excluded.can_download,can_view_adult=excluded.can_view_adult",
        id,
        record.username,
        record.password_hash,
        record.role,
        record.can_download ? 1 : 0,
        record.can_view_adult ? 1 : 0,
      );
      await run(
        env,
        "DELETE FROM sessions WHERE user_id=? AND user_id!=?",
        id,
        user.user_id,
      );
      results.push({ old_id: String(record.id), id });
    } else if (kind === "groups") {
      const name = groupName(record.name);
      if (!name || !["comic", "novel"].includes(record.kind))
        throw new HttpError(400, "分组无效");
      await run(
        env,
        "INSERT INTO library_groups VALUES (?,?,?) ON CONFLICT DO NOTHING",
        record.kind,
        name,
        record.created_at || now(),
      );
    } else if (kind === "permissions") {
      const mediaKind = record.kind || "comic";
      if (!["comic", "novel"].includes(mediaKind))
        throw new HttpError(400, "授权类型无效");
      const permissionTable =
        mediaKind === "novel" ? "novel_group_permissions" : "group_permissions";
      if (
        !(await one(
          env,
          "SELECT id FROM users WHERE id=?",
          String(record.user_id),
        ))
      )
        throw new HttpError(400, "用户映射不存在");
      if (
        !(await one(
          env,
          "SELECT name FROM library_groups WHERE kind=? AND name=?",
          mediaKind,
          record.group_name,
        ))
      )
        throw new HttpError(400, "分组映射不存在");
      await run(
        env,
        `INSERT INTO ${permissionTable} VALUES (?,?) ON CONFLICT DO NOTHING`,
        String(record.user_id),
        record.group_name,
      );
    } else if (kind === "comics") {
      if (
        typeof record.id !== "string" ||
        typeof record.name !== "string" ||
        record.name.length > 255
      )
        throw new HttpError(400, "漫画记录无效");
      await run(
        env,
        "INSERT INTO comics(id,name,provider,source_url,description,group_name,comic_format,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET description=excluded.description,group_name=excluded.group_name",
        record.id,
        record.name,
        record.provider || "import",
        record.source_url || "",
        record.description || "",
        record.group_name || "默认分组",
        record.comic_format || 2,
        record.created_at || 0,
        record.updated_at || now(),
      );
    } else if (kind === "chapters") {
      const key = importObjectKey(record.object_key);
      const object = await env.LIBRARY.head(key);
      if (!object || object.size !== record.size)
        throw new HttpError(409, "章节对象尚未上传或长度不一致");
      if (!["cbz", "pdf"].includes(record.format))
        throw new HttpError(400, "章节格式无效");
      await env.DB.batch([
        env.DB.prepare(
          "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,object_key,size,format,state,public_id,legacy_filename) VALUES (?,?,?,?,?,?,?,?,'ready',?,?) ON CONFLICT(id) DO UPDATE SET object_key=excluded.object_key,size=excluded.size,legacy_filename=excluded.legacy_filename",
        ).bind(
          record.id,
          record.comic_id,
          record.ordinal,
          record.title,
          record.source_id || record.legacy_filename,
          key,
          record.size,
          record.format,
          record.public_id || null,
          record.legacy_filename || null,
        ),
      ]);
    } else if (kind === "novels") {
      const key = importObjectKey(record.object_key);
      const object = await env.LIBRARY.head(key);
      if (!object || object.size !== record.size)
        throw new HttpError(409, "小说对象尚未上传或长度不一致");
      await run(
        env,
        "INSERT INTO books(id,source_id,title,author,object_key,size,sha256,created_at,description,group_name,chapter_count) VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET object_key=excluded.object_key,size=excluded.size,sha256=excluded.sha256,index_key=NULL,description=excluded.description,group_name=excluded.group_name",
        record.id,
        record.source_id || record.id,
        record.title,
        record.author || "未知作者",
        key,
        record.size,
        record.sha256,
        record.created_at || now(),
        record.description || "",
        record.group_name || "默认分组",
        record.chapter_count || 0,
      );
    } else if (kind === "covers") {
      const key = importObjectKey(record.object_key);
      const table = record.kind === "novel" ? "books" : "comics";
      if (!(await env.LIBRARY.head(key)))
        throw new HttpError(409, "封面对象不存在");
      const field = ["original", "hero", "mobile", "banner"].includes(
        record.variant,
      )
        ? (
            {
              original: "cover_key",
              hero: "hero_key",
              mobile: "mobile_hero_key",
              banner: "banner_key",
            } as Row
          )[record.variant]
        : "cover_key";
      if (table === "books" && field === "banner_key")
        throw new HttpError(400, "小说不支持漫画横幅");
      await run(
        env,
        `UPDATE ${table} SET ${field}=? WHERE id=?`,
        key,
        record.media_id,
      );
    } else if (kind === "progress") {
      const novel = record.kind === "novel",
        table = novel ? "progress" : "comic_progress",
        column = novel ? "book_id" : "comic_id";
      await run(
        env,
        `INSERT INTO ${table}(user_id,${column},chapter,page,scroll,client_ms,updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id,${column}) DO UPDATE SET chapter=excluded.chapter,page=excluded.page,scroll=excluded.scroll,client_ms=excluded.client_ms,updated_at=excluded.updated_at WHERE excluded.client_ms>${table}.client_ms`,
        String(record.user_id),
        record.media_id,
        record.chapter || 0,
        record.page || 0,
        record.scroll || 0,
        record.client_ms || 0,
        record.updated_at || now(),
      );
      if (novel)
        await run(
          env,
          "UPDATE progress SET anchor_paragraph=?,anchor_offset=? WHERE user_id=? AND book_id=?",
          record.anchor_paragraph ?? null,
          record.anchor_offset ?? null,
          String(record.user_id),
          record.media_id,
        );
    } else if (kind === "reading-time") {
      await run(
        env,
        "INSERT INTO reading_time(user_id,media_id,day,seconds,media_name) VALUES (?,?,?,?,?) ON CONFLICT(user_id,media_id,day) DO UPDATE SET seconds=MAX(seconds,excluded.seconds),media_name=excluded.media_name",
        String(record.user_id),
        record.media_id,
        record.day,
        record.seconds || 0,
        record.media_name || "",
      );
    } else if (kind === "hidden") {
      await run(
        env,
        "INSERT INTO hidden_items VALUES (?,?,?) ON CONFLICT DO NOTHING",
        String(record.user_id),
        record.kind,
        record.target,
      );
    } else if (kind === "login-logs") {
      await run(
        env,
        "INSERT INTO login_logs(username,ip_address,user_agent,success,created_at) VALUES (?,?,?,?,?)",
        record.username,
        record.ip_address || "",
        record.user_agent || "",
        record.success ? 1 : 0,
        record.created_at || now(),
      );
    } else if (kind === "settings") {
      if (!["adult_content_enabled", "comic_update_mode"].includes(record.key))
        throw new HttpError(400, "设置需通过相应配置接口迁移");
      await run(
        env,
        "INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        record.key,
        String(record.value),
      );
    } else throw new HttpError(400, "不支持的迁移记录类型");
  }
  return json({
    ok: true,
    data: { kind, imported: data.records.length, mappings: results },
  });
}
