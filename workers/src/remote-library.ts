import { Inflate } from "fflate";
import { HttpError, type Env } from "./index";
import { one, run, putBytes, type Row } from "./library";
import { webdavRequest } from "./integrations";
const crcTable = Uint32Array.from({ length: 256 }, (_, n) => {
  for (let i = 0; i < 8; i++) n = (n >>> 1) ^ (n & 1 ? 0xedb88320 : 0);
  return n >>> 0;
});
const crc32 = (bytes: Uint8Array) => {
  let crc = 0xffffffff;
  for (const byte of bytes) crc = (crc >>> 8) ^ crcTable[(crc ^ byte) & 255];
  return (crc ^ 0xffffffff) >>> 0;
};
async function range(env: Env, chapter: Row, start: number, end: number) {
  if (chapter.object_key) {
    const object = await env.LIBRARY.get(chapter.object_key, {
      range: { offset: start, length: end - start + 1 },
    });
    if (!object) throw new HttpError(404, "章节文件不存在");
    return new Uint8Array(await object.arrayBuffer());
  }
  const mount = await one(
    env,
    "SELECT * FROM remote_mounts WHERE id=? AND enabled=1",
    chapter.mount_id,
  );
  if (!mount) throw new HttpError(503, "网盘已断开");
  const response = await webdavRequest(env, mount, chapter.remote_url, "GET", {
    Range: `bytes=${start}-${end}`,
    ...(chapter.source_version?.startsWith('"')
      ? { "If-Match": chapter.source_version }
      : {}),
  });
  if (response.status === 412)
    throw new HttpError(409, "章节已更新，请重新同步网盘");
  if (response.status !== 206) {
    await response.body?.cancel();
    throw new HttpError(
      503,
      "网盘暂不支持分段读取，请使用云端导入工具建立章节索引",
    );
  }
  const buffer = new Uint8Array(await response.arrayBuffer());
  if (buffer.length !== end - start + 1)
    throw new HttpError(502, "网盘分段长度不一致");
  return buffer;
}
export async function remoteManifest(env: Env, chapter: Row) {
  const size = Number(chapter.size);
  if (!Number.isSafeInteger(size) || size < 22)
    throw new HttpError(422, "远端章节长度无效");
  const tail = await range(env, chapter, Math.max(0, size - 65557), size - 1),
    view = new DataView(tail.buffer, tail.byteOffset, tail.byteLength);
  let eocd = -1;
  for (let i = tail.length - 22; i >= 0; i--)
    if (
      view.getUint32(i, true) === 0x06054b50 &&
      i + 22 + view.getUint16(i + 20, true) === tail.length
    ) {
      eocd = i;
      break;
    }
  if (eocd < 0) throw new HttpError(422, "CBZ 中央目录不存在");
  if (view.getUint16(eocd + 4, true) || view.getUint16(eocd + 6, true))
    throw new HttpError(422, "不支持多卷 CBZ");
  let count = view.getUint16(eocd + 10, true),
    length = view.getUint32(eocd + 12, true),
    offset = view.getUint32(eocd + 16, true);
  if (count === 65535 || length === 4294967295 || offset === 4294967295) {
    if (eocd < 20 || view.getUint32(eocd - 20, true) !== 0x07064b50)
      throw new HttpError(422, "ZIP64 目录缺失");
    const position = Number(view.getBigUint64(eocd - 12, true)),
      header = await range(env, chapter, position, position + 55),
      v = new DataView(header.buffer);
    if (v.getUint32(0, true) !== 0x06064b50)
      throw new HttpError(422, "ZIP64 头无效");
    count = Number(v.getBigUint64(32, true));
    length = Number(v.getBigUint64(40, true));
    offset = Number(v.getBigUint64(48, true));
  }
  if (count > 100000 || length > 16777216 || offset + length > size)
    throw new HttpError(413, "CBZ 目录超出解析预算");
  const directory = await range(env, chapter, offset, offset + length - 1),
    v = new DataView(directory.buffer);
  const pages: Row[] = [];
  let cursor = 0;
  for (let i = 0; i < count; i++) {
    if (
      cursor + 46 > directory.length ||
      v.getUint32(cursor, true) !== 0x02014b50
    )
      throw new HttpError(422, "CBZ 目录损坏");
    const flags = v.getUint16(cursor + 8, true),
      method = v.getUint16(cursor + 10, true),
      crc = v.getUint32(cursor + 16, true),
      nameLength = v.getUint16(cursor + 28, true),
      extraLength = v.getUint16(cursor + 30, true),
      commentLength = v.getUint16(cursor + 32, true);
    let compressed = v.getUint32(cursor + 20, true),
      original = v.getUint32(cursor + 24, true),
      local = v.getUint32(cursor + 42, true);
    const name = new TextDecoder().decode(
        directory.subarray(cursor + 46, cursor + 46 + nameLength),
      ),
      extra = directory.subarray(
        cursor + 46 + nameLength,
        cursor + 46 + nameLength + extraLength,
      );
    if ([compressed, original, local].includes(4294967295)) {
      const ev = new DataView(extra.buffer, extra.byteOffset, extra.byteLength);
      for (let at = 0; at + 4 <= extra.length;) {
        const id = ev.getUint16(at, true),
          len = ev.getUint16(at + 2, true);
        if (id === 1) {
          let pos = at + 4;
          if (original === 4294967295) {
            original = Number(ev.getBigUint64(pos, true));
            pos += 8;
          }
          if (compressed === 4294967295) {
            compressed = Number(ev.getBigUint64(pos, true));
            pos += 8;
          }
          if (local === 4294967295) local = Number(ev.getBigUint64(pos, true));
          break;
        }
        at += 4 + len;
      }
    }
    cursor += 46 + nameLength + extraLength + commentLength;
    if (
      !/\.(jpe?g|png|webp|gif|avif|bmp)$/i.test(name) ||
      name.startsWith("__MACOSX/")
    )
      continue;
    if (
      flags & 1 ||
      ![0, 8].includes(method) ||
      original > 20971520 ||
      compressed > 20971520 ||
      local + 30 + compressed > size
    )
      throw new HttpError(422, "CBZ 图片格式或长度无效");
    pages.push({ name, offset: local, compressed, original, method, crc });
  }
  const collator = new Intl.Collator("en", { numeric: true });
  pages.sort((a, b) => collator.compare(a.name, b.name));
  if (!pages.length || pages.length > 10000)
    throw new HttpError(422, "CBZ 图片数量无效");
  const key = `webdav/${chapter.comic_id}/${chapter.id}/${encodeURIComponent(chapter.source_version || String(size))}/manifest.json`;
  await putBytes(
    env,
    key,
    new TextEncoder().encode(JSON.stringify(pages)),
    "application/json",
  );
  await run(
    env,
    "UPDATE chapters SET manifest_key=?,page_count=? WHERE id=?",
    key,
    pages.length,
    chapter.id,
  );
  return pages;
}
export function boundedInflate(compressed: Uint8Array, expected: number) {
  if (!Number.isSafeInteger(expected) || expected < 1 || expected > 20971520)
    throw new HttpError(422, "CBZ 图片长度无效");
  const output = new Uint8Array(expected);
  let size = 0;
  const decoder = new Inflate((chunk) => {
    size += chunk.length;
    if (size > expected) throw new HttpError(422, "CBZ 图片超过解压上限");
    output.set(chunk, size - chunk.length);
  });
  // Small input chunks bound each inflate step, including a forged directory size.
  try {
    for (let offset = 0; offset < compressed.length; offset += 256)
      decoder.push(
        compressed.subarray(offset, offset + 256),
        offset + 256 >= compressed.length,
      );
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw new HttpError(422, "CBZ 图片解压失败");
  }
  if (size !== expected) throw new HttpError(422, "CBZ 图片长度不一致");
  return output;
}
export async function remotePage(env: Env, chapter: Row, page: Row) {
  const header = await range(env, chapter, page.offset, page.offset + 29),
    view = new DataView(header.buffer);
  if (view.getUint32(0, true) !== 0x04034b50)
    throw new HttpError(422, "CBZ 本地文件头损坏");
  const start =
      page.offset + 30 + view.getUint16(26, true) + view.getUint16(28, true),
    compressed = await range(env, chapter, start, start + page.compressed - 1),
    bytes =
      page.method === 8
        ? boundedInflate(compressed, page.original)
        : compressed;
  if (bytes.length !== page.original || crc32(bytes) !== page.crc)
    throw new HttpError(422, "CBZ 图片校验失败");
  const mime = /\.png$/i.test(page.name)
    ? "image/png"
    : /\.webp$/i.test(page.name)
      ? "image/webp"
      : /\.gif$/i.test(page.name)
        ? "image/gif"
        : /\.avif$/i.test(page.name)
          ? "image/avif"
          : /\.bmp$/i.test(page.name)
            ? "image/bmp"
            : "image/jpeg";
  return new Response(bytes, {
    headers: { "Content-Type": mime, "Cache-Control": "private, no-store" },
  });
}
export async function remoteFile(env: Env, chapter: Row, request: Request) {
  const mount = await one(
    env,
    "SELECT * FROM remote_mounts WHERE id=? AND enabled=1",
    chapter.mount_id,
  );
  if (!mount) throw new HttpError(503, "网盘已断开");
  const response = await webdavRequest(
    env,
    mount,
    chapter.remote_url,
    request.method,
    {
      ...(request.headers.has("Range")
        ? { Range: request.headers.get("Range")! }
        : {}),
    },
  );
  if (!response.ok) throw new HttpError(502, "网盘文件暂时不可用");
  const headers = new Headers(response.headers);
  headers.delete("Set-Cookie");
  headers.delete("WWW-Authenticate");
  headers.set("Cache-Control", "private, no-store");
  return new Response(response.body, { status: response.status, headers });
}
