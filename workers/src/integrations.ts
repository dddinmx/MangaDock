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
export async function webdavRequest(
  env: Env,
  mount: Row,
  path: string,
  method = "GET",
  extraHeaders: Record<string, string> = {},
) {
  const root = remoteUrl(mount.url),
    url = new URL(path, root);
  if (
    url.origin !== root.origin ||
    !decodeURIComponent(url.pathname).startsWith(
      decodeURIComponent(root.pathname),
    )
  )
    throw new HttpError(403, "WebDAV 路径超出已配置目录");
  const password = await decrypt(env, mount.password);
  const authorization =
    "Basic " +
    btoa(
      String.fromCharCode(
        ...new TextEncoder().encode(mount.username + ":" + password),
      ),
    );
  return remote(url.href, {
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
  if (data.depth > 10) throw new HttpError(413, "WebDAV 目录层级过深");
  const response = await webdavRequest(env, mount, data.url, "PROPFIND");
  if (response.status !== 207 && !response.ok)
    throw new HttpError(502, `WebDAV PROPFIND HTTP ${response.status}`);
  const document = new DOMParser().parseFromString(
      await response.text(),
      "text/xml",
    ),
    localTags = (node: any, name: string) =>
      [...node.querySelectorAll("*")].filter(
        (tag: any) => (tag.localName || "").split(":").at(-1) === name,
      ),
    nodes = localTags(document, "response");
  for (const item of nodes) {
    const current = await one(
      env,
      "SELECT state,enabled FROM remote_mounts WHERE id=?",
      mount.id,
    );
    if (current?.state === "paused") throw new HttpError(425, "同步已暂停");
    if (!current?.enabled || current.state !== "running")
      throw new HttpError(409, "同步已停止");
    const href = localTags(item, "href")[0]?.textContent;
    if (!href) continue;
    const url = new URL(href, mount.url);
    if (url.href.replace(/\/$/, "") === data.url.replace(/\/$/, "")) continue;
    if (
      url.origin !== new URL(mount.url).origin ||
      !url.pathname.startsWith(new URL(mount.url).pathname)
    )
      continue;
    const directory = !!localTags(item, "collection").length;
    if (directory) {
      await enqueue(
        env,
        job.user_id,
        "webdav",
        { mount_id: mount.id, url: url.href, depth: data.depth + 1 },
        job.parent_id || job.id,
      );
      continue;
    }
    if (!/\.(cbz|pdf|epub)$/i.test(url.pathname)) continue;
    const size = Number(localTags(item, "getcontentlength")[0]?.textContent),
      etag = localTags(item, "getetag")[0]?.textContent || String(size);
    await registerRemoteFile(env, mount, url, size, etag);
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
  const parts = decodeURIComponent(url.pathname)
      .slice(decodeURIComponent(new URL(mount.url).pathname).length)
      .split("/")
      .filter(Boolean),
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
  await run(
    env,
    "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,format,size,state,remote_url,mount_id,source_version) VALUES (?,?,?,?,?,?,?,'ready',?,?,?) ON CONFLICT(id) DO UPDATE SET manifest_key=CASE WHEN source_version=excluded.source_version THEN manifest_key ELSE NULL END,size=excluded.size,remote_url=excluded.remote_url,source_version=excluded.source_version",
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
  );
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
