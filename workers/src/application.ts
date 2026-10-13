import { checkUpdates } from "./updates";
import { publicUser } from "./public-user";
import { DurableObject } from "cloudflare:workers";
import { databaseEnv } from "./database";
import { DatabaseBudget, DatabasePausedError } from "./database-budget";
import {
  HttpError,
  now,
  json,
  hash,
  source,
  sourceJson,
  pilotRoute,
  processJob,
  maintenance,
  type Env,
  type Identity,
} from "./index";
import routes from "../legacy-routes.json";
import {
  all,
  one,
  run,
  admin,
  downloader,
  comicById,
  canAccess,
  library,
  groups,
  novels,
  csrfIdentity,
  input,
  list,
  groupName,
  setting,
  setSetting,
  safeReturn,
  stored,
  putBytes,
  type Row,
} from "./library";
import { pages, pageContext } from "./pages";
import { render, date, urlFor } from "./render";
import {
  createUser,
  getUser,
  saveGroups,
  changePassword,
  deleteUser,
} from "./accounts";
import { saveProgress, serializeProgress, statistics } from "./reading";
import {
  runJob,
  createDownload,
  tasks,
  taskAction,
  enqueue,
} from "./downloads";
import { indexNovel, novelChapter } from "./novels";
import {
  anilistAction,
  saveWebdav,
  scanWebdav,
  saveMetadata,
} from "./integrations";
import { computeApi, queueCompute } from "./compute";
import { importApi } from "./imports";
import { uploadApi } from "./uploads";
import { mediaApi } from "./media";
import { scanLibrary, r2Prefix } from "./scans";
import { loadComic } from "./providers";
const definitions = routes.map((route) => ({
  ...route,
  keys: [...route.path.matchAll(/<(?:(?:int|path):)?(\w+)>/g)].map(
    (match) => match[1],
  ),
  pattern: new RegExp(
    "^" +
      route.path
        .replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
        .replace(/<(?:(?:int|path):)?\w+>/g, "([^/]+)") +
      "/?$",
  ),
}));
export class ApplicationService extends DurableObject<Env> {
  constructor(state: DurableObjectState, env: Env) {
    super(state, databaseEnv(env, new DatabaseBudget(state.storage)));
  }
  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    try {
      if (
        url.hostname === "jobs.internal" &&
        request.method === "POST" &&
        url.pathname === "/run"
      ) {
        const { id } = (await request.json()) as { id: string };
        await runJob(this.env, id);
        return json({ ok: true });
      }
      if (
        url.hostname === "jobs.internal" &&
        request.method === "POST" &&
        url.pathname === "/run-novel"
      ) {
        const { id } = (await request.json()) as { id: string };
        await processJob(this.env, id);
        return json({ ok: true });
      }
      if (
        url.hostname === "jobs.internal" &&
        request.method === "POST" &&
        url.pathname === "/maintenance"
      ) {
        await maintenance(this.env);
        return json({ ok: true });
      }
      if (
        url.pathname === "/api/info" ||
        url.pathname === "/api/v1" ||
        url.pathname === "/api/v1/" ||
        (url.pathname === "/api/auth/login" && request.method === "POST") ||
        (url.pathname === "/login" && request.method === "POST") ||
        /^(?:\/api\/v1\/auth\/(?:login|logout|me)|\/api\/(?:books|search|jobs|source\/register))$/.test(
          url.pathname,
        ) ||
        /^\/api\/books\/[^/]+\/(?:file|progress)$/.test(url.pathname)
      )
        return await pilotRoute(request, this.env);
      if (/^\/login-cover\/\d+\.jpg$/.test(url.pathname)) {
        const wall = await this.env.ASSETS.fetch(
          new Request(new URL("/login-wall.json", url)),
        );
        const items = (await wall.json()) as { url: string }[],
          slot = Number(url.pathname.match(/\d+/)![0]);
        if (!items[slot]) throw new HttpError(404, "壁纸不存在");
        return this.env.ASSETS.fetch(
          new Request(new URL(items[slot].url, url)),
        );
      }
      if (url.pathname === "/login" && request.method === "GET") {
        const wall = await this.env.ASSETS.fetch(
          new Request(new URL("/login-wall.json", url)),
        );
        return render("login.html", {
          login_wall: await wall.json(),
          csrf_token: () => "",
          get_flashed_messages: () => [],
          request: {
            endpoint: "login",
            args: {
              get: (key: string, fallback: string = "") =>
                url.searchParams.get(key) || fallback,
            },
          },
        });
      }
      const user = await csrfIdentity(request, this.env);
      if (url.pathname === "/reader") {
        const id = url.searchParams.get("book");
        if (!id) throw new HttpError(400, "请选择作品");
        const target = new URL(
          "/novel/" + encodeURIComponent(id) + "/read",
          url,
        );
        if (url.searchParams.has("start_chapter"))
          target.searchParams.set(
            "start_chapter",
            url.searchParams.get("start_chapter")!,
          );
        return Response.redirect(target.href, 302);
      }
      const imported = await importApi(request, this.env, user);
      if (imported) return imported;
      const upload = await uploadApi(request, this.env, user);
      if (upload) return upload;
      const compute = await computeApi(request, this.env, user);
      if (compute) return compute;
      const media = await mediaApi(request, this.env, user);
      if (media) return media;
      const matched = definitions.find(
        (route) =>
          route.methods.includes(
            request.method === "HEAD" ? "GET" : request.method,
          ) && route.pattern.test(url.pathname),
      );
      if (!matched) throw new HttpError(404, "接口或页面不存在");
      const match = matched.pattern.exec(url.pathname)!,
        params = Object.fromEntries(
          matched.keys.map((key, i) => [key, decodeURIComponent(match[i + 1])]),
        ),
        endpoint = matched.endpoint;
      if (request.method === "GET" || request.method === "HEAD")
        return await this.get(request, user, endpoint, params);
      return await this.mutate(request, user, endpoint, params);
    } catch (error) {
      const status =
        error instanceof DatabasePausedError
          ? 503
          : error instanceof HttpError
            ? error.status
            : 500;
      const message =
        error instanceof DatabasePausedError
          ? error.message
          : error instanceof HttpError
            ? error.message
            : "服务暂时不可用";
      if (!(error instanceof HttpError))
        console.error(
          JSON.stringify({
            component: "application",
            error_type: error instanceof Error ? error.name : "unknown",
            frames:
              error instanceof Error
                ? error.stack?.split("\n").slice(1, 4)
                : [],
          }),
        );
      if (!url.pathname.startsWith("/api/") && status === 401)
        return Response.redirect(
          url.origin +
            "/login?next=" +
            encodeURIComponent(url.pathname + url.search),
          302,
        );
      if (
        url.pathname.startsWith("/api/") ||
        request.headers.get("Accept")?.includes("application/json")
      )
        return json(
          {
            ok: false,
            error: {
              code: status === 403 ? "FORBIDDEN" : "REQUEST_FAILED",
              message,
            },
          },
          status,
        );
      const context = {
        session: {},
        request: { endpoint: "error", args: { get: () => null } },
        get_flashed_messages: () => [],
        csrf_token: () => "",
      };
      return render("error.html", { ...context, message }, status);
    }
  }
  async get(request: Request, user: Identity, endpoint: string, params: Row) {
    const env = this.env,
      url = new URL(request.url);
    if (endpoint === "get_progress") {
      const name = params.comic_name;
      const legacy = (row: Row | null) =>
        row
          ? {
              status: "success",
              chapter: row.chapter,
              page: row.page,
              ...serializeProgress(row),
            }
          : {
              status: "success",
              chapter: 0,
              page: 0,
              scroll_position: 0,
              anchor_paragraph: null,
              anchor_offset: null,
            };
      if (name.startsWith("novel:"))
        return json(
          legacy(
            await one(
              env,
              "SELECT * FROM progress WHERE user_id=? AND book_id=?",
              user.user_id,
              name.slice(6),
            ),
          ),
        );
      const comic = await comicById(env, user, name);
      return json(
        legacy(
          await one(
            env,
            "SELECT * FROM comic_progress WHERE user_id=? AND comic_id=?",
            user.user_id,
            comic.id,
          ),
        ),
      );
    }
    if (endpoint === "novel_chapter_data")
      return json(
        await novelChapter(env, params.novel_id, Number(params.chapter_index)),
      );
    if (
      endpoint === "task_status" ||
      endpoint === "get_task_detail" ||
      endpoint === "list_tasks"
    ) {
      downloader(user);
      const rows = await tasks(env, user);
      if (endpoint === "list_tasks")
        return json({
          ok: true,
          data: {
            items: rows,
            active_count: rows.filter(
              (row) =>
                !["completed", "cancelled", "error"].includes(row.status),
            ).length,
            deletable_count: rows.filter((row) =>
              ["completed", "cancelled", "error"].includes(row.status),
            ).length,
            total: rows.length,
          },
        });
      const task = rows.find((t) => t.id === params.task_id);
      if (!task) throw new HttpError(404, "任务不存在");
      return endpoint === "task_status"
        ? json({
            ...task,
            comic_name: task.comic_name,
            status: task.status,
            log: task.message,
          })
        : json({ ok: true, data: task });
    }
    if (endpoint === "list_users") {
      admin(user);
      const items = await all(
        env,
        "SELECT id,username,role,can_download,can_view_adult FROM users",
      );
      for (const item of items)
        item.group_names =
          item.role === "admin"
            ? await groups(env, user)
            : await all(
                env,
                "SELECT group_name FROM group_permissions WHERE user_id=?",
                item.id,
              ).then((rows) => rows.map((r) => r.group_name));
      return json({
        ok: true,
        data: {
          items: items.map((item) => ({
            ...publicUser(item as any),
            group_names: item.group_names,
          })),
        },
      });
    }
    if (endpoint === "list_groups") {
      const names = await groups(env, user),
        items = (await library(env, user)).comics;
      return json({
        ok: true,
        data: {
          items: names.map((name) => ({
            name,
            comic_count: items.filter((c) => c.group === name).length,
          })),
          groups: names,
        },
      });
    }
    if (endpoint === "get_adult_content_setting") {
      admin(user);
      return json({
        ok: true,
        data: {
          enabled: (await setting(env, "adult_content_enabled", "0")) === "1",
        },
      });
    }
    if (endpoint === "get_updates_status") {
      downloader(user);
      return json({
        ok: true,
        data: {
          candidates: JSON.parse(await setting(env, "update_candidates", "[]")),
          checking: false,
          mode: await setting(env, "comic_update_mode", "manual"),
          last_checked_at: new Date(
            Number(await setting(env, "updates_checked_at", "0")) * 1000,
          ).toISOString(),
          tracked_comics: (await library(env, user, true)).comics.map(
            (row) => row.name,
          ),
        },
      });
    }
    if (endpoint === "latest_release") {
      const response = await fetch(
        "https://api.github.com/repos/dddinmx/MangaDock/releases/latest",
        {
          headers: {
            "User-Agent": "MangaDock",
            Accept: "application/vnd.github+json",
          },
          signal: AbortSignal.timeout(10000),
        },
      );
      const data = (await response.json()) as Row;
      if (!response.ok) throw new HttpError(502, "暂时无法获取最新版本");
      return json({
        version: data.tag_name,
        latest_version: data.tag_name,
        current_version: "5.0",
        release_url: data.html_url,
      });
    }
    if (endpoint === "webdav_sync_progress") {
      admin(user);
      const mount = await one(
        env,
        "SELECT state,message FROM remote_mounts LIMIT 1",
      );
      return json({
        configured: !!mount,
        version: String(
          (
            await one(
              env,
              "SELECT COALESCE(MAX(updated_at),0) AS value FROM comics",
            )
          )?.value || 0,
        ),
        status: mount?.state || "idle",
        state: mount?.state || "idle",
        message: mount?.message || "",
        progress: 0,
      });
    }
    if (endpoint === "list_scan_paths") {
      admin(user);
      return json({
        ok: true,
        data: {
          default_root: "r2://library/",
          items: await all(
            env,
            "SELECT key AS id,value AS path,1 AS enabled FROM settings WHERE key LIKE 'scan_path:%'",
          ),
        },
      });
    }
    return pages(request, env, user, endpoint, params);
  }
  async mutate(
    request: Request,
    user: Identity,
    endpoint: string,
    params: Row,
  ) {
    const env = this.env,
      url = new URL(request.url),
      data = await input(request);
    let result: Row = {},
      redirect = "/";
    const api = url.pathname.startsWith("/api/v1/");
    if (endpoint === "users" || endpoint === "create_user") {
      const created = await createUser(env, user, data);
      result = {
        user: publicUser(created as any),
        id: publicUser(created as any).id,
        message: "用户已创建",
      };
      redirect = "/users/" + created.id;
    } else if (
      ["change_password", "change_password_v1", "reset_user_password"].includes(
        endpoint,
      )
    ) {
      await changePassword(env, user, data, params.user_id);
      result = { message: "密码已更新，请重新登录" };
      redirect =
        endpoint === "reset_user_password"
          ? "/users/" + params.user_id
          : "/login";
    } else if (endpoint === "update_user_permissions") {
      admin(user);
      const target = await getUser(env, params.user_id);
      if (target.role === "admin")
        throw new HttpError(400, "管理员无需分配权限");
      await run(
        env,
        "UPDATE users SET can_download=?,can_view_adult=? WHERE id=?",
        data.can_download ? 1 : 0,
        data.can_view_adult ? 1 : 0,
        target.id,
      );
      result = { message: "权限已更新" };
      redirect = "/users/" + target.id;
    } else if (
      ["update_user_groups", "update_user_groups_v1"].includes(endpoint)
    ) {
      admin(user);
      const target = await getUser(env, params.user_id);
      if (target.role === "admin")
        throw new HttpError(400, "管理员无需分配分组");
      result = {
        group_names: await saveGroups(
          env,
          target.id,
          list(data.group_names || data.groups),
        ),
        message: "分组权限已更新",
      };
      redirect = "/users/" + target.id;
    } else if (["delete_user", "delete_user_v1"].includes(endpoint)) {
      await deleteUser(env, user, params.user_id);
      result = { deleted: true, message: "用户已删除" };
      redirect = "/users";
    } else if (
      [
        "create_comic_group_route",
        "create_comic_group",
        "create_novel_group",
        "create_group",
        "remove_comic_group",
        "remove_novel_group",
        "remove_group",
        "delete_comic_group_route",
        "assign_comic_group_route",
        "batch_assign_comic_group",
        "batch_assign_novel_group",
        "assign_group",
      ].includes(endpoint)
    ) {
      admin(user);
      const kind = endpoint.includes("novel") ? "novel" : "comic",
        name = groupName(params.group_name || data.group_name || data.name);
      if (!name || name === "全部") throw new HttpError(400, "分组名称无效");
      const removing =
          endpoint.includes("remove") ||
          endpoint === "delete_comic_group_route",
        assigning = endpoint.includes("assign");
      if (removing) {
        if (name === "默认分组") throw new HttpError(400, "不能删除默认分组");
        await env.DB.batch([
          env.DB.prepare(
            `UPDATE ${kind === "novel" ? "books" : "comics"} SET group_name='默认分组' WHERE group_name=?`,
          ).bind(name),
          env.DB.prepare(
            "DELETE FROM library_groups WHERE kind=? AND name=?",
          ).bind(kind, name),
          ...(kind === "comic"
            ? [
                env.DB.prepare(
                  "DELETE FROM group_permissions WHERE group_name=?",
                ).bind(name),
              ]
            : []),
        ]);
      } else if (assigning) {
        if (
          !(await one(
            env,
            "SELECT name FROM library_groups WHERE kind=? AND name=?",
            kind,
            name,
          ))
        )
          throw new HttpError(404, "分组不存在");
        const selected = list(
          kind === "novel"
            ? data.novel_ids || data.novel_id
            : data.comic_names || data.comic_name || data.names,
        ).slice(0, 500);
        for (const id of selected)
          await run(
            env,
            `UPDATE ${kind === "novel" ? "books" : "comics"} SET group_name=? WHERE ${kind === "novel" ? "id" : "name"}=?`,
            name,
            String(id),
          );
      } else
        await run(
          env,
          "INSERT INTO library_groups VALUES (?,?,?) ON CONFLICT DO NOTHING",
          kind,
          name,
          now(),
        );
      result = { message: "分组已更新" };
      redirect = kind === "novel" ? "/novels/groups" : "/comics/groups";
      if (data.return_to === "detail")
        redirect =
          "/comic/" +
          encodeURIComponent(String(data.task_id || data.comic_name || ""));
    } else if (
      [
        "toggle_admin_hidden_comic",
        "toggle_admin_hidden_group",
        "toggle_hidden_comic",
        "toggle_hidden_group",
      ].includes(endpoint)
    ) {
      admin(user);
      const kind = endpoint.includes("group") ? "group" : "comic",
        target = String(data.comic_name || data.group_name || "");
      if (kind === "comic") await comicById(env, user, target);
      else if (
        !(await one(
          env,
          "SELECT name FROM library_groups WHERE kind='comic' AND name=?",
          target,
        ))
      )
        throw new HttpError(404, "分组不存在");
      const existing = await one(
          env,
          "SELECT 1 FROM hidden_items WHERE user_id=? AND kind=? AND target=?",
          user.user_id,
          kind,
          target,
        ),
        hide =
          typeof data.hidden === "boolean"
            ? data.hidden
            : data.action
              ? data.action === "hide"
              : !existing;
      if (hide)
        await run(
          env,
          "INSERT INTO hidden_items VALUES (?,?,?) ON CONFLICT DO NOTHING",
          user.user_id,
          kind,
          target,
        );
      else
        await run(
          env,
          "DELETE FROM hidden_items WHERE user_id=? AND kind=? AND target=?",
          user.user_id,
          kind,
          target,
        );
      result = { hidden: hide, message: "隐藏状态已更新" };
      redirect =
        data.return_to === "detail"
          ? "/comic/" + encodeURIComponent(String(data.task_id || target))
          : "/comics";
    } else if (
      ["set_adult_content", "update_adult_content_setting"].includes(endpoint)
    ) {
      admin(user);
      const enabled =
        data.enabled === true ||
        ["1", "true", "on", "yes"].includes(String(data.enabled || ""));
      await setSetting(env, "adult_content_enabled", enabled ? "1" : "0");
      result = { enabled, message: "18+ 来源设置已更新" };
      redirect = "/settings";
    } else if (
      ["download", "create_download", "start_update", "update"].includes(
        endpoint,
      )
    ) {
      downloader(user);
      if (endpoint === "update" || endpoint === "start_update") {
        const selected = list(
          data.comic_names || data.comic_name || data.comics,
        );
        const ids = [];
        for (const name of selected) {
          const comic = await comicById(env, user, String(name));
          ids.push(
            await createDownload(env, user, {
              url: comic.source_url,
              group: comic.group_name,
              format: data.format || 2,
              is_update: true,
            }),
          );
        }
        result = { task_ids: ids };
        redirect = "/tasks";
      } else {
        const id = await createDownload(env, user, data);
        result = {
          task_id: id,
          id,
          next_url: "/progress/" + id,
          message: "下载已排队",
        };
        redirect = "/progress/" + id;
      }
    } else if (["check_updates_now", "check_updates"].includes(endpoint)) {
      downloader(user);
      const candidates = await checkUpdates(env, user);
      result = { items: candidates, candidates, message: "更新检查完成" };
      redirect = "/update";
    } else if (["update_mode", "set_update_mode"].includes(endpoint)) {
      downloader(user);
      const mode = String(data.mode || "");
      if (!["auto", "manual"].includes(mode))
        throw new HttpError(400, "更新模式无效");
      await setSetting(env, "comic_update_mode", mode);
      result = { mode };
      redirect = "/update";
    } else if (
      [
        "cancel_task",
        "cancel_task_v1",
        "delete_task_route",
        "delete_task_v1",
        "delete_tasks_route",
        "delete_finished_tasks_v1",
      ].includes(endpoint)
    ) {
      downloader(user);
      const deleting = endpoint.startsWith("delete"),
        ids = params.task_id
          ? [params.task_id]
          : data.task_ids
            ? list(data.task_ids)
            : (await tasks(env, user))
                .filter((t) =>
                  ["completed", "cancelled", "error"].includes(t.status),
                )
                .map((t) => t.id);
      for (const id of ids)
        await taskAction(env, user, String(id), deleting ? "delete" : "cancel");
      result = { message: deleting ? "任务已删除" : "任务已取消" };
      redirect = "/tasks";
    } else if (endpoint === "save_progress") {
      await saveProgress(env, user, data);
      return json({ status: "success", success: true });
    } else if (endpoint === "fanqie_novel_download") {
      admin(user);
      const id = String(data.book_id || "");
      if (!/^\d{8,24}$/.test(id)) throw new HttpError(400, "番茄书籍 ID 无效");
      const requestId = crypto.randomUUID();
      if (
        await one(
          env,
          "SELECT id FROM jobs WHERE source_id=? AND state IN ('queued','submitting','running')",
          id,
        )
      )
        throw new HttpError(409, "已有进行中的小说下载");
      await run(
        env,
        "INSERT INTO jobs(id,user_id,source_id,created_at,updated_at) VALUES (?,?,?,?,?)",
        requestId,
        user.user_id,
        id,
        now(),
        now(),
      );
      try {
        await env.DOWNLOADS.send({ id: requestId });
      } catch {}
      result = { task_id: requestId };
      redirect = "/progress/" + requestId;
    } else if (
      ["update_comic_cover", "upload_cover", "update_novel_cover"].includes(
        endpoint,
      )
    ) {
      admin(user);
      const novel = endpoint === "update_novel_cover",
        media = novel
          ? await one(env, "SELECT * FROM books WHERE id=?", params.novel_id)
          : await comicById(env, user, params.task_id || params.comic_name);
      if (!media) throw new HttpError(404, "作品不存在");
      const file = data.cover_file || data.file;
      if (
        !(file instanceof File) ||
        file.size < 1 ||
        file.size > 5242880 ||
        !["image/jpeg", "image/png", "image/webp"].includes(file.type)
      )
        throw new HttpError(400, "请上传 5 MB 以内 JPEG/PNG/WebP 封面");
      const bytes = new Uint8Array(await file.arrayBuffer());
      const key = `${novel ? "novels" : "comics"}/${media.id}/cover/${await hash(String(now()) + file.name)}`;
      await putBytes(env, key, bytes, file.type);
      await run(
        env,
        `UPDATE ${novel ? "books" : "comics"} SET cover_key=?,hero_key=NULL,mobile_hero_key=NULL WHERE id=?`,
        key,
        media.id,
      );
      await queueCompute(env, "cover", key, {
        kind: novel ? "novel" : "comic",
        media_id: media.id,
      });
      result = { message: "封面已保存，超分缓存将由云端作业生成" };
      redirect = novel
        ? "/novel/" + encodeURIComponent(media.id)
        : "/comic/" + encodeURIComponent(media.id);
    } else if (endpoint === "logout" || endpoint === "api_auth_logout") {
      await run(
        env,
        "DELETE FROM sessions WHERE token_hash=?",
        user.token_hash,
      );
      if (endpoint === "api_auth_logout")
        return new Response(
          JSON.stringify({ ok: true, data: {}, error: null }),
          {
            headers: {
              "Content-Type": "application/json; charset=utf-8",
              "Set-Cookie":
                "md_session=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0",
            },
          },
        );
      return new Response(null, {
        status: 303,
        headers: {
          Location: "/login",
          "Set-Cookie":
            "md_session=; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=0",
        },
      });
    } else if (endpoint === "anilist_save_metadata") {
      result = await saveMetadata(env, user, params.comic_name, data);
      redirect =
        "/anilist/match/" +
        encodeURIComponent(params.comic_name) +
        "?metadata=1";
    } else if (endpoint.startsWith("anilist_")) {
      const action = (
        {
          anilist_connect: "connect",
          anilist_disconnect: "disconnect",
          anilist_save_match: "match",
          anilist_save_metadata: "metadata",
          anilist_unmatch: "unmatch",
          anilist_complete: "complete",
          anilist_sync: "sync",
        } as Row
      )[endpoint];
      result = await anilistAction(env, user, action, data, params.comic_name);
      if (endpoint === "anilist_complete" || endpoint === "anilist_sync")
        return json({ success: true, ...result });
      redirect = params.comic_name
        ? "/anilist/match/" + encodeURIComponent(params.comic_name)
        : "/settings";
    } else if (endpoint === "save_webdav") {
      result = await saveWebdav(env, user, data);
      redirect = "/settings/webdav";
    } else if (endpoint === "sync_webdav") {
      result = await scanWebdav(env, user);
      redirect = "/settings/webdav";
    } else if (endpoint === "control_webdav_sync") {
      admin(user);
      const state = ["pause", "stop"].includes(String(data.action))
        ? "paused"
        : "running";
      await run(env, "UPDATE remote_mounts SET state=?", state);
      result = { state };
      redirect = "/settings/webdav";
      if (state === "running") await scanWebdav(env, user);
    } else if (endpoint === "disconnect_webdav_route") {
      admin(user);
      await run(env, "UPDATE remote_mounts SET enabled=0,state='disconnected'");
      result = { message: "WebDAV 已断开" };
      redirect = "/settings/webdav";
    } else if (
      [
        "add_scan_path",
        "add_scan_path_v1",
        "delete_scan_path",
        "delete_scan_path_v1",
        "rescan_comic_library",
        "rescan_library",
      ].includes(endpoint)
    ) {
      admin(user);
      if (endpoint.startsWith("add")) {
        const path = String(data.path || data.scan_path || "").trim();
        if (!path.startsWith("r2://") && !path.startsWith("https://"))
          throw new HttpError(
            400,
            "云端扫盘请使用 R2 前缀或 WebDAV HTTPS 目录；本地目录由 Cloud 导入工具处理",
          );
        if (path.startsWith("r2://")) r2Prefix(path);
        const key = "scan_path:" + (await hash(path)).slice(0, 12);
        await setSetting(env, key, path);
        result = { id: key, path };
      } else if (endpoint.startsWith("delete"))
        await run(env, "DELETE FROM settings WHERE key=?", params.scan_path_id);
      else {
        result = await scanLibrary(env, user);
      }
      redirect = "/settings/scan_paths";
    } else if (endpoint === "delete_comic_route") {
      admin(user);
      const comic = await comicById(
        env,
        user,
        String(data.comic_name || data.task_id || ""),
      );
      await run(
        env,
        "UPDATE comic_jobs SET state='cancelled' WHERE comic_id=? AND state IN ('queued','running','waiting')",
        comic.id,
      );
      await env.DB.batch([
        env.DB.prepare("DELETE FROM anilist_links WHERE comic_id=?").bind(
          comic.id,
        ),
        env.DB.prepare("DELETE FROM comic_progress WHERE comic_id=?").bind(
          comic.id,
        ),
        env.DB.prepare("DELETE FROM chapters WHERE comic_id=?").bind(comic.id),
        env.DB.prepare(
          "UPDATE comic_jobs SET comic_id=NULL WHERE comic_id=?",
        ).bind(comic.id),
        env.DB.prepare("DELETE FROM comics WHERE id=?").bind(comic.id),
      ]);
      let cursor: string | undefined;
      do {
        const objects = await env.LIBRARY.list({
          prefix: `comics/${comic.id}/`,
          cursor,
        });
        await env.LIBRARY.delete(objects.objects.map((o) => o.key));
        for (const object of objects.objects)
          await run(env, "DELETE FROM storage_ledger WHERE key=?", object.key);
        cursor = objects.truncated ? objects.cursor : undefined;
      } while (cursor);
      result = { message: "漫画已删除" };
      redirect = "/comics";
    } else throw new HttpError(404, "接口不存在");
    if (api)
      return json(
        { ok: true, data: result },
        endpoint === "create_user" ? 201 : 200,
      );
    const location = new URL(
      safeReturn(data.return_url || data.return_to_url, redirect),
      url,
    );
    if (result.message) location.searchParams.set("message", result.message);
    return new Response(null, {
      status: 303,
      headers: { Location: location.pathname + location.search },
    });
  }
}
