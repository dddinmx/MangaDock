import { test, expect } from "@playwright/test";
import { spawnSync } from "node:child_process";
import { readFile } from "node:fs/promises";
const user = {
  id: 1,
  username: "admin",
  role: "admin",
  is_admin: true,
  can_download: false,
  can_view_adult: false,
};
async function ready(page: any) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all(
      [...document.images]
        .filter((image) => {
          const rect = image.getBoundingClientRect();
          return rect.top < innerHeight && rect.bottom > 0;
        })
        .map((image) => image.decode().catch(() => {})),
    );
  });
}
function reference(
  template: string,
  endpoint: string,
  values: any,
  mode = "comic",
) {
  const result = spawnSync(
    process.env.MANGADOCK_REFERENCE_PYTHON || "python3",
    ["scripts/render-reference.py"],
    {
      input: JSON.stringify({ template, endpoint, values, mode }),
      encoding: "utf8",
    },
  );
  if (result.status !== 0) throw new Error(result.stderr);
  return result.stdout;
}
const comic = {
  id: "c_test",
  comic_name: "本地测试漫画",
  name: "本地测试漫画",
  group: "默认分组",
  comic_format: 2,
  status: "completed",
  available_chapters: 1,
  total_chapters: 1,
  description: "这是用于原版界面和漫画阅读验证的作品简介。",
  has_progress: false,
};
const novel = {
  id: "123456789",
  novel_id: "123456789",
  title: "本地测试小说",
  author: "测试作者",
  description: "",
  chapter_count: 2,
  total_chapters: 2,
  group: "默认分组",
  last_chapter: 0,
  progress: null,
  cover_version: 0,
  fanqie_book_id: "123456789",
};
const cases = [
  {
    path: "/novels/fanqie",
    template: "novel_fanqie.html",
    endpoint: "fanqie_novel_search",
    mode: "novel",
    values: {
      query: "",
      results: [],
      local_ids: {},
      error: "",
      fanqie_api_health: { ok: true, message: "服务已配置" },
    },
  },
  {
    path: "/anilist/match/c_test",
    template: "anilist_match.html",
    endpoint: "anilist_match",
    values: {
      comic_name: comic.name,
      account: null,
      link: null,
      query: comic.name,
      results: [],
      searched: false,
      metadata_mode: false,
      metadata: {},
      chapter_count: 1,
      suggested_first_chapter: 1,
      metadata_provider: "anilist",
      metadata_source_name: "AniList",
    },
  },
  {
    path: "/search?q=本地",
    template: "search.html",
    endpoint: "search_page",
    values: { query: "本地", comics: [comic], result_count: 1 },
  },
  {
    path: "/history",
    template: "history.html",
    endpoint: "history_page",
    values: { items: [] },
  },
  {
    path: "/novels/search?q=本地",
    template: "novel_search.html",
    endpoint: "novel_search",
    mode: "novel",
    values: { query: "本地", novels: [novel], result_count: 1 },
  },
  {
    path: "/novels/history",
    template: "novel_history.html",
    endpoint: "novel_history",
    mode: "novel",
    values: { items: [] },
  },
  {
    path: "/novels/groups",
    template: "novel_groups.html",
    endpoint: "novel_group_manager",
    mode: "novel",
    values: {
      novels: [novel],
      groups: ["默认分组"],
      group_counts: { 默认分组: 1 },
    },
  },
  {
    path: "/tasks",
    template: "tasks.html",
    endpoint: "tasks",
    values: { tasks: [], active_task_count: 0, deletable_task_count: 0 },
  },
  {
    path: "/update",
    template: "update.html",
    endpoint: "update",
    values: {
      comics: [comic],
      update_candidates: [],
      update_checking: false,
      last_update_checked_at: null,
      comic_update_mode: "manual",
    },
  },
  {
    path: "/anilist/connect",
    template: "anilist_connect.html",
    endpoint: "anilist_connect",
    values: {
      authorize_url:
        "https://anilist.co/api/v2/oauth/authorize?client_id=51982&response_type=token",
    },
  },
  {
    path: "/settings/scan_paths",
    template: "settings_scan.html",
    endpoint: "scan_settings",
    values: { scan_paths: [] },
  },
  {
    path: "/settings/webdav",
    template: "settings_webdav.html",
    endpoint: "webdav_settings",
    values: {
      webdav: {
        configured: false,
        enabled: false,
        url: "",
        root: "/",
        cache_gb: 10,
        password_set: false,
        username: "",
        password: "",
        state: "idle",
        message: "",
        sync_status: "idle",
        base_url: "",
        sync_enabled: false,
      },
    },
  },
  {
    path: "/",
    template: "index.html",
    endpoint: "index",
    values: {
      spotlight: comic,
      hero_comics: [comic],
      recent_comics: [],
      latest_comics: [comic],
      group_rails: [{ name: "默认分组", comics: [comic] }],
    },
  },
  {
    path: "/novels",
    template: "novel_index.html",
    endpoint: "novel_index",
    mode: "novel",
    values: { spotlight: novel, novels: [novel], recent_novels: [] },
  },
  {
    path: "/novels/library",
    template: "novels.html",
    endpoint: "novels_list",
    mode: "novel",
    values: {
      novels: [novel],
      groups: ["默认分组"],
      group_counts: { 默认分组: 1 },
      selected_group: "全部",
      pagination: {
        page: 1,
        total_pages: 1,
        total_items: 1,
        page_links: [1],
        has_prev: false,
        has_next: false,
      },
    },
  },
  {
    path: "/novel/123456789",
    template: "novel_detail.html",
    endpoint: "novel_detail",
    mode: "novel",
    values: {
      novel,
      chapters: [
        { index: 0, title: "第一章" },
        { index: 1, title: "第二章" },
      ],
      progress: null,
      reading_time: 0,
    },
  },
  {
    path: "/comic/c_test",
    template: "comic_detail.html",
    endpoint: "comic_detail",
    values: {
      task: comic,
      chapters: [{ filename: "ch_test.cbz", number: 1, title: "第一话" }],
      progress: null,
      reading_time: 0,
      comic_description: comic.description,
      current_group: "默认分组",
      groups: ["默认分组"],
      group_counts: {},
      group_total_count: 0,
      show_group_menu: true,
      group_menu_return_to: "detail",
      group_menu_task_id: "c_test",
      is_current_comic_hidden: false,
      is_current_comic_directly_hidden: false,
      is_current_group_hidden: false,
      hidden_group_names: [],
      library_return_url: "/comics?group=默认分组",
      deletion_path: "c_test",
      anilist_metadata: {},
      is_webdav_comic: false,
    },
  },
  {
    path: "/settings",
    template: "settings.html",
    endpoint: "settings",
    values: { anilist_account: null },
  },
  {
    path: "/users",
    template: "users.html",
    endpoint: "users",
    values: { users: [user] },
  },
  {
    path: "/change_password",
    template: "change_password.html",
    endpoint: "change_password",
    values: {},
  },
  {
    path: "/download",
    template: "download.html",
    endpoint: "download",
    values: { adult_content_enabled: false },
  },
  {
    path: "/comics/groups",
    template: "comic_groups.html",
    endpoint: "comic_group_manager",
    values: {
      comics: [
        {
          id: "c_test",
          comic_name: "本地测试漫画",
          name: "本地测试漫画",
          group: "默认分组",
        },
      ],
      groups: ["默认分组"],
      group_counts: { 默认分组: 1 },
    },
  },
  {
    path: "/comics",
    template: "comics.html",
    endpoint: "comics_list",
    values: {
      tasks: [
        {
          id: "c_test",
          comic_name: "本地测试漫画",
          group: "默认分组",
          available_chapters: 1,
        },
      ],
      groups: ["默认分组"],
      group_counts: { 默认分组: 1 },
      group_total_count: 1,
      selected_group_count: 1,
      pagination: {
        page: 1,
        total_pages: 1,
        total_items: 1,
        page_links: [1],
        has_prev: false,
        has_next: false,
      },
      selected_group: "全部",
      show_group_menu: true,
      group_menu_return_to: "comics",
      show_hidden_library: false,
      hidden_library_count: 0,
      hidden_comic_names: [],
      hidden_group_names: [],
      progresses: {},
      library_version: "1700000000",
      library_return_url: "/comics",
    },
  },
];
for (const width of [1280, 390])
  test(`original Jinja screenshots match migrated pages at ${width}px`, async ({
    page,
  }) => {
    test.setTimeout(120000);
    await page.setViewportSize({ width, height: 900 });
    await page.addInitScript(() => {
      localStorage.setItem("mangadock-install-muted-until", "9999999999999");
      localStorage.setItem("mangadock-cover-motion", "off");
    });
    await page.goto("/login");
    await page.getByLabel("用户名").fill("admin");
    await page.getByLabel("密码").fill("local-e2e-password-2026");
    await page.locator("#loginButton").click();
    await expect(page.locator(".md-topnav")).toBeVisible();
    for (const item of cases) {
      await page.goto(item.path);
      await ready(page);
      await page.locator(".md-topnav").waitFor({ state: "attached" });
      await page.screenshot({
        path: `test-results/parity-${width}-${item.endpoint}-actual.png`,
        animations: "disabled",
      });
      await page.route(
        "http://localhost:8787" + item.path,
        (route) =>
          route.fulfill({
            status: 200,
            contentType: "text/html",
            body: reference(
              item.template,
              item.endpoint,
              item.values,
              (item as any).mode || "comic",
            ),
          }),
        { times: 1 },
      );
      await page.goto(item.path);
      await ready(page);
      await page.screenshot({
        path: `test-results/parity-${width}-${item.endpoint}-reference.png`,
        animations: "disabled",
      });
      const result = spawnSync(
        process.env.MANGADOCK_REFERENCE_PYTHON || "python3",
        [
          "scripts/compare-screenshots.py",
          `test-results/parity-${width}-${item.endpoint}-reference.png`,
          `test-results/parity-${width}-${item.endpoint}-actual.png`,
        ],
        { encoding: "utf8" },
      );
      expect(result.status, result.stdout + " " + result.stderr).toBe(0);
    }
  });
