import { HttpError, type Env, type Identity, sourceJson } from "./index";
import { publicUser } from "./public-user";
import { render, date, pagination } from "./render";
import {
  all,
  one,
  library,
  novels,
  groups,
  comicById,
  admin,
  downloader,
  setting,
  type Row,
} from "./library";
import { getUser } from "./accounts";
import { indexNovel, novelChapter } from "./novels";
import { updateStatus } from "./updates";
import { tasks } from "./downloads";
import { statistics, serializeProgress } from "./reading";
import { anilistSearch, metadataSearch } from "./integrations";
export async function pageContext(
  request: Request,
  env: Env,
  user: Identity,
  endpoint: string,
) {
  const url = new URL(request.url);
  return {
    current_user: publicUser(user),
    session: { user_id: user.user_id, username: user.username },
    csrf_token: () => user.csrf,
    is_admin_user: user.role === "admin",
    can_manage_library: user.role === "admin" || !!user.can_download,
    request: {
      endpoint,
      args: {
        get: (key: string, fallback: any = null) =>
          url.searchParams.get(key) ?? fallback,
      },
    },
    get_flashed_messages: () =>
      url.searchParams.get("message") ? [url.searchParams.get("message")] : [],
    release_version: "2.11.29",
    adult_content_enabled:
      !!user.can_view_adult ||
      (await setting(env, "adult_content_enabled", "0")) === "1",
    library_mode: url.pathname.startsWith("/novel")
      ? "novel"
      : url.searchParams.get("mode") === "novel"
        ? "novel"
        : "comic",
  };
}
const countGroups = (items: Row[], names: string[]) =>
  Object.fromEntries(
    names.map((name) => [
      name,
      items.filter((item) => item.group === name).length,
    ]),
  );
export async function pages(
  request: Request,
  env: Env,
  user: Identity,
  endpoint: string,
  params: Row,
) {
  const url = new URL(request.url),
    context = await pageContext(request, env, user, endpoint),
    show = (name: string, data: Row = {}) =>
      render(name, { ...context, ...data });
  const q = (url.searchParams.get("q") || "").trim().slice(0, 100);
  if (
    endpoint === "index" ||
    endpoint === "comics_list" ||
    endpoint === "search_page" ||
    endpoint === "history_page" ||
    endpoint === "comic_group_manager"
  ) {
    const includeHidden =
        url.searchParams.get("show_hidden") === "1" && user.role === "admin",
      data = await library(env, user, includeHidden),
      names = await groups(env, user),
      progress = await all(
        env,
        "SELECT p.*,c.name FROM comic_progress p JOIN comics c ON c.id=p.comic_id WHERE p.user_id=? ORDER BY p.updated_at DESC",
        user.user_id,
      ),
      progresses = Object.fromEntries(
        progress.map((p) => [
          p.name,
          {
            ...serializeProgress(p),
            has_progress: true,
            last_read_at: date(p.updated_at),
          },
        ]),
      );
    const recent = progress
      .map((p): Row => ({
        ...data.comics.find((c) => c.id === p.comic_id),
        ...serializeProgress(p),
        has_progress: true,
        last_read_at: date(p.updated_at),
      }))
      .filter((c) => c.id);
    const counts = countGroups(data.comics, names);
    if (endpoint === "index") {
      const spotlight = recent[0] || data.comics[0] || null;
      return show("index.html", {
        spotlight,
        hero_comics: recent.length ? recent : data.comics.slice(0, 10),
        recent_comics: recent,
        latest_comics: data.comics
          .filter((c) => !progresses[c.comic_name])
          .slice(0, 12),
        group_rails: names
          .map((name) => ({
            name,
            comics: data.comics.filter((c) => c.group === name).slice(0, 10),
          }))
          .filter((r) => r.comics.length)
          .slice(0, 6),
      });
    }
    if (endpoint === "search_page") {
      const found = data.comics.filter((c) =>
        c.name.toLowerCase().includes(q.toLowerCase()),
      );
      return show("search.html", {
        query: q,
        comics: found,
        result_count: found.length,
      });
    }
    if (endpoint === "history_page")
      return show("history.html", { items: recent });
    if (endpoint === "comic_group_manager") {
      admin(user);
      return show("comic_groups.html", {
        comics: data.comics,
        groups: names,
        group_counts: counts,
      });
    }
    const selected = names.includes(url.searchParams.get("group") || "")
        ? url.searchParams.get("group")!
        : "全部",
      items = data.comics
        .filter((c) => selected === "全部" || c.group === selected)
        .sort(
          (a, b) =>
            (progress.find((p) => p.comic_id === b.id)?.updated_at || 0) -
            (progress.find((p) => p.comic_id === a.id)?.updated_at || 0),
        ),
      page = pagination(items, url.searchParams.get("page"));
    return show("comics.html", {
      tasks: page.items,
      pagination: page.pagination,
      progresses,
      groups: names,
      group_counts: counts,
      group_total_count: data.comics.length,
      selected_group_count: items.length,
      selected_group: selected,
      library_version: String(
        data.all.reduce((n, c) => Math.max(n, c.updated_at), 0),
      ),
      recent_comic: recent[0],
      recent_progress: recent[0] ? progresses[recent[0].comic_name] : null,
      library_return_url: url.pathname + url.search,
      show_group_menu: !!names.length,
      group_menu_return_to: "comics",
      show_hidden_library: includeHidden,
      hidden_library_count: data.all.length - data.comics.length,
      hidden_comic_names: data.hidden
        .filter((h) => h.kind === "comic")
        .map((h) => h.target),
      hidden_group_names: data.hidden
        .filter((h) => h.kind === "group")
        .map((h) => h.target),
    });
  }
  if (
    [
      "novel_index",
      "novels_list",
      "novel_search",
      "novel_history",
      "novel_group_manager",
    ].includes(endpoint)
  ) {
    const items = await novels(env, user),
      names = await groups(env, user, "novel"),
      recent = items.filter((n) => n.progress),
      counts = countGroups(items, names);
    if (endpoint === "novel_index")
      return show("novel_index.html", {
        novels: items,
        spotlight: recent[0] || items[0] || null,
        recent_novels: recent.slice(0, 18),
      });
    if (endpoint === "novel_search") {
      const found = items.filter((n) =>
        (n.title + " " + n.author).toLowerCase().includes(q.toLowerCase()),
      );
      return show("novel_search.html", {
        query: q,
        novels: found,
        result_count: found.length,
      });
    }
    if (endpoint === "novel_history")
      return show("novel_history.html", { items: recent });
    if (endpoint === "novel_group_manager") {
      admin(user);
      return show("novel_groups.html", {
        novels: items,
        groups: names,
        group_counts: counts,
      });
    }
    const selected = names.includes(url.searchParams.get("group") || "")
        ? url.searchParams.get("group")!
        : "全部",
      page = pagination(
        items.filter((n) => selected === "全部" || n.group === selected),
        url.searchParams.get("page"),
      );
    return show("novels.html", {
      novels: page.items,
      pagination: page.pagination,
      groups: names,
      group_counts: counts,
      selected_group: selected,
    });
  }
  if (endpoint === "fanqie_novel_search") {
    let results: any[] = [],
      error = "";
    try {
      if (q) {
        const data = await sourceJson(
          env,
          `/v1/novels/search?q=${encodeURIComponent(q)}&page=${Math.max(1, Number(url.searchParams.get("page")) || 1)}`,
        );
        results = data.items || data.books || data.results || [];
      }
    } catch {
      error = "番茄搜索暂时不可用，请稍后重试";
    }
    const local = await novels(env, user);
    return show("novel_fanqie.html", {
      query: q,
      results,
      local_ids: Object.fromEntries(local.map((n) => [n.source_book_id, n])),
      error,
      fanqie_api_health: { ok: !error, message: error || "服务已配置" },
    });
  }
  if (endpoint === "novel_detail" || endpoint === "novel_reader") {
    const novel = (await novels(env, user)).find(
      (n) => n.id === params.novel_id,
    );
    if (!novel) throw new HttpError(404, "小说不存在");
    let index;
    try {
      index = await indexNovel(env, novel.id);
    } catch (error) {
      if (error instanceof HttpError && error.status === 503)
        return show("novel_detail.html", {
          novel,
          chapters: [],
          progress: null,
          reading_time: 0,
          index_pending: true,
        });
      throw error;
    }
    const progress = await one(
        env,
        "SELECT * FROM progress WHERE user_id=? AND book_id=?",
        user.user_id,
        novel.id,
      ),
      seconds = await one(
        env,
        "SELECT SUM(seconds) AS n FROM reading_time WHERE user_id=? AND media_id=?",
        user.user_id,
        "novel:" + novel.id,
      );
    novel.total_chapters = index.total_chapters;
    novel.chapter_count = index.total_chapters;
    if (endpoint === "novel_detail")
      return show("novel_detail.html", {
        novel,
        chapters: index.chapters,
        progress: serializeProgress(progress),
        reading_time: Math.ceil((seconds?.n || 0) / 60),
      });
    const requested = url.searchParams.has("start_chapter")
        ? Number(url.searchParams.get("start_chapter"))
        : progress?.chapter || 0,
      chapter = Math.max(
        0,
        Math.min(
          Number.isSafeInteger(requested) ? requested : 0,
          index.total_chapters - 1,
        ),
      );
    return show("novel_reader.html", {
      novel,
      chapter: await novelChapter(env, novel.id, chapter),
      saved_page: chapter === progress?.chapter ? progress.page : 0,
      server_progress_ms: progress?.client_ms || null,
      saved_anchor_paragraph: progress?.anchor_paragraph ?? null,
      saved_anchor_offset: progress?.anchor_offset ?? null,
      progress_key: "novel:" + novel.id,
      reader_user_id: publicUser(user).id,
      chapter_api_base_url: `/novel/${encodeURIComponent(novel.id)}/chapter`,
      reader_base_url: `/novel/${encodeURIComponent(novel.id)}/read`,
    });
  }
  if (endpoint === "comic_detail" || endpoint === "comic_reader") {
    const comic = await comicById(env, user, params.task_id),
      chapters = await all(
        env,
        "SELECT * FROM chapters WHERE comic_id=? AND state='ready' ORDER BY ordinal,id",
        comic.id,
      ),
      progress = await one(
        env,
        "SELECT * FROM comic_progress WHERE user_id=? AND comic_id=?",
        user.user_id,
        comic.id,
      ),
      task = {
        ...comic,
        id: comic.id,
        comic_name: comic.name,
        comic_format: comic.comic_format || 2,
        available_chapters: chapters.length,
        status: "completed",
      },
      names = await groups(env, user),
      hidden = await all(
        env,
        "SELECT kind,target FROM hidden_items WHERE user_id=?",
        user.user_id,
      ),
      anilist = await one(
        env,
        "SELECT * FROM anilist_links WHERE user_id=? AND comic_id=?",
        user.user_id,
        comic.id,
      );
    const libraryUrl = url.searchParams.get("return_to");
    const library_return_url =
      libraryUrl?.startsWith("/comics") && !libraryUrl.startsWith("//")
        ? libraryUrl
        : "/comics?group=" + encodeURIComponent(comic.group_name);
    if (endpoint === "comic_reader")
      return show("reader.html", {
        task,
        file_ext: comic.comic_format === 1 ? "pdf" : "cbz",
        start_chapter: url.searchParams.has("start_chapter")
          ? Math.max(0, Number(url.searchParams.get("start_chapter")) || 0)
          : progress?.chapter || 0,
        start_page: progress?.page || 0,
        library_return_url,
        deletion_path: comic.id,
        anilist_enabled: !!anilist,
      });
    const seconds = await one(
      env,
      "SELECT SUM(seconds) AS n FROM reading_time WHERE user_id=? AND media_id=?",
      user.user_id,
      comic.id,
    );
    return show("comic_detail.html", {
      task,
      chapters: chapters.map((c) => ({
        ...c,
        filename:
          c.legacy_filename || c.id + (c.format === "pdf" ? ".pdf" : ".cbz"),
        order: c.ordinal + 1,
        number: c.ordinal + 1,
      })),
      progress: serializeProgress(progress),
      reading_time: Math.ceil((seconds?.n || 0) / 60),
      comic_description: comic.description,
      anilist_link: anilist,
      anilist_metadata: JSON.parse(comic.metadata || "{}"),
      is_webdav_comic: comic.provider === "webdav",
      current_group: comic.group_name,
      groups: names,
      group_counts: {},
      group_total_count: 0,
      show_group_menu: true,
      group_menu_return_to: "detail",
      group_menu_task_id: comic.id,
      is_current_comic_hidden: hidden.some(
        (h) =>
          (h.kind === "comic" && h.target === comic.name) ||
          (h.kind === "group" && h.target === comic.group_name),
      ),
      is_current_comic_directly_hidden: hidden.some(
        (h) => h.kind === "comic" && h.target === comic.name,
      ),
      is_current_group_hidden: hidden.some(
        (h) => h.kind === "group" && h.target === comic.group_name,
      ),
      hidden_group_names: hidden
        .filter((h) => h.kind === "group")
        .map((h) => h.target),
      library_return_url,
      deletion_path: comic.id,
    });
  }
  if (endpoint === "users") {
    admin(user);
    const rows = await all(
      env,
      "SELECT id,username,role,can_download,can_view_adult FROM users ORDER BY role!='admin',username",
    );
    return show("users.html", { users: rows.map((u) => publicUser(u as any)) });
  }
  if (endpoint === "user_detail") {
    admin(user);
    const target = await getUser(env, params.user_id),
      names = await groups(env, user),
      novelNames = await groups(env, user, "novel"),
      novelAssigned = (
        await all(
          env,
          "SELECT group_name FROM novel_group_permissions WHERE user_id=?",
          target.id,
        )
      ).map((row) => row.group_name),
      assigned = (
        await all(
          env,
          "SELECT group_name FROM group_permissions WHERE user_id=?",
          target.id,
        )
      ).map((r) => r.group_name),
      logs = await all(
        env,
        "SELECT * FROM login_logs WHERE username=? ORDER BY created_at DESC LIMIT 5",
        target.username,
      );
    return show("user_detail.html", {
      user: publicUser(target as any),
      groups: names,
      user_groups: assigned,
      novel_groups: novelNames,
      user_novel_groups: novelAssigned,
      login_logs: logs.map((log) => ({
        ...log,
        login_time: date(log.created_at),
      })),
    });
  }
  if (endpoint === "change_password") return show("change_password.html");
  if (endpoint === "settings") {
    const account = await one(
      env,
      "SELECT name AS username FROM anilist_accounts WHERE user_id=?",
      user.user_id,
    );
    return show("settings.html", { anilist_account: account });
  }
  if (endpoint === "statistics")
    return show(
      "statistics.html",
      await statistics(
        env,
        user,
        url.searchParams.get("year"),
        context.library_mode,
      ),
    );
  if (endpoint === "download") {
    downloader(user);
    return show("download.html");
  }
  if (endpoint === "tasks" || endpoint === "progress") {
    downloader(user);
    const rows = await tasks(env, user);
    if (endpoint === "progress") {
      const task = rows.find((r) => r.id === params.task_id);
      if (!task) throw new HttpError(404, "任务不存在");
      return show("progress.html", {
        task_id: task.id,
        is_novel_task: !!task.is_novel,
        novel_cover_url: task.is_novel
          ? `/novels/fanqie/${task.source_id}/cover`
          : null,
      });
    }
    return show("tasks.html", {
      tasks: rows.map((t) => ({ ...t, start_time: date(t.created_at) })),
      active_task_count: rows.filter(
        (t) => !["completed", "cancelled", "error"].includes(t.status),
      ).length,
      deletable_task_count: rows.filter((t) =>
        ["completed", "cancelled", "error"].includes(t.status),
      ).length,
    });
  }
  if (endpoint === "update") {
    downloader(user);
    const data = await library(env, user, true),
      status = await updateStatus(
        env,
        user,
        url.searchParams.get("after") || "",
      );
    return show("update.html", {
      comics: data.comics.map((row) => row.name),
      update_candidates: status.candidates,
      update_checking: status.checking,
      update_check_status: status,
      last_update_checked_at: date(status.finished_at || null),
      comic_update_mode: await setting(env, "comic_update_mode", "manual"),
    });
  }
  if (endpoint === "anilist_connect")
    return show("anilist_connect.html", {
      authorize_url:
        "https://anilist.co/api/v2/oauth/authorize?client_id=51982&response_type=token",
    });
  if (endpoint === "anilist_match") {
    const provider =
        url.searchParams.get("provider") === "mangaupdates"
          ? "mangaupdates"
          : "anilist",
      metadataMode =
        url.searchParams.get("metadata") === "1" && user.role === "admin";
    const comic = await comicById(env, user, params.comic_name),
      account = await one(
        env,
        "SELECT name AS username FROM anilist_accounts WHERE user_id=?",
        user.user_id,
      ),
      link = await one(
        env,
        "SELECT * FROM anilist_links WHERE user_id=? AND comic_id=?",
        user.user_id,
        comic.id,
      ),
      chapters = await all(
        env,
        "SELECT title FROM chapters WHERE comic_id=?",
        comic.id,
      );
    return show("anilist_match.html", {
      comic_name: comic.name,
      account,
      link,
      query: q || comic.name,
      results: q
        ? await metadataSearch(q, metadataMode ? provider : "anilist")
        : [],
      searched: !!q,
      metadata_mode: metadataMode,
      metadata: JSON.parse(comic.metadata || "{}"),
      chapter_count: chapters.length,
      suggested_first_chapter: 1,
      metadata_provider: metadataMode ? provider : "anilist",
      metadata_source_name:
        metadataMode && provider === "mangaupdates"
          ? "MangaUpdates"
          : "AniList",
    });
  }
  if (endpoint === "webdav_settings") {
    admin(user);
    const mount = await one(
      env,
      "SELECT id,url,base_url,root,cache_gb,username,state,message FROM remote_mounts LIMIT 1",
    );
    return show("settings_webdav.html", {
      webdav: {
        configured: !!mount,
        enabled: !!mount,
        url: mount?.base_url || mount?.url || "",
        root: mount?.root || "/",
        cache_gb: mount?.cache_gb || 10,
        password_set: !!mount,
        username: mount?.username || "",
        password: "",
        state: mount?.state || "idle",
        message: mount?.message || "",
        sync_status: mount?.state || "idle",
        base_url: mount?.url || "",
        sync_enabled: !!mount,
      },
    });
  }
  if (endpoint === "scan_settings") {
    admin(user);
    return show("settings_scan.html", {
      scan_paths: await all(
        env,
        "SELECT key AS id,value AS path,1 AS enabled FROM settings WHERE key LIKE 'scan_path:%'",
      ),
    });
  }
  throw new HttpError(404, "页面不存在");
}
