import { parseHTML, DOMParser } from "linkedom";
import { HttpError, hash, now, type Env, type Identity } from "./index";
import {
  one,
  all,
  run,
  comicById,
  admin,
  setting,
  setSetting,
  type Row,
} from "./library";
import { encrypt, decrypt, remote, remoteUrl } from "./security";
import { enqueue } from "./downloads";
export async function anilistQuery(
  query: string,
  variables: Row = {},
  token?: string,
) {
  const response = await fetch("https://graphql.anilist.co", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: "Bearer " + token } : {}),
    },
    body: JSON.stringify({ query, variables }),
    signal: AbortSignal.timeout(25000),
  });
  const data = (await response.json()) as Row;
  if (!response.ok || data.errors)
    throw new HttpError(502, "AniList 请求失败，请检查连接后重试");
  return data.data;
}
export async function anilistSearch(query: string) {
  return (
    await anilistQuery(
      "query($search:String){Page(perPage:20){media(search:$search,type:MANGA){id title{userPreferred romaji native} description chapters coverImage{large} status}}}",
      { search: query },
    )
  ).Page.media;
}
export async function anilistAction(
  env: Env,
  user: Identity,
  action: string,
  data: Row,
  name?: string,
) {
  if (action === "connect") {
    const token = String(data.access_token || "").trim();
    if (!token || token.length > 4096) throw new HttpError(400, "访问令牌无效");
    const viewer = (await anilistQuery("query{Viewer{id name}}", {}, token))
      .Viewer;
    await run(
      env,
      "INSERT INTO anilist_accounts VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET token=excluded.token,name=excluded.name,anilist_id=excluded.anilist_id",
      user.user_id,
      await encrypt(env, token),
      viewer.name,
      viewer.id,
    );
    return { username: viewer.name };
  }
  if (action === "disconnect") {
    await env.DB.batch([
      env.DB.prepare("DELETE FROM anilist_links WHERE user_id=?").bind(
        user.user_id,
      ),
      env.DB.prepare("DELETE FROM anilist_accounts WHERE user_id=?").bind(
        user.user_id,
      ),
    ]);
    return {};
  }
  const comic = await comicById(
    env,
    user,
    name || String(data.comic_name || ""),
  );
  if (action === "unmatch") {
    await run(
      env,
      "DELETE FROM anilist_links WHERE user_id=? AND comic_id=?",
      user.user_id,
      comic.id,
    );
    return {};
  }
  if (action === "match" || action === "metadata") {
    const mediaId = Number(data.media_id);
    if (!Number.isSafeInteger(mediaId) || mediaId < 1)
      throw new HttpError(400, "作品 ID 无效");
    const media = (
      await anilistQuery(
        "query($id:Int){Media(id:$id,type:MANGA){id title{userPreferred romaji native} description coverImage{large}}}",
        { id: mediaId },
      )
    ).Media;
    if (action === "metadata") {
      admin(user);
      await run(
        env,
        "UPDATE comics SET description=? WHERE id=?",
        parseHTML(media.description || "").document.textContent || "",
        comic.id,
      );
      return { message: "作品资料已更新" };
    }
    const account = await one(
      env,
      "SELECT user_id FROM anilist_accounts WHERE user_id=?",
      user.user_id,
    );
    if (!account) throw new HttpError(400, "请先连接 AniList");
    const first = Math.max(1, Number(data.first_chapter) || 1);
    await run(
      env,
      "INSERT INTO anilist_links(user_id,comic_id,media_id,media_title,first_chapter) VALUES (?,?,?,?,?) ON CONFLICT(user_id,comic_id) DO UPDATE SET media_id=excluded.media_id,media_title=excluded.media_title,first_chapter=excluded.first_chapter",
      user.user_id,
      comic.id,
      mediaId,
      media.title.userPreferred || media.title.native || media.title.romaji,
      first,
    );
    return {};
  }
  const account = await one(
      env,
      "SELECT * FROM anilist_accounts WHERE user_id=?",
      user.user_id,
    ),
    link = await one(
      env,
      "SELECT * FROM anilist_links WHERE user_id=? AND comic_id=?",
      user.user_id,
      comic.id,
    );
  if (!account || !link) throw new HttpError(400, "作品尚未关联 AniList");
  const token = await decrypt(env, account.token);
  let completed = link.completed_progress;
  if (action === "complete") {
    const index = Number(data.chapter_index);
    if (!Number.isSafeInteger(index) || index < 0 || index > 100000)
      throw new HttpError(400, "章节编号无效");
    completed = Math.max(completed, index + link.first_chapter);
    await run(
      env,
      "UPDATE anilist_links SET completed_progress=? WHERE user_id=? AND comic_id=?",
      completed,
      user.user_id,
      comic.id,
    );
  }
  const current = (
    await anilistQuery(
      "query($media:Int,$user:Int){MediaList(mediaId:$media,userId:$user){progress status}}",
      { media: link.media_id, user: account.anilist_id },
      token,
    )
  ).MediaList;
  const progress = Math.max(
    current?.progress || 0,
    completed,
    link.synced_progress,
  );
  await anilistQuery(
    "mutation($media:Int,$progress:Int){SaveMediaListEntry(mediaId:$media,progress:$progress,status:CURRENT){id progress}}",
    { media: link.media_id, progress },
    token,
  );
  await run(
    env,
    "UPDATE anilist_links SET synced_progress=? WHERE user_id=? AND comic_id=?",
    progress,
    user.user_id,
    comic.id,
  );
  return { progress };
}
export async function saveWebdav(env: Env, user: Identity, data: Row) {
  admin(user);
  const baseUrl = remoteUrl(
      String(data.url || data.base_url || ""),
    ).href.replace(/\/?$/, "/"),
    root = String(data.root || "/");
  if (root.split("/").includes("..") || /[\\\r\n]/.test(root))
    throw new HttpError(400, "WebDAV 根目录无效");
  const url = new URL(root.replace(/^\/+/, ""), baseUrl).href.replace(
      /\/?$/,
      "/",
    ),
    username = String(data.username || "").slice(0, 512),
    password = String(data.password || "");
  const existing = await one(
    env,
    "SELECT password FROM remote_mounts WHERE id='default'",
  );
  if (!password && !existing) throw new HttpError(400, "请输入 WebDAV 密码");
  const encrypted = password
    ? await encrypt(env, password)
    : existing!.password;
  await run(
    env,
    "INSERT INTO remote_mounts(id,url,username,password) VALUES ('default',?,?,?) ON CONFLICT(id) DO UPDATE SET url=excluded.url,username=excluded.username,password=excluded.password,enabled=1",
    url,
    username,
    encrypted,
  );
  await run(
    env,
    "UPDATE remote_mounts SET base_url=?,root=?,cache_gb=? WHERE id='default'",
    baseUrl,
    root,
    Math.max(1, Math.min(200, Number(data.cache_gb) || 10)),
  );
  return {};
}
const WEBDAV_PROPFIND_MAX_BYTES = 5 * 1024 * 1024;
const WEBDAV_MAX_DEPTH = 8;
const WEBDAV_MAX_LISTING = 10000;
const WEBDAV_MAX_TOTAL_ENTRIES = 100000;
const WEBDAV_CHUNK = 40;
const WEBDAV_ENTRY_KEY = "webdav_scan_entries";
const FORBIDDEN_PATH = /[\\\u0000]/;

function decodeWebdavPath(
  pathname: string,
): { segments: string[]; pathname: string } | null {
  if (!pathname.startsWith("/") || FORBIDDEN_PATH.test(pathname)) return null;
  let decoded: string;
  try {
    decoded = decodeURIComponent(pathname);
  } catch {
    return null;
  }
  if (!decoded.startsWith("/") || FORBIDDEN_PATH.test(decoded)) return null;
  const trailing = decoded.endsWith("/");
  const segments: string[] = [];
  for (const segment of decoded.split("/")) {
    if (segment === "" || segment === ".") continue;
    if (segment === "..") return null;
    segments.push(segment);
  }
  return {
    segments,
    pathname: segments.length
      ? "/" + segments.join("/") + (trailing ? "/" : "")
      : "/",
  };
}

function rawAbsolutePath(absolute: string) {
  const match = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\/([^/?#]*)([^?#]*)$/.exec(
    absolute,
  );
  if (!match || match[1].includes("@")) return null;
  const path = match[2] || "/";
  return path.startsWith("/") ? path : null;
}

function resolveWebdavPath(base: URL, candidate: string) {
  const hashAt = candidate.indexOf("#");
  const noHash = hashAt >= 0 ? candidate.slice(0, hashAt) : candidate;
  const queryAt = noHash.indexOf("?");
  const before = queryAt >= 0 ? noHash.slice(0, queryAt) : noHash;
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:/.test(before)) {
    let parsed: URL;
    try {
      parsed = new URL(before);
    } catch {
      return null;
    }
    if (parsed.username || parsed.password || parsed.origin !== base.origin)
      return null;
    return rawAbsolutePath(before);
  }
  if (before.startsWith("//")) {
    let parsed: URL;
    try {
      parsed = new URL(base.protocol + before);
    } catch {
      return null;
    }
    if (parsed.username || parsed.password || parsed.origin !== base.origin)
      return null;
    return rawAbsolutePath(base.protocol + before);
  }
  if (before.startsWith("/")) return before;
  const baseDir = base.pathname.endsWith("/")
    ? base.pathname
    : base.pathname.slice(0, base.pathname.lastIndexOf("/") + 1);
  return baseDir + before;
}

function prefixContains(rootPath: string, candidatePath: string) {
  const rootKey = rootPath.endsWith("/") ? rootPath : rootPath + "/";
  const candidateKey = candidatePath.endsWith("/")
    ? candidatePath
    : candidatePath + "/";
  return candidateKey.startsWith(rootKey);
}

function containWebdav(
  rootRaw: string,
  candidateRaw: string,
  baseRaw = rootRaw,
) {
  if (typeof rootRaw !== "string" || typeof candidateRaw !== "string")
    return null;
  if (!rootRaw || FORBIDDEN_PATH.test(rootRaw) || FORBIDDEN_PATH.test(baseRaw))
    return null;
  if (FORBIDDEN_PATH.test(candidateRaw)) return null;
  let root: URL;
  try {
    root = new URL(rootRaw);
  } catch {
    return null;
  }
  const rootPath = decodeWebdavPath(root.pathname);
  if (!rootPath) return null;
  let base = root;
  if (baseRaw !== rootRaw) {
    if (typeof baseRaw !== "string" || !baseRaw) return null;
    try {
      base = new URL(baseRaw);
    } catch {
      return null;
    }
    if (base.origin !== root.origin) return null;
    const basePath = decodeWebdavPath(base.pathname);
    if (!basePath || !prefixContains(rootPath.pathname, basePath.pathname))
      return null;
  }
  const target = candidateRaw === "" ? baseRaw || rootRaw : candidateRaw;
  const pathname = resolveWebdavPath(base, target);
  if (pathname === null) return null;
  const candidatePath = decodeWebdavPath(pathname);
  if (
    !candidatePath ||
    !prefixContains(rootPath.pathname, candidatePath.pathname)
  )
    return null;
  const out = new URL(root.origin);
  out.pathname = candidatePath.pathname;
  return {
    href: out.href,
    parts: candidatePath.segments.slice(rootPath.segments.length),
  };
}

/** Normalized href when `candidate` is inside `root`; null if it escapes or is malformed.
 * Relative paths resolve against `base` (defaults to the mount root). `..` is rejected,
 * including after decoding `%2e` / `%2F`, and so are backslash, NUL, and bad escapes.
 * The root prefix keeps its trailing slash, so `/lib2` is not inside `/lib`.
 */
export function webdavContainedHref(
  rootRaw: string,
  candidateRaw: string,
  baseRaw = rootRaw,
) {
  return containWebdav(rootRaw, candidateRaw, baseRaw)?.href ?? null;
}

export async function readBoundedText(
  response: Response,
  limit = WEBDAV_PROPFIND_MAX_BYTES,
) {
  const declared = response.headers.get("Content-Length");
  if (
    declared !== null &&
    (!/^\d{1,10}$/.test(declared) || Number(declared) > limit)
  ) {
    await response.body?.cancel();
    throw new HttpError(502, "WebDAV PROPFIND 响应过大");
  }
  if (!response.body) return "";
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!value?.byteLength) continue;
      total += value.byteLength;
      if (total > limit) throw new HttpError(502, "WebDAV PROPFIND 响应过大");
      chunks.push(value);
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(body);
}

function webdavResumeUrl(directoryHref: string, cursor: string) {
  return (
    directoryHref.split("#")[0] + "#md-cursor=" + encodeURIComponent(cursor)
  );
}

async function countWebdavEntry(env: Env) {
  const row = await one(
    env,
    "INSERT INTO settings(key,value) VALUES (?,'1') ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) RETURNING value",
    WEBDAV_ENTRY_KEY,
  );
  const total = Number(row?.value);
  if (
    !Number.isSafeInteger(total) ||
    total < 1 ||
    total > WEBDAV_MAX_TOTAL_ENTRIES
  )
    throw new HttpError(413, "WebDAV 条目过多");
}

async function saveWebdavCursor(
  env: Env,
  job: Row,
  data: Row,
  depth: number,
  cursor: string,
  handedOff = false,
) {
  const payload: Row = {
    mount_id: data.mount_id,
    url: data.url,
    depth,
    cursor,
  };
  if (handedOff) payload.handed_off = true;
  return run(
    env,
    "UPDATE comic_jobs SET payload=?,updated_at=? WHERE id=? AND state='running'",
    JSON.stringify(payload),
    now(),
    job.id,
  );
}

export async function webdavRequest(
  env: Env,
  mount: Row,
  path: string,
  method = "GET",
  extraHeaders: Record<string, string> = {},
) {
  const root = remoteUrl(mount.url);
  const href = webdavContainedHref(root.href, path);
  if (!href) throw new HttpError(403, "WebDAV 路径超出已配置目录");
  const password = await decrypt(env, mount.password);
  const authorization =
    "Basic " +
    btoa(
      String.fromCharCode(
        ...new TextEncoder().encode(mount.username + ":" + password),
      ),
    );
  return remote(href, {
    method,
    headers: {
      Authorization: authorization,
      ...extraHeaders,
      ...(method === "PROPFIND"
        ? { Depth: "1", "Content-Type": "application/xml" }
        : {}),
    },
    body:
      method === "PROPFIND"
        ? '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:allprop/></d:propfind>'
        : undefined,
  });
}
export async function scanWebdav(env: Env, user: Identity) {
  admin(user);
  const mount = await one(
    env,
    "SELECT * FROM remote_mounts WHERE id='default' AND enabled=1",
  );
  if (!mount) throw new HttpError(400, "WebDAV 尚未连接");
  await setSetting(env, WEBDAV_ENTRY_KEY, "0");
  const id = await enqueue(env, user.user_id, "webdav", {
    mount_id: mount.id,
    url: mount.url,
    depth: 0,
  });
  await run(
    env,
    "UPDATE remote_mounts SET state='running',message='扫描已排队' WHERE id=?",
    mount.id,
  );
  return { task_id: id };
}
export async function runWebdavScan(env: Env, job: Row, data: Row) {
  const mount = await one(
    env,
    "SELECT * FROM remote_mounts WHERE id=? AND enabled=1",
    data.mount_id,
  );
  if (!mount) throw new HttpError(409, "WebDAV 已断开");
  if (mount.state === "paused") throw new HttpError(425, "同步已暂停");
  if (mount.state !== "running") throw new HttpError(409, "同步已停止");
  if (data.handed_off) return;
  const depth = data.depth;
  if (
    typeof depth !== "number" ||
    !Number.isInteger(depth) ||
    depth < 0 ||
    depth > WEBDAV_MAX_DEPTH
  )
    throw new HttpError(413, "WebDAV 目录层级过深");
  const here = webdavContainedHref(mount.url, String(data.url || ""));
  if (!here) throw new HttpError(403, "WebDAV 路径超出已配置目录");
  const response = await webdavRequest(env, mount, here, "PROPFIND");
  if (response.status !== 207 && !response.ok) {
    await response.body?.cancel();
    throw new HttpError(502, `WebDAV PROPFIND HTTP ${response.status}`);
  }
  const document = new DOMParser().parseFromString(
      await readBoundedText(response),
      "text/xml",
    ),
    localTags = (node: any, name: string) =>
      [...node.querySelectorAll("*")].filter(
        (tag: any) => (tag.localName || "").split(":").at(-1) === name,
      ),
    nodes = localTags(document, "response");
  if (nodes.length > WEBDAV_MAX_LISTING)
    throw new HttpError(413, "WebDAV 条目过多");
  const hereKey = here.replace(/\/$/, "");
  const cursor = typeof data.cursor === "string" ? data.cursor : "";
  const pending: { href: string; url: URL; item: any }[] = [];
  for (const item of nodes) {
    const raw = String(localTags(item, "href")[0]?.textContent || "").trim();
    if (!raw) continue;
    const safe = webdavContainedHref(mount.url, raw, here);
    if (!safe || safe.replace(/\/$/, "") === hereKey) continue;
    pending.push({ href: safe, url: new URL(safe), item });
  }
  pending.sort((a, b) => (a.href < b.href ? -1 : a.href > b.href ? 1 : 0));
  let processed = 0;
  for (let index = 0; index < pending.length; index++) {
    const entry = pending[index];
    if (cursor && entry.href <= cursor) continue;
    const current = await one(
      env,
      "SELECT state,enabled FROM remote_mounts WHERE id=?",
      mount.id,
    );
    if (current?.state === "paused") throw new HttpError(425, "同步已暂停");
    if (!current?.enabled || current.state !== "running")
      throw new HttpError(409, "同步已停止");
    await countWebdavEntry(env);
    const directory = !!localTags(entry.item, "collection").length;
    if (directory) {
      if (depth < WEBDAV_MAX_DEPTH)
        await enqueue(
          env,
          job.user_id,
          "webdav",
          { mount_id: mount.id, url: entry.href, depth: depth + 1 },
          job.parent_id || job.id,
        );
    } else if (/\.(cbz|pdf|epub)$/i.test(entry.url.pathname)) {
      const size = Number(
          localTags(entry.item, "getcontentlength")[0]?.textContent,
        ),
        etag = localTags(entry.item, "getetag")[0]?.textContent || String(size);
      await registerRemoteFile(env, mount, entry.url, size, etag);
    }
    processed += 1;
    const saved = await saveWebdavCursor(env, job, data, depth, entry.href);
    if (!saved.meta.changes) throw new HttpError(409, "同步已停止");
    const next = pending[index + 1];
    if (processed >= WEBDAV_CHUNK && next && next.href > entry.href) {
      // Active WebDAV jobs are deduped by payload url, so the next slice uses
      // a fragment cursor. The request strips it; payload.cursor is the resume point.
      await enqueue(
        env,
        job.user_id,
        "webdav",
        {
          mount_id: mount.id,
          url: webdavResumeUrl(here, entry.href),
          depth,
          cursor: entry.href,
        },
        job.parent_id || job.id,
      );
      await saveWebdavCursor(env, job, data, depth, entry.href, true);
      return;
    }
  }
  await run(
    env,
    "UPDATE remote_mounts SET message='扫描目录完成，正在导入文件' WHERE id=?",
    mount.id,
  );
}

async function registerRemoteFile(
  env: Env,
  mount: Row,
  url: URL,
  size: number,
  etag: string,
) {
  const contained = containWebdav(mount.url, url.href);
  if (!contained?.parts.length) return;
  url = new URL(contained.href);
  const parts = contained.parts,
    name =
      parts.length > 1 ? parts[0] : parts[0].replace(/\.(cbz|pdf|epub)$/i, ""),
    ext = url.pathname.toLowerCase().endsWith(".pdf")
      ? "pdf"
      : url.pathname.toLowerCase().endsWith(".epub")
        ? "epub"
        : "cbz";
  if (ext === "epub") {
    const id = "n_" + (await hash(url.href)).slice(0, 24);
    await run(
      env,
      "INSERT INTO books(id,source_id,title,author,object_key,size,sha256,created_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET size=excluded.size",
      id,
      id,
      parts.at(-1)!.replace(/\.epub$/i, ""),
      "未知作者",
      `webdav://${mount.id}/${encodeURIComponent(url.href)}`,
      size,
      await hash(etag),
      now(),
    );
    return;
  }
  const existing = await one(
    env,
    "SELECT id,provider FROM comics WHERE name=?",
    name,
  );
  if (existing && existing.provider !== "webdav") return;
  const comicId =
    existing?.id || "c_" + (await hash(mount.url + "|" + name)).slice(0, 12);
  await run(
    env,
    "INSERT INTO comics(id,name,provider,source_url,created_at,updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at",
    comicId,
    name,
    "webdav",
    url.href,
    now(),
    now(),
  );
  const id = "ch_" + (await hash(url.href)).slice(0, 24),
    title = parts.at(-1)!.replace(/\.(cbz|pdf)$/i, ""),
    order = Number(/^([0-9]+)/.exec(title)?.[1] || 0);
  await env.DB.batch([
    env.DB.prepare(
      "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,format,size,state,remote_url,mount_id,source_version) VALUES (?,?,?,?,?,?,?,'ready',?,?,?) ON CONFLICT(id) DO UPDATE SET manifest_key=CASE WHEN source_version=excluded.source_version THEN manifest_key ELSE NULL END,size=excluded.size,remote_url=excluded.remote_url,source_version=excluded.source_version",
    ).bind(
      id,
      comicId,
      order,
      title,
      url.href,
      ext,
      size,
      url.href,
      mount.id,
      etag,
    ),
  ]);
}
export async function metadataSearch(query: string, provider: string) {
  if (provider !== "mangaupdates") return anilistSearch(query);
  const response = await fetch(
    "https://api.mangaupdates.com/v1/series/search",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "User-Agent": "MangaDock/metadata",
      },
      body: JSON.stringify({ search: query.slice(0, 100), perpage: 10 }),
      signal: AbortSignal.timeout(25000),
    },
  );
  if (!response.ok) throw new HttpError(502, "MangaUpdates 搜索暂时不可用");
  const data = (await response.json()) as Row;
  return (data.results || []).map((hit: Row) => {
    const record = hit.record;
    return {
      id: record.series_id,
      provider: "mangaupdates",
      title: { userPreferred: record.title, native: record.title },
      description: record.description || "",
      coverImage: {
        large: record.image?.url?.original || record.image?.url?.thumb || "",
      },
    };
  });
}
export async function saveMetadata(
  env: Env,
  user: Identity,
  name: string,
  data: Row,
) {
  admin(user);
  const comic = await comicById(env, user, name),
    id = Number(data.media_id);
  if (!Number.isSafeInteger(id) || id < 1)
    throw new HttpError(400, "作品 ID 无效");
  let media: Row;
  if (data.provider === "mangaupdates") {
    const response = await fetch(
      "https://api.mangaupdates.com/v1/series/" + id,
      {
        headers: { "User-Agent": "MangaDock/metadata" },
        signal: AbortSignal.timeout(25000),
      },
    );
    if (!response.ok) throw new HttpError(502, "MangaUpdates 资料暂时不可用");
    const record = (await response.json()) as Row;
    media = {
      title: { native: record.title },
      description: record.description || "",
      provider: "mangaupdates",
      id,
    };
  } else
    media = (
      await anilistQuery(
        "query($id:Int){Media(id:$id,type:MANGA){id title{native userPreferred} description bannerImage coverImage{large}}}",
        { id },
      )
    ).Media;
  const doc = parseHTML(
    "<body>" + String(media.description || "") + "</body>",
  ).document;
  for (const node of doc.querySelectorAll("script,style")) node.remove();
  const description =
    doc.body.textContent?.replace(/\s+/g, " ").trim().slice(0, 10000) || "";
  await run(
    env,
    "UPDATE comics SET description=?,metadata=? WHERE id=?",
    description,
    JSON.stringify(media),
    comic.id,
  );
  return { message: "作品资料已更新" };
}
