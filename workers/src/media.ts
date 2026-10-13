import { publicUser } from "./public-user";
import { queueCompute } from "./compute";
import { remoteManifest, remotePage, remoteFile } from "./remote-library";
import { Zip, ZipPassThrough } from "fflate";
import { HttpError, json, source, type Env, type Identity } from "./index";
import {
  all,
  one,
  comicById,
  bookById,
  findChapter,
  library,
  stored,
  type LibraryRange,
  type Row,
} from "./library";
import {
  serializeProgress,
  apiProgress,
  saveProgress,
  statistics,
} from "./reading";
export async function chapterManifest(env: Env, chapter: Row): Promise<Row[]> {
  const convert = async () => {
    await queueCompute(
      env,
      "archive",
      chapter.object_key ||
        `webdav://${chapter.mount_id}/${encodeURIComponent(chapter.remote_url)}`,
      {
        chapter_id: chapter.id,
        format: chapter.format,
        source_version: chapter.source_version || null,
      },
    );
    throw new HttpError(503, "章节页面正在由云端转换，请稍后重试");
  };
  if (!chapter.manifest_key && chapter.format === "pdf") return convert();
  if (
    !chapter.manifest_key &&
    (chapter.remote_url || chapter.object_key) &&
    chapter.format !== "pdf"
  )
    try {
      return await remoteManifest(env, chapter);
    } catch (error) {
      if (
        chapter.remote_url &&
        error instanceof HttpError &&
        error.status === 503
      )
        return convert();
      throw error;
    }
  const object = chapter.manifest_key
    ? await env.LIBRARY.get(chapter.manifest_key)
    : null;
  if (!object) throw new HttpError(404, "章节尚未就绪");
  return object.json() as Promise<Row[]>;
}
export function chapterSummary(comic: Row, c: Row, i: number) {
  const ext = c.format === "pdf" ? "pdf" : "cbz";
  return {
    id: c.id,
    chapter_id: c.id,
    index: i,
    title: c.title,
    filename: c.legacy_filename || c.id + "." + ext,
    format: ext,
    order: i + 1,
    url: `/static/comic/${encodeURIComponent(comic.name)}/${c.id}.${ext}`,
    file_url: `/api/comics/${comic.id}/chapters/${c.id}/file`,
    pages_url: `/api/comics/${comic.id}/chapters/${c.id}/pages`,
  };
}
export async function cbz(env: Env, chapter: Row) {
  const pages = await chapterManifest(env, chapter);
  let cancelled = false,
    ready: (() => void) | undefined;
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const zip = new Zip((error, chunk, final) => {
        if (cancelled) return;
        if (error) controller.error(error);
        else {
          controller.enqueue(chunk);
          if (final) controller.close();
        }
      });
      try {
        for (const [i, page] of pages.entries()) {
          if (cancelled) return;
          while ((controller.desiredSize || 0) < 0 && !cancelled)
            await new Promise<void>((resolve) => {
              ready = resolve;
            });
          const obj = await env.LIBRARY.get(page.key);
          if (!obj) throw Error("Missing page");
          const ext =
              (
                {
                  "image/png": "png",
                  "image/webp": "webp",
                  "image/gif": "gif",
                  "image/avif": "avif",
                  "image/bmp": "bmp",
                } as Record<string, string>
              )[obj.httpMetadata?.contentType || ""] || "jpg",
            entry = new ZipPassThrough(
              String(i + 1).padStart(5, "0") + "." + ext,
            );
          zip.add(entry);
          const reader = obj.body.getReader();
          for (;;) {
            const part = await reader.read();
            if (part.done) {
              entry.push(new Uint8Array(), true);
              break;
            }
            entry.push(part.value);
            if (cancelled) {
              await reader.cancel();
              return;
            }
          }
        }
        zip.end();
      } catch (error) {
        if (!cancelled) controller.error(error);
      }
    },
    pull() {
      ready?.();
      ready = undefined;
    },
    cancel() {
      cancelled = true;
      ready?.();
    },
  });
  return new Response(stream, {
    headers: {
      "Content-Type": "application/vnd.comicbook+zip",
      "Content-Disposition": `attachment; filename="${chapter.id}.cbz"`,
      "Cache-Control": "private, no-store",
    },
  });
}
export function apiChapter(comic: Row, c: Row, index: number) {
  const id = c.public_id || c.id;
  return {
    id,
    index,
    order: c.ordinal + 1,
    title: c.title,
    format: c.format === "pdf" ? "pdf" : "cbz",
    page_count: null,
    pages_url: `/api/comics/${comic.id}/chapters/${id}/pages`,
    resource_url: `/api/comics/${comic.id}/chapters/${id}/file`,
  };
}
export function apiComic(c: Row, progress: Row | null = null) {
  return {
    id: c.id,
    title: c.name,
    cover_url: "/static/cover/" + encodeURIComponent(c.name) + ".jpg",
    group_name: c.group_name,
    format: c.comic_format === 1 ? "pdf" : "cbz",
    chapter_count: c.available_chapters || 0,
    source_url: c.source_url || null,
    created_at: c.created_at?.timestamp
      ? new Date(c.created_at.timestamp() * 1000).toISOString()
      : new Date(c.created_at * 1000).toISOString(),
    updated_at: new Date(
      (progress?.updated_at || c.updated_at) * 1000,
    ).toISOString(),
    progress: apiProgress(progress),
  };
}
export const COVER_BYTE_LIMIT = 5 * 1024 * 1024;
const COVER_TYPES = new Set([
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/webp",
  "image/avif",
  "image/bmp",
]);
export function fanqieBookId(id: string | null | undefined) {
  if (typeof id !== "string" || !/^\d{8,24}$/.test(id))
    throw new HttpError(400, "书籍 ID 无效");
  return id;
}
export function coverContentType(header: string | null) {
  const type = (header || "").split(";", 1)[0].trim().toLowerCase();
  if (!COVER_TYPES.has(type)) throw new HttpError(502, "封面格式无效");
  return type;
}
export function coverContentLength(header: string | null) {
  if (header === null) return null;
  if (!/^\d+$/.test(header) || Number(header) > COVER_BYTE_LIMIT)
    throw new HttpError(413, "封面超过 5 MB");
  return Number(header);
}
export async function limitedCover(response: Response) {
  let type: string;
  try {
    type = coverContentType(response.headers.get("Content-Type"));
    coverContentLength(response.headers.get("Content-Length"));
  } catch (error) {
    await response.body?.cancel().catch(() => {});
    throw error;
  }
  if (!response.body) throw new HttpError(502, "封面为空");
  const reader = response.body.getReader();
  const parts: Uint8Array[] = [];
  let size = 0;
  try {
    for (;;) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > COVER_BYTE_LIMIT) {
        await reader.cancel();
        throw new HttpError(413, "封面超过 5 MB");
      }
      parts.push(part.value);
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const part of parts) {
    bytes.set(part, offset);
    offset += part.byteLength;
  }
  return new Response(bytes, {
    headers: {
      "Content-Type": type,
      "Content-Length": String(size),
      "X-Content-Type-Options": "nosniff",
      "Cache-Control": "private, no-store",
    },
  });
}
export async function mediaApi(
  request: Request,
  env: Env,
  user: Identity,
): Promise<Response | null> {
  const url = new URL(request.url),
    path = url.pathname,
    method = request.method;
  let match: RegExpMatchArray | null;
  if (
    method === "GET" &&
    (match = path.match(/^\/api\/novels\/([^/]+)\/index-status$/))
  ) {
    const book = await bookById(env, user, decodeURIComponent(match[1]));
    const job = await one(
      env,
      "SELECT state FROM compute_jobs WHERE kind='index-epub' AND input_key=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
      book.object_key,
    );
    return json({
      ok: true,
      data: { ready: !!book.index_key, error: job?.state === "error" },
    });
  }
  if ((match = path.match(/^\/comic\/([^/]+)\/banner$/))) {
    const comic = await comicById(env, user, decodeURIComponent(match[1]));
    if (!comic.banner_key) throw new HttpError(404, "横幅不存在");
    return stored(env, comic.banner_key, request);
  }
  if (
    (match = path.match(/^\/static\/cover\/(?:hero\/(mobile\/)?|)(.+)\.jpg$/))
  ) {
    const comic = await comicById(env, user, decodeURIComponent(match[2])),
      variant = path.startsWith("/static/cover/hero/")
        ? match[1]
          ? "mobile_hero_key"
          : "hero_key"
        : "cover_key",
      key = comic[variant] || comic.cover_key;
    if (!key) throw new HttpError(404, "封面尚未生成");
    return stored(env, key, request);
  }
  if (
    (match = path.match(/^\/novel\/([^/]+)\/cover$/)) &&
    ["GET", "HEAD"].includes(method)
  ) {
    const book = await bookById(env, user, decodeURIComponent(match[1]));
    if (book.cover_key) return stored(env, book.cover_key, request);
    try {
      const response = await source(
        env,
        `/v1/resources/${book.source_id}/cover`,
      );
      return new Response(response.body, {
        headers: {
          "Content-Type": response.headers.get("Content-Type") || "image/jpeg",
        },
      });
    } catch {
      return env.ASSETS.fetch(
        new Request(new URL("/static/default-comic-cover.jpg", url)),
      );
    }
  }
  if (
    (match = path.match(/^\/novels\/fanqie\/(\d{8,24})\/cover$/)) ||
    path === "/novels/fanqie/cover"
  ) {
    const id = fanqieBookId(match?.[1] || url.searchParams.get("book_id"));
    const response = await source(env, `/v1/resources/${id}/cover`);
    return limitedCover(response);
  }
  if (path === "/api/info")
    return json({
      name: "MangaDock",
      version: "1",
      supports_basic_auth: true,
      supports_page_images: true,
      page_formats: ["cbz", "pdf"],
      api_v1: "/api/v1",
    });
  if (path === "/api/auth/me") return json({ user: publicUser(user) });
  if (path === "/api/statistics")
    return json(
      await statistics(
        env,
        user,
        url.searchParams.get("year"),
        url.searchParams.get("mode") || "comic",
        true,
      ),
    );
  if (path === "/api/comics" && method === "GET") {
    const bound = (
      raw: string | null,
      fallback: number,
      min: number,
      max: number,
    ) => {
      const parsed = Number.parseInt(raw ?? "", 10);
      const value = Number.isFinite(parsed) ? parsed : fallback;
      return Math.min(max, Math.max(min, value));
    };
    const cursor = url.searchParams.get("cursor") || null;
    const explicitWindow =
      url.searchParams.has("limit") || url.searchParams.has("offset");
    const limit = explicitWindow
      ? bound(url.searchParams.get("limit"), 60, 1, 200)
      : bound(url.searchParams.get("page_size"), 60, 1, 200);
    const page = Math.max(
      1,
      Number.parseInt(url.searchParams.get("page") || "1") || 1,
    );
    const offset = cursor
      ? 0
      : explicitWindow
        ? Math.max(
            0,
            Number.parseInt(url.searchParams.get("offset") || "0") || 0,
          )
        : (page - 1) * limit;
    const range: LibraryRange = {
      limit,
      offset,
      cursor,
      query: url.searchParams.get("query") || "",
      group: url.searchParams.get("group"),
    };
    const data = await library(env, user, true, range);
    const selected = data.comics;
    const resolvedPage = explicitWindow ? Math.floor(offset / limit) + 1 : page;
    const progress = selected.length
      ? await all(
          env,
          `SELECT * FROM comic_progress WHERE user_id=? AND comic_id IN (${selected.map(() => "?").join(",")})`,
          user.user_id,
          ...selected.map((c) => c.id),
        )
      : [];
    const byComic = new Map(progress.map((row) => [row.comic_id, row]));
    return json({
      items: selected.map((comic) =>
        apiComic(comic, byComic.get(comic.id) || null),
      ),
      pagination: {
        page: resolvedPage,
        page_size: limit,
        limit,
        offset,
        total: data.total,
        has_more: data.next_cursor != null,
        next_cursor: data.next_cursor,
      },
    });
  }
  if ((match = path.match(/^\/api\/chapters\/([^/]+)$/))) {
    const comic = await comicById(env, user, decodeURIComponent(match[1])),
      chapters = await all(
        env,
        "SELECT * FROM chapters WHERE comic_id=? AND state='ready' ORDER BY ordinal,id",
        comic.id,
      );
    return json({
      chapters: chapters.map((c, i) => chapterSummary(comic, c, i)),
    });
  }
  if (
    (match = path.match(
      /^\/api\/comics\/([^/]+)(?:\/(cover(?:\.jpg|\/\d+\.jpg)?|chapters|progress))?$/,
    ))
  ) {
    const comic = await comicById(env, user, decodeURIComponent(match[1])),
      action = match[2];
    if (action?.startsWith("cover")) {
      if (!comic.cover_key) throw new HttpError(404, "封面尚未生成");
      const response = await stored(env, comic.cover_key, request);
      response.headers.set(
        "Content-Disposition",
        `inline; filename="${comic.id}.jpg"`,
      );
      return response;
    }
    const chapters = await all(
        env,
        "SELECT * FROM chapters WHERE comic_id=? AND state='ready' ORDER BY ordinal,id",
        comic.id,
      ),
      progress = await one(
        env,
        "SELECT * FROM comic_progress WHERE user_id=? AND comic_id=?",
        user.user_id,
        comic.id,
      );
    if (action === "progress") {
      if (method === "POST") {
        const data = (await request.json()) as Row;
        await saveProgress(
          env,
          user,
          {
            ...data,
            chapter: data.chapter_index ?? data.chapter,
            page: data.page_index ?? data.page,
            client_progress_ms: data.client_progress_ms || data.client_ms,
            scroll_position: data.scroll_position || data.scroll,
          },
          comic.id,
        );
        return json({
          success: true,
          progress: apiProgress(
            await one(
              env,
              "SELECT * FROM comic_progress WHERE user_id=? AND comic_id=?",
              user.user_id,
              comic.id,
            ),
          ),
        });
      }
      return json({ comic_id: comic.id, progress: apiProgress(progress) });
    }
    if (action === "chapters")
      return json({
        comic_id: comic.id,
        comic_name: comic.name,
        chapters: chapters.map((c, i) => apiChapter(comic, c, i)),
        total_chapters: chapters.length,
      });
    return json({
      item: {
        ...apiComic(
          { ...comic, available_chapters: chapters.length },
          progress,
        ),
        chapters: chapters.map((c, i) => apiChapter(comic, c, i)),
        progress: apiProgress(progress),
      },
    });
  }
  if (
    (match = path.match(
      /^\/api\/comics\/([^/]+)\/chapters\/([^/]+)\/(file|pages)(?:\/(\d+))?$/,
    ))
  ) {
    const comic = await comicById(env, user, decodeURIComponent(match[1])),
      chapter = await findChapter(env, comic.id, decodeURIComponent(match[2]));
    if (!chapter) throw new HttpError(404, "章节不存在");
    if (match[3] === "file")
      return chapter.remote_url
        ? remoteFile(env, chapter, request)
        : chapter.object_key
          ? stored(env, chapter.object_key, request)
          : cbz(env, chapter);
    const pages = await chapterManifest(env, chapter);
    if (match[4] != null) {
      const page = pages[Number(match[4])];
      if (!page) throw new HttpError(404, "页面不存在");
      return !page.key
        ? remotePage(env, chapter, page as any)
        : stored(env, page.key, request);
    }
    return json({
      comic_id: comic.id,
      chapter_id: chapter.id,
      total_pages: pages.length,
      pages: pages.map((page, i) => ({
        index: i,
        image_url: `/api/comics/${comic.id}/chapters/${chapter.id}/pages/${i}`,
      })),
    });
  }
  if ((match = path.match(/^\/api\/comic-pages\/([^/]+)\/([^/]+)$/))) {
    const comic = await comicById(env, user, decodeURIComponent(match[1])),
      chapter = await findChapter(env, comic.id, decodeURIComponent(match[2]));
    if (!chapter) throw new HttpError(404, "章节不存在");
    const pages = await chapterManifest(env, chapter);
    if (url.searchParams.has("page")) {
      const index = Number(url.searchParams.get("page"));
      if (!Number.isSafeInteger(index) || !pages[index])
        throw new HttpError(404, "页面不存在");
      return pages[index].key
        ? stored(env, pages[index].key, request)
        : remotePage(env, chapter, pages[index]);
    }
    return json({
      pages: pages.length,
      dimensions: pages.map((p) => [p.width || 0, p.height || 0]),
    });
  }
  if ((match = path.match(/^\/static\/comic\/([^/]+)\/([^/]+)$/))) {
    const comic = await comicById(env, user, decodeURIComponent(match[1])),
      chapter = await findChapter(env, comic.id, decodeURIComponent(match[2]));
    if (!chapter) throw new HttpError(404, "章节不存在");
    if (url.searchParams.has("page")) {
      const pages = await chapterManifest(env, chapter),
        page = pages[Number(url.searchParams.get("page"))];
      if (!page) throw new HttpError(404, "页面不存在");
      return !page.key
        ? remotePage(env, chapter, page as any)
        : stored(env, page.key, request);
    }
    return chapter.remote_url
      ? remoteFile(env, chapter, request)
      : chapter.object_key
        ? stored(env, chapter.object_key, request)
        : cbz(env, chapter);
  }
  return null;
}
