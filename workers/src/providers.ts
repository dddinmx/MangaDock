import { parseHTML } from "linkedom";
import LZString from "lz-string";
import { HttpError, sourceJson, type Env } from "./index";
import { remote, remoteUrl } from "./security";
export interface SourceChapter {
  order: number;
  title: string;
  id: string;
  url?: string;
  mid?: number;
  hid?: string;
  book_id?: string;
}
export interface ComicSource {
  provider: string;
  title: string;
  description: string;
  cover: string | null;
  chapters: SourceChapter[];
  adult?: boolean;
  mid?: number;
}
export const UA =
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36";
export const clean = (name: string) =>
  (name || "未知漫画")
    .replace(/[\\/:*?"<>|\x00-\x1f]/g, "_")
    .trim()
    .slice(0, 200);
const FROM = "_-9876543210abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ",
  TO = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
export function decodeImages(
  encoded: string,
  prefix = "J7r",
  inner = "kD",
  split = "W4s",
  suffix = "nQ",
) {
  if (
    typeof encoded !== "string" ||
    !encoded.startsWith(prefix) ||
    !encoded.endsWith(suffix)
  )
    throw new HttpError(502, "章节图片数据格式无效");
  const body = encoded.slice(prefix.length, -suffix.length),
    len = body.length - inner.length - split.length,
    tail = Math.floor(len / 3),
    lead = Math.floor((len - tail) / 2),
    middle = len - tail - lead;
  if (
    len < 1 ||
    body.slice(lead, lead + inner.length) !== inner ||
    body.slice(
      lead + inner.length + middle,
      lead + inner.length + middle + split.length,
    ) !== split
  )
    throw new HttpError(502, "章节图片校验失败");
  const mixed =
    body.slice(-tail) +
    body.slice(0, lead) +
    body.slice(lead + inner.length, lead + inner.length + middle);
  let restored = "";
  for (let i = 0; i < mixed.length; i += 7) {
    const chunk = mixed.slice(i, i + 7);
    restored += (i / 7) % 2 ? [...chunk].reverse().join("") : chunk;
  }
  const translated = [...restored]
    .map((c) => (FROM.includes(c) ? TO[FROM.indexOf(c)] : c))
    .join("")
    .replaceAll("-", "+")
    .replaceAll("_", "/");
  const data = JSON.parse(
    new TextDecoder().decode(
      Uint8Array.from(
        atob(translated + "=".repeat((4 - (translated.length % 4)) % 4)),
        (c) => c.charCodeAt(0),
      ),
    ),
  );
  if (!Array.isArray(data)) throw new HttpError(502, "章节图片不是列表");
  return data;
}
export function providerFor(raw: string) {
  if (/^(?:fanqie-comic:\/\/)?\d{8,24}$/.test(raw)) return "fanqie";
  const url = remoteUrl(raw),
    host = url.hostname.replace(/^www\./, "");
  if (
    ["fanqienovel.com", "changdunovel.com"].some(
      (domain) => host === domain || host.endsWith("." + domain),
    )
  )
    return "fanqie";
  if (host === "baozimh.org" && /^\/manga\/[^/?#]+\/?$/.test(url.pathname))
    return "baozimh_org";
  if (["m.hipmh.com", "hipmh.com", "hipmh.top"].includes(host)) return "hipmh";
  if (
    ["manhuagui.com", "m.manhuagui.com"].includes(host) &&
    /^\/comic\/\d+(?:\/\d+\.html)?\/?$/.test(url.pathname)
  )
    return "manhuagui";
  if (["mxs12.cc", "wzd1.cc"].includes(host)) return "mxs";
  if (
    host === "guazimanhua.com" &&
    url.pathname === "/comic.php" &&
    /^\d+$/.test(url.searchParams.get("id") || "")
  )
    return "guazimanhua";
  if (host === "kalamanhua.com" && /^\/comic\/\d+\.html\/?$/.test(url.pathname))
    return "kalamanhua";
  if (["baozimh.com", "cn.baozimhcn.com", "baozimhcn.com"].includes(host))
    return "baozimhcn";
  throw new HttpError(400, "不支持的漫画来源链接");
}
export async function page(url: string, referer = url) {
  const response = await remote(url, {
    headers: { "User-Agent": UA, Referer: referer },
  });
  if (!response.ok) {
    await response.body?.cancel();
    throw new HttpError(502, `来源页面 HTTP ${response.status}`);
  }
  const text = await response.text();
  if (text.length > 8388608) throw new HttpError(502, "来源页面过大");
  return { text, document: parseHTML(text).document };
}
const api = async (url: string) => {
  const response = await remote(url, {
    headers: { "User-Agent": UA, Referer: "https://baozimh.org/" },
  });
  if (!response.ok)
    throw new HttpError(502, `来源接口 HTTP ${response.status}`);
  return response.json() as Promise<any>;
};
function description(document: any) {
  return (
    document
      .querySelector('meta[property="og:description"],meta[name="description"]')
      ?.getAttribute("content") ||
    document
      .querySelector(".detail-content,.book-intro,.comic-intro,.book-detail")
      ?.textContent?.trim() ||
    ""
  );
}
function anchors(
  document: any,
  selector: string,
  base: string,
  filter: (url: URL) => boolean,
) {
  const seen = new Set<string>();
  return [...document.querySelectorAll(selector)]
    .map((a: any) => ({
      url: new URL(a.getAttribute("href") || "", base),
      title: a.textContent?.trim() || "",
    }))
    .filter(
      (item) =>
        filter(item.url) &&
        !seen.has(item.url.href) &&
        !!seen.add(item.url.href),
    );
}
export async function loadComic(url: string, env?: Env): Promise<ComicSource> {
  const provider = providerFor(url);
  if (provider === "fanqie") {
    if (!env) throw new HttpError(503, "番茄来源未配置");
    const result = await sourceJson(env, "/v1/resources/resolve", {
        method: "POST",
        body: JSON.stringify({
          target: url.replace(/^fanqie-comic:\/\//, ""),
          kind: "comic",
        }),
      }),
      metadata = result.metadata || {};
    if (
      !/^\d{8,24}$/.test(String(metadata.book_id || "")) ||
      !Array.isArray(result.chapters) ||
      !result.chapters.length
    )
      throw new HttpError(502, "番茄漫画目录无效");
    return {
      provider,
      title: clean(metadata.title || metadata.book_id),
      description: String(metadata.description || ""),
      cover: null,
      chapters: result.chapters.map((ch: any, i: number) => ({
        order: i,
        title: ch.title || `第${i + 1}话`,
        id: String(ch.id),
        book_id: String(metadata.book_id),
      })),
    };
  }
  if (provider === "manhuagui") {
    const target = new URL(url);
    target.pathname =
      "/comic/" + /\/comic\/(\d+)/.exec(target.pathname)![1] + "/";
    url = target.href;
  }
  const { text, document } = await page(url);
  let title = clean(
      document.querySelector("h1")?.textContent ||
        document.querySelector("title")?.textContent ||
        "",
    ),
    cover =
      document
        .querySelector('meta[property="og:image"],meta[name="og:image"]')
        ?.getAttribute("content") ||
      document
        .querySelector(".stui-content__thumb img,.book-cover img")
        ?.getAttribute("data-original") ||
      null;
  let chapters: SourceChapter[] = [];
  let desc = description(document);
  if (provider === "baozimh_org") {
    const mid = Number(/data-mid="(\d+)"/.exec(text)?.[1]);
    if (!mid) throw new HttpError(502, "缺少作品 ID");
    const result = await api(
      `https://v2.apikk.top/api/manga/get?mid=${mid}&mode=all`,
    );
    if (result.code !== 200) throw new HttpError(502, "来源信息失败");
    title = clean(result.data.title);
    cover = result.data.cover;
    desc = result.data.desc || result.data.description || "";
    chapters = [...result.data.chapters]
      .sort(
        (a: any, b: any) =>
          Number(a.attributes?.order || 0) - Number(b.attributes?.order || 0),
      )
      .map((c: any, i: number) => ({
        order: i,
        title: c.attributes?.title || `第${i + 1}话`,
        id: String(c.id),
        mid,
      }));
  } else if (provider === "hipmh") {
    const node = document.querySelector("#chapters-config"),
      mid = node?.getAttribute("data-mid");
    if (!mid) throw new HttpError(502, "嬉皮漫画缺少作品 ID");
    title = clean(node.getAttribute("data-title") || title);
    cover = node.getAttribute("data-cover") || cover;
    for (let p = 1; p <= 1000; p++) {
      const result = await api(
        `https://hipapi1.s3file.top/v1/manga/chapters?mid=${encodeURIComponent(mid)}&page=${p}&per_page=100&order=asc`,
      );
      if (result.code !== 200) throw new HttpError(502, "嬉皮目录接口错误");
      const items = result.data.items || [];
      for (const item of items)
        if (item.hid)
          chapters.push({
            order: chapters.length,
            title: item.title || `第${chapters.length + 1}话`,
            id: String(item.hid),
            hid: String(item.hid),
          });
      if (!items.length || p >= result.data.total_pages) break;
    }
  } else if (provider === "mxs") {
    chapters = anchors(
      document,
      "ul#detail-list-select li a",
      url,
      (u) => u.origin === new URL(url).origin,
    ).map((a, i) => ({
      order: i,
      title: a.title || `第${i + 1}章`,
      id: a.url.href,
      url: a.url.href,
    }));
    const parts = new URL(url).pathname.split("/").filter(Boolean);
    cover = new URL(
      `/static/upload/book/${parts[parts.indexOf("book") + 1] || parts.at(-1)}/cover.jpg`,
      url,
    ).href;
  } else if (provider === "guazimanhua") {
    const links = anchors(
      document,
      "[data-chapter-list] a[href],a[href]",
      url,
      (u) =>
        u.hostname === new URL(url).hostname &&
        u.pathname === "/chapter.php" &&
        /^\d+$/.test(u.searchParams.get("id") || ""),
    );
    chapters = links.reverse().map((a, i) => ({
      order: i,
      title: a.title,
      id: a.url.href,
      url: a.url.href,
    }));
    for (const node of document.querySelectorAll(
      'script[type="application/ld+json"]',
    )) {
      try {
        const data = JSON.parse(node.textContent || "");
        if (data["@type"] === "ComicStory") {
          title = clean(data.name || title);
          cover = data.image || cover;
        }
      } catch {}
    }
  } else if (provider === "kalamanhua") {
    title = title
      .split(/\s*[_\-|｜]\s*/)[0]
      .replace("漫画免费在线免费下拉观看", "");
    const comicId = /\/comic\/(\d+)/.exec(url)![1],
      grouped = new Map<number, any[]>();
    for (const a of anchors(
      document,
      '[id^="playlist"] a[href],a[href]',
      url,
      (u) => u.pathname.startsWith("/chapter/"),
    )) {
      const match = /\/chapter\/(\d+)-(\d+)-(\d+)\.html/.exec(a.url.pathname);
      if (match && match[1] === comicId) {
        const sid = Number(match[2]);
        if (!grouped.has(sid)) grouped.set(sid, []);
        grouped.get(sid)!.push({ ...a, nid: Number(match[3]) });
      }
    }
    const best = [...grouped].sort(
      (a, b) => b[1].length - a[1].length || a[0] - b[0],
    )[0];
    chapters = (best?.[1] || [])
      .sort((a, b) => a.nid - b.nid)
      .map((a, i) => ({
        order: i,
        title: a.title,
        id: a.url.href,
        url: a.url.href,
      }));
  } else if (provider === "manhuagui") {
    const bookId = /\/comic\/(\d+)/.exec(url)![1];
    chapters = anchors(document, "a[href]", url, (u) =>
      new RegExp(`^/comic/${bookId}/\\d+\\.html$`).test(u.pathname),
    )
      .sort(
        (a, b) =>
          Number(/\/(\d+)\.html$/.exec(a.url.pathname)![1]) -
          Number(/\/(\d+)\.html$/.exec(b.url.pathname)![1]),
      )
      .map((a, i) => ({
        order: i,
        title: a.title,
        id: a.url.href,
        url: a.url.href,
      }));
  } else {
    const comicId = new URL(url).pathname.split("/").filter(Boolean).at(-1),
      slots = new Map<number, string>();
    for (const a of document.querySelectorAll("a[href]")) {
      const u = new URL(a.getAttribute("href") || "", url);
      if (
        u.pathname === "/user/page_direct" &&
        u.searchParams.get("comic_id") === comicId &&
        u.searchParams.get("section_slot") === "0"
      ) {
        const slot = Number(u.searchParams.get("chapter_slot"));
        if (Number.isSafeInteger(slot))
          slots.set(slot, a.textContent?.trim() || `第${slot + 1}章`);
      }
    }
    if (!slots.size)
      for (const match of text.matchAll(
        /\/comic\/chapter\/[^/]+\/0_(\d+)\.html/g,
      ))
        slots.set(Number(match[1]), `第${Number(match[1]) + 1}章`);
    chapters = [...slots]
      .sort((a, b) => a[0] - b[0])
      .map(([slot, name], i) => {
        const href = new URL(`/comic/chapter/${comicId}/0_${slot}.html`, url)
          .href;
        return { order: i, title: name, id: href, url: href };
      });
  }
  if (!chapters.length)
    throw new HttpError(502, "未解析到有效章节，已停止下载");
  if (chapters.length > 10000)
    throw new HttpError(413, "来源章节超过单次任务上限");
  return {
    provider,
    title,
    description: desc.slice(0, 10000),
    cover: cover ? new URL(cover, url).href : null,
    chapters,
    adult: ["mxs", "kalamanhua"].includes(provider),
  };
}
export function unpackManhuagui(html: string) {
  const params = /',(\d+),(\d+),'/.exec(html);
  if (!params) throw new HttpError(502, "漫画柜缺少 packer 参数");
  const start = html.indexOf("}('") + 3,
    payload = html.slice(start, params.index),
    rest = html.slice(params.index + params[0].length),
    encoded = rest.slice(0, rest.lastIndexOf("'["));
  const words = (LZString.decompressFromBase64(encoded) || "").split("|"),
    base = Number(params[1]);
  const token = (n: number): string =>
    n < base
      ? n > 35
        ? String.fromCharCode(n + 29)
        : n.toString(36)
      : token(Math.floor(n / base)) + token(n % base);
  const dictionary = new Map(words.map((word, i) => [token(i), word]));
  const unpacked = payload.replace(/\b\w+\b/g, (v) => dictionary.get(v) || v),
    json = /\{[\s\S]*\}/.exec(unpacked)?.[0];
  if (!json) throw new HttpError(502, "漫画柜解包失败");
  return JSON.parse(json);
}
export async function chapterImages(provider: string, chapter: SourceChapter) {
  let images: string[] = [];
  if (provider === "baozimh_org") {
    const info = await api(
      `https://v2.apikk.top/api/v2/chapter/getinfo?m=${chapter.mid}&c=${encodeURIComponent(chapter.id)}`,
    );
    if (info.code !== 200) throw new HttpError(502, "章节接口失败");
    const data = info.data?.info?.images || {},
      raw =
        typeof data.images === "string"
          ? decodeImages(data.images)
          : data.images,
      host =
        Number(data.line) === 2
          ? "https://c-nd2-1.6wm.top"
          : "https://c-nd3-1.6wm.top";
    images = [...raw]
      .sort((a: any, b: any) => Number(a.order || 0) - Number(b.order || 0))
      .map((x: any) => new URL(x.url, host).href);
  } else if (provider === "hipmh") {
    const { document } = await page(
        `https://reader.hipmh.top/chapter/${encodeURIComponent(chapter.hid || chapter.id)}`,
      ),
      attr = (key: string) =>
        document.querySelector(`[${key}]`)?.getAttribute(key),
      hid = attr("data-api-hid");
    if (!hid) throw new HttpError(502, "嬉皮章节标识缺失");
    const apiBase = (
        attr("data-api-base-url-line1") || "https://hipapi1.s3file.top"
      ).replace(/\/v1\/?$/, ""),
      result = await api(
        `${apiBase}/v2/chapter?hid=${encodeURIComponent(hid)}`,
      ),
      data = result.data || {};
    images =
      typeof data.images === "string"
        ? decodeImages(data.images, "qM9", "Vx", "pL0", "Z7")
        : data.images;
    const sid = Number(data.sid),
      order = Number(data.order_id),
      total = images.length;
    if (
      Number.isInteger(sid) &&
      sid >= 0 &&
      sid <= 4294967295 &&
      Number.isInteger(order) &&
      order >= 0 &&
      order <= 4294967295
    ) {
      const index =
        order ^
        Number(
          ((BigInt(sid) * BigInt(((40503 << 16) | 31153) >>> 0)) ^
            (BigInt(total) * BigInt(((34283 << 16) | 51819) >>> 0))) %
            BigInt(total),
        );
      if (index >= 0 && index < total) images.splice(index, 1);
    }
    const base =
      Number(data.line) === 9
        ? attr("data-chapter-img-base-line1s") ||
          attr("data-chapter-img-base-line1")
        : attr("data-chapter-img-base-line1");
    images = images.map(
      (v) => new URL(v, base || "https://hip-tx-1.s3imgs.top").href,
    );
  } else {
    const { text, document } = await page(chapter.url || chapter.id);
    if (provider === "manhuagui") {
      const config = unpackManhuagui(text);
      images = config.files.map(
        (file: string) =>
          `https://us.hamreus.com${config.path}${file}?e=${config.sl.e}&m=${config.sl.m}`,
      );
    } else if (provider === "kalamanhua") {
      const player = JSON.parse(
        /var\s+player_aaaa\s*=\s*(\{[\s\S]*?\})\s*;/.exec(text)?.[1] || "{}",
      );
      if (!player.url) throw new HttpError(502, "卡拉播放地址缺失");
      const url = new URL(
        player.url.startsWith("/play/")
          ? player.url
          : /^[a-f0-9]+$/i.test(player.url)
            ? "/play/min/" + player.url
            : player.url,
        "https://image.kalaimg.top",
      ).href;
      const reader = await page(url, chapter.url);
      images = [...reader.document.querySelectorAll("img.comic-img")].map(
        (node) =>
          new URL(
            node.getAttribute("data-src") || node.getAttribute("src") || "",
            url,
          ).href,
      );
    } else {
      const selector =
        provider === "mxs"
          ? "img.lazy"
          : provider === "guazimanhua"
            ? "img.reading-image"
            : "img.comic-contain__item,img[data-src]";
      images = [...document.querySelectorAll(selector)]
        .sort(
          (a, b) =>
            Number(a.getAttribute("data-page") || 0) -
            Number(b.getAttribute("data-page") || 0),
        )
        .map(
          (node) =>
            new URL(
              node.getAttribute("data-original") ||
                node.getAttribute("data-src") ||
                node.getAttribute("src") ||
                "",
              chapter.url || chapter.id,
            ).href,
        );
    }
  }
  if (!images.length || images.length > 10000)
    throw new HttpError(502, "章节图片数量无效");
  images.forEach(remoteUrl);
  return images;
}
