import { unzipSync, strFromU8, gzipSync, gunzipSync } from "fflate";
import { DOMParser } from "linkedom";
import { HttpError, now, type Env } from "./index";
import { webdavRequest } from "./integrations";
import { queueCompute } from "./compute";
import { one, run, putBytes } from "./library";
const parser = new DOMParser();
const xml = (s: string) => parser.parseFromString(s, "text/xml");
const tags = (document: any, name: string) =>
  [...document.querySelectorAll("*")].filter(
    (node: any) =>
      (node.localName || node.tagName || "").split(":").at(-1) === name,
  );
const words = (node: any): string =>
  [...node.childNodes]
    .map((child: any) =>
      child.nodeType === 3 ? child.textContent : words(child),
    )
    .join(" ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 10000);
function relative(base: string, href: string) {
  const path = new URL(href, "https://epub.invalid/" + base);
  if (path.origin !== "https://epub.invalid")
    throw new HttpError(422, "不支持 EPUB 外部正文");
  return decodeURIComponent(path.pathname.slice(1));
}
async function indexNovelNative(env: Env, id: string) {
  const book = await one(env, "SELECT * FROM books WHERE id=?", id);
  if (!book) throw new HttpError(404, "小说不存在");
  if (book.index_key) {
    const cached = await env.LIBRARY.get(book.index_key);
    if (cached) return (await cached.json()) as any;
  }
  let object: R2ObjectBody | Response | null;
  if (book.object_key.startsWith("webdav://")) {
    const match = book.object_key.match(/^webdav:\/\/([^/]+)\/(.+)$/),
      mount = await one(
        env,
        "SELECT * FROM remote_mounts WHERE id=? AND enabled=1",
        match?.[1],
      );
    if (!mount) throw new HttpError(503, "网盘已断开");
    object = await webdavRequest(env, mount, decodeURIComponent(match![2]));
    if (!object.ok) throw new HttpError(502, "网盘小说暂时不可用");
  } else object = await env.LIBRARY.get(book.object_key);
  if (!object) throw new HttpError(404, "EPUB 文件不存在");
  if (book.size > 33554432)
    throw new HttpError(413, "大 EPUB 需使用迁移工具预建章节索引");
  const raw = new Uint8Array(await object.arrayBuffer());
  let total = 0;
  const entries = unzipSync(raw, {
    filter: (file) => {
      if (!/\.(?:xml|opf|ncx|xhtml|html)$/i.test(file.name)) return false;
      total += file.originalSize;
      if (file.originalSize > 16777216 || total > 33554432)
        throw new HttpError(413, "EPUB 正文超出安全解析预算");
      return true;
    },
  });
  const text = (name: string) => {
    if (!entries[name]) throw new HttpError(422, "EPUB 缺少 " + name);
    return strFromU8(entries[name]);
  };
  const path = tags(
    xml(text("META-INF/container.xml")),
    "rootfile",
  )[0]?.getAttribute("full-path");
  if (!path) throw new HttpError(422, "EPUB 缺少 package");
  const opf = xml(text(path)),
    manifest = new Map(
      tags(opf, "item").map((item) => [item.getAttribute("id"), item]),
    );
  const spine = tags(opf, "itemref")
    .map((item) => manifest.get(item.getAttribute("idref")))
    .filter(
      (item) =>
        item?.getAttribute("media-type")?.includes("html") &&
        !item.getAttribute("properties")?.includes("nav") &&
        !item.getAttribute("href")?.toLowerCase().includes("cover"),
    );
  if (!spine.length || spine.length > 10000)
    throw new HttpError(422, "EPUB 章节数量无效");
  const nav = new Map<string, string>();
  const tocItem = [...manifest.values()].find((item) =>
    item.getAttribute("properties")?.includes("nav"),
  );
  if (tocItem) {
    const navPath = relative(path, tocItem.getAttribute("href") || "");
    if (entries[navPath])
      for (const link of xml(text(navPath)).querySelectorAll("a[href]")) {
        const href = relative(navPath, link.getAttribute("href") || "").split(
          "#",
        )[0];
        nav.set(href, link.textContent?.trim() || "");
      }
  }
  const chapters = [];
  for (const [index, item] of spine.entries()) {
    const file = relative(path, item!.getAttribute("href") || "");
    const document = xml(text(file));
    for (const node of document.querySelectorAll("script,style,noscript,svg"))
      node.remove();
    const title =
      nav.get(file) || (index === 0 ? "作品简介" : `第 ${index} 章`);
    let paragraphs = [...document.querySelectorAll("h1,h2,h3,p,blockquote,li")]
      .map(words)
      .filter(
        (text: string, i: number, items: string[]) =>
          !!text && (i === 0 || items[i - 1] !== text),
      );
    if (!paragraphs.length)
      paragraphs = (document.querySelector("body")?.textContent || "")
        .split(/\n+/)
        .map((v: string) => v.trim())
        .filter(Boolean);
    const chapter = { index, title, paragraphs, total_chapters: spine.length };
    const key = `novels/${book.id}/${book.sha256}/chapters/${index}.json.gz`;
    await putBytes(
      env,
      key,
      gzipSync(new TextEncoder().encode(JSON.stringify(chapter))),
      "application/gzip",
    );
    chapters.push({ index, title, key, gzip: true });
  }
  const coverMeta = tags(opf, "meta").find(
      (node: any) => node.getAttribute("name") === "cover",
    ),
    coverItem =
      [...manifest.values()].find((item: any) =>
        item.getAttribute("properties")?.includes("cover-image"),
      ) || manifest.get(coverMeta?.getAttribute("content"));
  if (coverItem && !book.cover_key) {
    const coverPath = relative(path, coverItem.getAttribute("href") || ""),
      coverFiles = unzipSync(raw, {
        filter: (file) =>
          file.name === coverPath && file.originalSize <= 8388608,
      });
    if (coverFiles[coverPath]) {
      const coverKey = `novels/${book.id}/${book.sha256}/cover`;
      await putBytes(
        env,
        coverKey,
        coverFiles[coverPath],
        coverItem.getAttribute("media-type") || "image/jpeg",
      );
      await run(
        env,
        "UPDATE books SET cover_key=? WHERE id=?",
        coverKey,
        book.id,
      );
      await queueCompute(env, "cover", coverKey, {
        kind: "novel",
        media_id: book.id,
      });
    }
  }
  const result = { chapters, total_chapters: chapters.length },
    key = `novels/${book.id}/${book.sha256}/index.json`;
  await putBytes(
    env,
    key,
    new TextEncoder().encode(JSON.stringify(result)),
    "application/json",
  );
  await run(
    env,
    "UPDATE books SET chapter_count=?,index_key=? WHERE id=?",
    chapters.length,
    key,
    id,
  );
  return result;
}
const active = new Map<string, Promise<any>>();
export async function indexNovel(env: Env, id: string) {
  if (active.has(id)) return active.get(id)!;
  const promise = indexNovelNative(env, id)
    .catch(async (error) => {
      if (error instanceof HttpError && error.status === 413) {
        const book = await one(env, "SELECT * FROM books WHERE id=?", id);
        if (book)
          await queueCompute(env, "index-epub", book.object_key, {
            book_id: book.id,
            sha256: book.sha256,
          });
        throw new HttpError(503, "章节索引正在由云端生成，请稍后重试");
      }
      throw error;
    })
    .finally(() => active.delete(id));
  active.set(id, promise);
  return promise;
}
export async function novelChapter(env: Env, id: string, index: number) {
  const info = await indexNovel(env, id);
  if (
    !Number.isSafeInteger(index) ||
    index < 0 ||
    index >= info.chapters.length
  )
    throw new HttpError(404, "章节不存在");
  const object = await env.LIBRARY.get(info.chapters[index].key);
  if (!object) throw new HttpError(404, "章节文件不存在");
  return info.chapters[index].gzip
    ? JSON.parse(
        strFromU8(gunzipSync(new Uint8Array(await object.arrayBuffer()))),
      )
    : object.json();
}
