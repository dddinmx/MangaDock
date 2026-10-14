import { HttpError, type Env } from "./index";
import { all, one } from "./library";
import { loadComic } from "./providers";

// Administrator-only, bounded, read-only verification. R2 lists avoid one SQL
// request per image, and each manifest is inspected in its own invocation.
export async function comicAudit(env: Env, id: string, query: URLSearchParams) {
  const comic = await one(
    env,
    "SELECT id,name,provider,source_url FROM comics WHERE id=?",
    id,
  );
  if (!comic) throw new HttpError(404, "漫画不存在");
  if (comic.provider === "fanqie")
    throw new HttpError(409, "小说和番茄来源不属于漫画验收范围");
  const chapterId = query.get("chapter_id");
  if (chapterId) {
    const chapter = await one(
      env,
      "SELECT id,state,page_count,manifest_key FROM chapters WHERE id=? AND comic_id=?",
      chapterId,
      id,
    );
    if (!chapter) throw new HttpError(404, "章节不存在");
    const object = chapter.manifest_key
      ? await env.LIBRARY.get(chapter.manifest_key)
      : null;
    const manifest = object ? await object.json<{ key?: string }[]>() : [];
    return { ...chapter, keys: manifest.map((page) => page.key || "") };
  }
  if (query.has("objects")) {
    const cursor = query.get("cursor") || undefined;
    if (cursor && cursor.length > 4096) throw new HttpError(400, "游标无效");
    const result = await env.LIBRARY.list({
      prefix: `comics/${id}/chapters/`,
      limit: 1000,
      cursor,
      include: ["httpMetadata"],
    });
    return {
      objects: result.objects.map((object) => ({
        key: object.key,
        size: object.size,
        type: object.httpMetadata?.contentType,
      })),
      cursor: result.truncated ? result.cursor : null,
    };
  }
  const taskId = query.get("task_id") || "";
  const task = await one(
    env,
    "SELECT id,state,completed_chapters,json_extract(payload,'$.source_cursor') AS source_cursor,json_extract(payload,'$.source_total') AS source_total FROM comic_jobs WHERE id=? AND comic_id=? AND kind='source'",
    taskId,
    id,
  );
  const chapters = await all(
    env,
    "SELECT id,ordinal,title,source_id,page_count,state,format,manifest_key FROM chapters WHERE comic_id=? ORDER BY ordinal,id LIMIT 1001",
    id,
  );
  if (chapters.length > 1000)
    throw new HttpError(413, "超过单次整部验收章节上限");
  const catalogue = query.has("catalogue")
    ? await loadComic(comic.source_url, env)
    : null;
  return {
    comic_id: id,
    title: comic.name,
    task,
    chapters,
    ...(catalogue
      ? {
          source_chapter_ids: catalogue.chapters.map((chapter) =>
            String(chapter.id),
          ),
        }
      : {}),
  };
}
