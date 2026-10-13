import { HttpError, now, json, hex, type Env, type Identity } from "./index";
import {
  admin,
  one,
  all,
  run,
  reserve,
  stored,
  input,
  type Row,
} from "./library";
export async function uploadApi(
  request: Request,
  env: Env,
  user: Identity,
): Promise<Response | null> {
  const url = new URL(request.url);
  if (!url.pathname.startsWith("/api/admin/uploads")) return null;
  admin(user);
  if (url.pathname === "/api/admin/uploads" && request.method === "POST") {
    const data = await input(request),
      size = Number(data.size),
      sha = String(data.sha256 || "").toLowerCase(),
      mime = String(data.mime || "application/octet-stream");
    if (
      !Number.isSafeInteger(size) ||
      size < 1 ||
      size > 2147483648 ||
      !/^[a-f0-9]{64}$/.test(sha)
    )
      throw new HttpError(400, "文件长度或校验和无效");
    let key: string;
    if (data.job_id) {
      const job = await one(
        env,
        "SELECT id FROM compute_jobs WHERE id=? AND state='running'",
        data.job_id,
      );
      if (
        !job ||
        !["artifact", "original", "hero", "mobile"].includes(data.variant)
      )
        throw new HttpError(400, "计算输出无效");
      key = `compute/${job.id}/${data.variant}`;
    } else {
      const ext = String(data.extension || "").toLowerCase();
      if (!["epub", "cbz", "pdf", "jpg", "png", "webp", "json"].includes(ext))
        throw new HttpError(400, "导入格式无效");
      key = `imports/${sha}.${ext}`;
    }
    const existing = await env.LIBRARY.head(key);
    if (existing && existing.size === size)
      return json({ ok: true, data: { key, already_stored: true } });
    await reserve(env, key, size);
    const multipart = await env.LIBRARY.createMultipartUpload(key, {
        httpMetadata: { contentType: mime },
        customMetadata: { sha256: sha },
      }),
      id = crypto.randomUUID();
    await run(
      env,
      "INSERT INTO uploads(id,user_id,object_key,upload_id,size,sha256,mime,created_at) VALUES (?,?,?,?,?,?,?,?)",
      id,
      user.user_id,
      key,
      multipart.uploadId,
      size,
      sha,
      mime,
      now(),
    );
    return json({ ok: true, data: { id, key, part_size: 5242880 } });
  }
  const match = url.pathname.match(
    /^\/api\/admin\/uploads\/([^/]+)\/(parts\/(\d+)|complete|abort|file)$/,
  );
  if (!match) throw new HttpError(404, "上传接口不存在");
  const row = await one(env, "SELECT * FROM uploads WHERE id=?", match[1]);
  if (!row) throw new HttpError(404, "上传不存在");
  if (match[2] === "file" && request.method === "GET")
    return stored(env, row.object_key, request);
  if (row.state !== "uploading") throw new HttpError(409, "上传已结束");
  const multipart = env.LIBRARY.resumeMultipartUpload(
    row.object_key,
    row.upload_id,
  );
  if (match[2].startsWith("parts/") && request.method === "PUT") {
    const number = Number(match[3]);
    if (!Number.isSafeInteger(number) || number < 1 || number > 410)
      throw new HttpError(400, "分段编号无效");
    const expected = String(request.headers.get("X-Content-SHA256") || "");
    if (!/^[a-f0-9]{64}$/.test(expected))
      throw new HttpError(400, "需要分段校验和");
    const reader = request.body?.getReader(),
      parts: Uint8Array[] = [];
    let size = 0;
    if (!reader) throw new HttpError(400, "分段为空");
    for (;;) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > 5242880) {
        await reader.cancel();
        throw new HttpError(413, "分段不能超过 5 MB");
      }
      parts.push(part.value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const part of parts) {
      bytes.set(part, offset);
      offset += part.byteLength;
    }
    if (hex(await crypto.subtle.digest("SHA-256", bytes)) !== expected)
      throw new HttpError(422, "分段校验失败");
    const uploaded = await multipart.uploadPart(number, bytes);
    await run(
      env,
      "INSERT INTO upload_parts VALUES (?,?,?,?,?) ON CONFLICT(upload_id,part_number) DO UPDATE SET etag=excluded.etag,size=excluded.size,sha256=excluded.sha256",
      row.id,
      number,
      uploaded.etag,
      size,
      expected,
    );
    return json({ ok: true, data: { part_number: number, bytes: size } });
  }
  if (match[2] === "complete" && request.method === "POST") {
    const parts = await all(
      env,
      "SELECT * FROM upload_parts WHERE upload_id=? ORDER BY part_number",
      row.id,
    );
    if (
      !parts.length ||
      parts.reduce((n, p) => n + p.size, 0) !== row.size ||
      parts.some(
        (p, i) =>
          p.part_number !== i + 1 ||
          (i < parts.length - 1 && p.size !== 5242880),
      )
    )
      throw new HttpError(409, "上传分段不完整");
    await multipart.complete(
      parts.map((p) => ({ partNumber: p.part_number, etag: p.etag })),
    );
    await env.DB.batch([
      env.DB.prepare("UPDATE uploads SET state='completed' WHERE id=?").bind(
        row.id,
      ),
      env.DB.prepare(
        "UPDATE storage_ledger SET state='stored' WHERE key=?",
      ).bind(row.object_key),
    ]);
    return json({
      ok: true,
      data: { key: row.object_key, size: row.size, sha256: row.sha256 },
    });
  }
  if (match[2] === "abort" && request.method === "POST") {
    await multipart.abort();
    await env.DB.batch([
      env.DB.prepare("UPDATE uploads SET state='aborted' WHERE id=?").bind(
        row.id,
      ),
      env.DB.prepare(
        "DELETE FROM storage_ledger WHERE key=? AND state='reserved'",
      ).bind(row.object_key),
    ]);
    return json({ ok: true });
  }
  throw new HttpError(405, "不支持的上传操作");
}
