import { HttpError, type Env } from "./index";
import type { Row } from "./library";

// One manifest and bounded prefix lists, never one SQL request per image.
export async function inspectChapterImages(env: Env, chapter: Row) {
  const prefix = String(chapter.manifest_key || "").replace(/[^/]*$/, "");
  if (!prefix || !chapter.page_count || chapter.page_count > 5000)
    throw new HttpError(409, "章节图片清单无效或超过完整性检查上限");
  const file = await env.LIBRARY.get(chapter.manifest_key);
  const pages = file ? await file.json<{ key: string }[]>() : [];
  if (
    !Array.isArray(pages) ||
    pages.length !== chapter.page_count ||
    pages.some(
      (page) => typeof page.key !== "string" || !page.key.startsWith(prefix),
    ) ||
    new Set(pages.map((page) => page.key)).size !== pages.length
  )
    throw new HttpError(409, "章节图片清单与页数不一致");
  const objects = new Map<string, R2Object>();
  let cursor: string | undefined;
  for (let count = 0; count < 6; count++) {
    const batch = await env.LIBRARY.list({
      prefix,
      cursor,
      limit: 1000,
      include: ["httpMetadata"],
    });
    for (const object of batch.objects) objects.set(object.key, object);
    cursor = batch.truncated ? batch.cursor : undefined;
    if (!cursor) break;
  }
  if (cursor) throw new HttpError(409, "章节存储对象超过单次检查上限");
  const missing = pages
    .filter((page) => !objects.has(page.key))
    .map((page) => page.key);
  const invalid = pages
    .filter((page) => {
      const object = objects.get(page.key);
      return (
        object &&
        (!object.size ||
          !object.httpMetadata?.contentType?.startsWith("image/"))
      );
    })
    .map((page) => page.key);
  return { keys: pages.map((page) => page.key), missing, invalid };
}
