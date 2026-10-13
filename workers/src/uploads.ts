import { notifyCompute } from "./compute-scheduler";
import { assertComputeLease } from "./job-state";
import { releaseReservation, queueGarbage } from "./storage";
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
      const job = await assertComputeLease(
        env,
        String(data.job_id),
        request.headers.get("X-Compute-Lease"),
      );
      if (!["artifact", "original", "hero", "mobile"].includes(data.variant))
        throw new HttpError(400, "计算输出无效");
      key = `compute/${job.id}/${job.claim_token}/${data.variant}`;
    } else {
      const ext = String(data.extension || "").toLowerCase();
      if (!["epub", "cbz", "pdf", "jpg", "png", "webp", "json"].includes(ext))
        throw new HttpError(400, "导入格式无效");
      key = `imports/${sha}.${ext}`;
    }
    const existing = await env.LIBRARY.head(key);
    if (existing && existing.size === size)
      return json({ ok: true, data: { key, already_stored: true } });
    const id = crypto.randomUUID();
    const owner = await reserve(env, key, size, id);
    let multipart: R2MultipartUpload | undefined;
    try {
      multipart = await env.LIBRARY.createMultipartUpload(key, {
        httpMetadata: { contentType: mime },
        customMetadata: { sha256: sha },
      });
      await run(
        env,
        "INSERT INTO uploads(id,user_id,object_key,upload_id,size,sha256,mime,created_at,compute_job_id,claim_token) VALUES (?,?,?,?,?,?,?,?,?,?)",
        id,
        user.user_id,
        key,
        multipart.uploadId,
        size,
        sha,
        mime,
        now(),
        data.job_id || null,
        data.job_id ? request.headers.get("X-Compute-Lease") : null,
      );
    } catch (error) {
      if (multipart) await multipart.abort().catch(() => {});
      await releaseReservation(env, key, owner).catch(() => {});
      throw error;
    }
    await notifyCompute(env, now() + 86400);
    return json({ ok: true, data: { id, key, part_size: 5242880 } });
  }
  const match = url.pathname.match(
    /^\/api\/admin\/uploads\/([^/]+)\/(parts\/(\d+)|complete|abort|file)$/,
  );
  if (!match) throw new HttpError(404, "上传接口不存在");
  const row = await one(env, "SELECT * FROM uploads WHERE id=?", match[1]);
  if (!row) throw new HttpError(404, "上传不存在");
  if (row.compute_job_id)
    await assertComputeLease(
      env,
      row.compute_job_id,
      request.headers.get("X-Compute-Lease"),
    );
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
    if (row.compute_job_id)
      await assertComputeLease(env, row.compute_job_id, row.claim_token);
    await multipart.complete(
      parts.map((p) => ({ partNumber: p.part_number, etag: p.etag })),
    );
    if (row.compute_job_id)
      await assertComputeLease(env, row.compute_job_id, row.claim_token).catch(
        async (error) => {
          await queueGarbage(env, row.object_key);
          throw error;
        },
      );
    await env.DB.batch([
      env.DB.prepare("UPDATE uploads SET state='completed' WHERE id=?").bind(
        row.id,
      ),
      env.DB.prepare(
        "UPDATE storage_ledger SET state='stored',owner=NULL WHERE key=? AND owner=?",
      ).bind(row.object_key, row.id),
    ]);
    return json({
      ok: true,
      data: { key: row.object_key, size: row.size, sha256: row.sha256 },
    });
  }
  if (match[2] === "abort" && request.method === "POST") {
    await multipart.abort();
    await run(env, "UPDATE uploads SET state='aborted' WHERE id=?", row.id);
    await releaseReservation(env, row.object_key, row.id);
    return json({ ok: true });
  }
  throw new HttpError(405, "不支持的上传操作");
}
