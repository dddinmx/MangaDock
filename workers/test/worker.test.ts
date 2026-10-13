import { env, SELF, fetchMock, applyD1Migrations } from "cloudflare:test";
import { zipSync, strToU8, strFromU8, unzipSync, gunzipSync } from "fflate";
import { runJob } from "../src/downloads";
import { scanLibrary } from "../src/scans";
import { indexNovel } from "../src/novels";
import { decodeImages, providerFor, unpackManhuagui } from "../src/providers";
import { beforeAll, beforeEach, afterEach, describe, it, expect } from "vitest";
import worker, { processJob, verifyPassword, pilotRoute } from "../src/index";
import type { Env } from "../src/index";
const bindings = env as unknown as Env & {
  TEST_MIGRATIONS: Parameters<typeof applyD1Migrations>[1];
};
const origin = "https://mangadock.test",
  password = "test-password-strong-2026";
let encoded: string;
beforeAll(async () => {
  await applyD1Migrations(bindings.DB, bindings.TEST_MIGRATIONS);
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    "PBKDF2",
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    {
      name: "PBKDF2",
      salt: new TextEncoder().encode("test-salt"),
      iterations: 100000,
      hash: "SHA-256",
    },
    key,
    256,
  );
  encoded =
    "pbkdf2:sha256:100000$test-salt$" +
    [...new Uint8Array(bits)]
      .map((x) => x.toString(16).padStart(2, "0"))
      .join("");
});
beforeEach(async () => {
  fetchMock.activate();
  fetchMock.disableNetConnect();
  await bindings.DB.batch(
    [
      "upload_parts",
      "uploads",
      "compute_jobs",
      "remote_mounts",
      "anilist_links",
      "anilist_accounts",
      "reading_reports",
      "reading_time",
      "hidden_items",
      "group_permissions",
      "comic_progress",
      "comic_jobs",
      "chapters",
      "comics",
      "progress",
      "jobs",
      "books",
      "sessions",
      "login_logs",
      "login_attempts",
      "users",
    ].map((table) => bindings.DB.prepare(`DELETE FROM ${table}`)),
  );
  await bindings.LIBRARY.delete(
    (await bindings.LIBRARY.list()).objects.map((obj) => obj.key),
  );
  await bindings.DB.prepare(
    "DELETE FROM library_groups WHERE name!='默认分组'",
  ).run();
  await bindings.DB.prepare("DELETE FROM settings").run();
  await bindings.DB.prepare("DELETE FROM storage_ledger").run();
  await bindings.DB.prepare(
    "INSERT INTO users(id,username,password_hash,role) VALUES (?,?,?,?)",
  )
    .bind("admin", "admin", encoded, "admin")
    .run();
});
afterEach(() => {
  fetchMock.assertNoPendingInterceptors();
  fetchMock.deactivate();
});
async function login(username = "admin", pwd = password) {
  const response = await SELF.fetch(origin + "/api/v1/auth/login", {
    method: "POST",
    headers: { Origin: origin, "Content-Type": "application/json" },
    body: JSON.stringify({ username, password: pwd }),
  });
  return {
    response,
    cookie: response.headers.get("Set-Cookie")?.split(";")[0] || "",
    payload: (await response.json()) as any,
  };
}
const auth = (cookie: string, csrf: string) => ({
  Cookie: cookie,
  Origin: origin,
  "Content-Type": "application/json",
  "X-CSRFToken": csrf,
});
const artifactBytes = new TextEncoder().encode("realistic-test-artifact-bytes");
async function completedJob(overrides: Record<string, unknown> = {}) {
  const digest = [
    ...new Uint8Array(await crypto.subtle.digest("SHA-256", artifactBytes)),
  ]
    .map((x) => x.toString(16).padStart(2, "0"))
    .join("");
  return {
    id: "upstream-job",
    status: "completed",
    metadata: { title: "测试小说", author: "测试作者" },
    artifact: {
      url: "/v1/jobs/upstream-job/artifact",
      size: artifactBytes.length,
      sha256: digest,
      ...overrides,
    },
  };
}
function mock(path: string, data: unknown, method = "GET") {
  return fetchMock
    .get("https://fanqie.dddinmx.cn")
    .intercept({ path, method })
    .reply(200, JSON.stringify({ ok: true, data }), {
      headers: { "content-type": "application/json" },
    });
}
async function insertJob(state = "queued", remote: string | null = null) {
  await bindings.DB.prepare(
    "INSERT INTO jobs(id,user_id,source_id,remote_id,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
  )
    .bind(
      "job",
      "admin",
      "123456789",
      remote,
      state,
      Math.floor(Date.now() / 1000),
      Math.floor(Date.now() / 1000),
    )
    .run();
}
describe("authentication and access", () => {
  it("protects files, progress, jobs and library", async () => {
    for (const path of [
      "/api/books",
      "/api/books/123456789/file",
      "/api/books/123456789/progress",
      "/api/jobs",
    ])
      expect((await SELF.fetch(origin + path)).status).toBe(401);
  });
  it("checks passwords, secure cookie, origin, CSRF and logout", async () => {
    expect(await verifyPassword("wrong", encoded)).toBe(false);
    const { response, cookie, payload } = await login();
    expect(response.status).toBe(200);
    expect(response.headers.get("Set-Cookie")).toContain(
      "HttpOnly; Secure; SameSite=Lax",
    );
    expect(
      (
        await SELF.fetch(origin + "/api/v1/auth/me", {
          headers: { Cookie: cookie },
        })
      ).status,
    ).toBe(200);
    expect(
      (
        await SELF.fetch(origin + "/api/jobs", {
          method: "POST",
          headers: {
            Cookie: cookie,
            Origin: origin,
            "Content-Type": "application/json",
          },
          body: "{}",
        })
      ).status,
    ).toBe(403);
    expect(
      (
        await SELF.fetch(origin + "/api/v1/auth/logout", {
          method: "POST",
          headers: {
            ...auth(cookie, payload.data.csrf_token),
            Origin: "https://attacker.test",
          },
          body: "{}",
        })
      ).status,
    ).toBe(403);
    expect(
      (
        await SELF.fetch(origin + "/api/v1/auth/logout", {
          method: "POST",
          headers: auth(cookie, payload.data.csrf_token),
          body: "{}",
        })
      ).status,
    ).toBe(200);
    expect(
      (await SELF.fetch(origin + "/api/books", { headers: { Cookie: cookie } }))
        .status,
    ).toBe(401);
  });
  it("verifies legacy 260k and new 600k passwords in the Durable Object", async () => {
    for (const key of ["TEST_LEGACY_HASH", "TEST_STRONG_HASH"]) {
      await bindings.DB.prepare("UPDATE users SET password_hash=? WHERE id=?")
        .bind((env as any)[key], "admin")
        .run();
      expect((await login()).response.status).toBe(200);
    }
  });
  it("creates durable downloads and rejects active or already stored duplicates", async () => {
    const { cookie, payload } = await login(),
      sent: any[] = [];
    const testEnv = {
      ...bindings,
      DOWNLOADS: {
        send: async (body: any) => {
          sent.push(body);
        },
      } as any,
    };
    const create = () =>
      pilotRoute(
        new Request(origin + "/api/jobs", {
          method: "POST",
          headers: auth(cookie, payload.data.csrf_token),
          body: JSON.stringify({ book_id: "123456789" }),
        }),
        testEnv,
      );
    expect((await create()).status).toBe(202);
    expect(sent).toHaveLength(1);
    expect((await create()).status).toBe(409);
    await bindings.DB.prepare("UPDATE jobs SET state='error'").run();
    await bindings.DB.prepare(
      "INSERT INTO books (id,source_id,title,author,object_key,size,sha256,created_at) VALUES ('123456789','123456789','stored','author','file',4,?,?)",
    )
      .bind("0".repeat(64), Math.floor(Date.now() / 1000))
      .run();
    expect((await create()).status).toBe(409);
    expect(sent).toHaveLength(1);
  });
  it("protects one-time source initialization and stores no credential in D1", async () => {
    const { cookie, payload } = await login();
    const request = () =>
      new Request(origin + "/api/source/register", {
        method: "POST",
        headers: auth(cookie, payload.data.csrf_token),
        body: JSON.stringify({
          instance: "edge-instance",
          registration: "fqr_" + "r".repeat(43),
        }),
      });
    expect((await pilotRoute(request(), bindings)).status).toBe(409);
    const fakeToken = "fqk_" + "a".repeat(43);
    mock("/v1/register", { token: fakeToken }, "POST");
    const response = await pilotRoute(request(), {
      ...bindings,
      FANQIE_API_TOKEN: undefined,
    });
    expect(response.status).toBe(200);
    expect(((await response.json()) as any).data.token).toBe(fakeToken);
  });
  it("rate limits in D1", async () => {
    for (let i = 0; i < 5; i++)
      expect((await login("admin", "wrong")).response.status).toBe(401);
    expect((await login()).response.status).toBe(429);
  });
  it("restricts download to admin", async () => {
    await bindings.DB.prepare(
      "INSERT INTO users (id,username,password_hash,role) VALUES ('reader','reader',?,'user')",
    )
      .bind(encoded)
      .run();
    const { cookie, payload } = await login("reader");
    expect(
      (
        await SELF.fetch(origin + "/api/jobs", {
          method: "POST",
          headers: auth(cookie, payload.data.csrf_token),
          body: JSON.stringify({ book_id: "123456789" }),
        })
      ).status,
    ).toBe(403);
  });
});
describe("source → streaming R2 → reading → D1 progress", () => {
  it("stores artifact, serves bytes/ranges and preserves newest per-user progress", async () => {
    await insertJob();
    mock("/v1/jobs", { id: "upstream-job" }, "POST");
    mock("/v1/jobs/upstream-job", await completedJob());
    fetchMock
      .get("https://fanqie.dddinmx.cn")
      .intercept({
        path: "/v1/jobs/upstream-job/artifact",
        headers: { authorization: "Bearer test-token" },
      })
      .reply(200, artifactBytes, {
        headers: { "content-length": String(artifactBytes.length) },
      });
    await processJob(bindings, "job");
    expect(
      (
        await bindings.DB.prepare("SELECT state FROM jobs WHERE id=?")
          .bind("job")
          .first<any>()
      )?.state,
    ).toBe("completed");
    const { cookie, payload } = await login(),
      headers = auth(cookie, payload.data.csrf_token);
    const file = await SELF.fetch(origin + "/api/books/123456789/file", {
      headers: { Cookie: cookie },
    });
    expect(file.status).toBe(200);
    expect(new Uint8Array(await file.arrayBuffer())).toEqual(artifactBytes);
    const range = await SELF.fetch(origin + "/api/books/123456789/file", {
      headers: { Cookie: cookie, Range: "bytes=0-3" },
    });
    expect(range.status).toBe(206);
    expect(await range.text()).toBe("real");
    const ms = Date.now();
    for (const data of [
      { chapter: 1, page: 2, scroll: 0, client_ms: ms },
      { chapter: 0, page: 0, scroll: 0, client_ms: ms - 10 },
    ])
      expect(
        (
          await SELF.fetch(origin + "/api/books/123456789/progress", {
            method: "PUT",
            headers,
            body: JSON.stringify(data),
          })
        ).status,
      ).toBe(200);
    const progress = (await (
      await SELF.fetch(origin + "/api/books/123456789/progress", {
        headers: { Cookie: cookie },
      })
    ).json()) as any;
    expect(progress.data.chapter).toBe(1);
    expect(progress.data.page).toBe(2);
    await bindings.DB.prepare(
      "INSERT INTO users (id,username,password_hash,role) VALUES ('other','other',?,'user')",
    )
      .bind(encoded)
      .run();
    const other = await login("other");
    expect(
      (
        (await (
          await SELF.fetch(origin + "/api/books/123456789/progress", {
            headers: { Cookie: other.cookie },
          })
        ).json()) as any
      ).data,
    ).toBeNull();
    expect(
      (
        await SELF.fetch(origin + "/api/books/123456789/progress", {
          method: "PUT",
          headers,
          body: JSON.stringify({
            chapter: -1,
            page: 0,
            scroll: 0,
            client_ms: ms,
          }),
        })
      ).status,
    ).toBe(400);
    await processJob(bindings, "job");
    expect(
      (
        await bindings.DB.prepare(
          "SELECT count(*) AS n FROM books",
        ).first<any>()
      )?.n,
    ).toBe(1);
  });
  it("rejects checksum mismatch before publication", async () => {
    await insertJob("running", "upstream-job");
    mock(
      "/v1/jobs/upstream-job",
      await completedJob({ sha256: "0".repeat(64) }),
    );
    fetchMock
      .get("https://fanqie.dddinmx.cn")
      .intercept({ path: "/v1/jobs/upstream-job/artifact" })
      .reply(200, artifactBytes);
    await processJob(bindings, "job");
    expect(
      await bindings.DB.prepare("SELECT id FROM books").first(),
    ).toBeNull();
    expect(
      (await bindings.DB.prepare("SELECT state FROM jobs").first<any>())?.state,
    ).toBe("error");
  });
  it("rejects artifact URLs from other hosts", async () => {
    await insertJob("running", "upstream-job");
    mock(
      "/v1/jobs/upstream-job",
      await completedJob({ url: "https://attacker.test/token" }),
    );
    await processJob(bindings, "job");
    expect(
      (await bindings.DB.prepare("SELECT state FROM jobs").first<any>())?.state,
    ).toBe("error");
  });
  it("rejects oversize artifacts before transfer", async () => {
    await insertJob("running", "upstream-job");
    mock("/v1/jobs/upstream-job", await completedJob({ size: 2147483649 }));
    await processJob(bindings, "job");
    expect(
      await bindings.DB.prepare("SELECT id FROM books").first(),
    ).toBeNull();
    expect(
      (await bindings.DB.prepare("SELECT state FROM jobs").first<any>())?.state,
    ).toBe("error");
  });
  it("enforces the library storage budget before transferring bytes", async () => {
    await insertJob("running", "upstream-job");
    mock("/v1/jobs/upstream-job", await completedJob());
    await processJob({ ...bindings, MAX_LIBRARY_BYTES: "10" }, "job");
    expect(
      await bindings.DB.prepare("SELECT id FROM books").first(),
    ).toBeNull();
    expect(
      (await bindings.DB.prepare("SELECT message FROM jobs").first<any>())
        ?.message,
    ).toContain("免费预算上限");
  });
  it("keeps pending work while upstream generates EPUB", async () => {
    await insertJob("running", "upstream-job");
    mock("/v1/jobs/upstream-job", { id: "upstream-job", status: "running" });
    await processJob(bindings, "job");
    expect(
      await bindings.DB.prepare(
        "SELECT state,lease_until,attempts FROM jobs",
      ).first(),
    ).toMatchObject({ state: "running", lease_until: 0, attempts: 1 });
  });
  it("does not retry uncertain upstream submissions", async () => {
    await insertJob();
    fetchMock
      .get("https://fanqie.dddinmx.cn")
      .intercept({ path: "/v1/jobs", method: "POST" })
      .reply(503, "unavailable");
    await processJob(bindings, "job");
    await processJob(bindings, "job");
    expect(
      (await bindings.DB.prepare("SELECT state FROM jobs").first<any>())?.state,
    ).toBe("error");
  });
});

const fullGet = (path: string, cookie: string) =>
  SELF.fetch(origin + path, { headers: { Cookie: cookie } });
async function comicFixture() {
  await bindings.DB.prepare(
    "INSERT INTO comics(id,name,provider,source_url,description,created_at,updated_at) VALUES ('c_test','原版漫画','baozimh_org','https://baozimh.org/manga/test','作品简介',?,?)",
  )
    .bind(Math.floor(Date.now() / 1000), Math.floor(Date.now() / 1000))
    .run();
  await bindings.DB.prepare(
    "INSERT INTO chapters(id,comic_id,ordinal,title,source_id,manifest_key,page_count,state) VALUES ('ch_test','c_test',0,'第一话','remote-chapter','fixture/manifest',1,'ready')",
  ).run();
  await bindings.LIBRARY.put(
    "fixture/manifest",
    JSON.stringify([{ key: "fixture/page", width: 3, height: 4 }]),
    { httpMetadata: { contentType: "application/json" } },
  );
  await bindings.LIBRARY.put(
    "fixture/page",
    new Uint8Array([137, 80, 78, 71]),
    { httpMetadata: { contentType: "image/png" } },
  );
}
async function novelFixture() {
  const zip = zipSync({
    "META-INF/container.xml": strToU8(
      '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>',
    ),
    "book.opf": strToU8(
      '<package><manifest><item id="a" href="one.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest><spine><itemref idref="a"/></spine></package>',
    ),
    "nav.xhtml": strToU8(
      '<html><body><nav><a href="one.xhtml">第一章</a></nav></body></html>',
    ),
    "one.xhtml": strToU8(
      "<html><body><h1>第一章</h1><p>原文内容</p><script>bad()</script><p>原文内容</p></body></html>",
    ),
  });
  await bindings.LIBRARY.put("fixture/book", zip);
  await bindings.DB.prepare(
    "INSERT INTO books(id,source_id,title,author,object_key,size,sha256,created_at) VALUES ('123456789','123456789','原版小说','作者','fixture/book',?,?,?)",
  )
    .bind(zip.length, "1".repeat(64), Math.floor(Date.now() / 1000))
    .run();
}
describe("full legacy routes and permissions", () => {
  it("renders original home, shelves, settings, admin pages and readers", async () => {
    await comicFixture();
    await novelFixture();
    const { cookie } = await login();
    for (const path of [
      "/",
      "/comics",
      "/comics/groups",
      "/search?q=原版",
      "/history",
      "/settings",
      "/statistics",
      "/users",
      "/users/admin",
      "/change_password",
      "/download",
      "/update",
      "/tasks",
      "/novels",
      "/novels/library",
      "/novels/groups",
      "/novels/search?q=原版",
      "/novels/history",
      "/novel/123456789",
      "/novel/123456789/read",
      "/comic/c_test",
      "/reader/c_test",
      "/settings/webdav",
      "/settings/scan_paths",
      "/anilist/connect",
    ]) {
      const response = await fullGet(path, cookie);
      expect(response.status, path).toBe(200);
      expect(response.headers.get("Content-Type")).toContain("text/html");
      expect(await response.text()).not.toContain("接口尚未迁移");
    }
  });
  it("enforces comic groups on shelf, detail, chapters, page lists, bytes, progress and covers", async () => {
    await comicFixture();
    await bindings.DB.prepare(
      "INSERT INTO users(id,username,password_hash,role) VALUES ('reader','reader',?,'user')",
    )
      .bind(encoded)
      .run();
    const { cookie } = await login("reader");
    expect(
      ((await (await fullGet("/api/comics", cookie)).json()) as any).items,
    ).toHaveLength(0);
    for (const path of [
      "/comic/c_test",
      "/reader/c_test",
      "/api/chapters/c_test",
      "/api/comics/c_test",
      "/api/comics/c_test/chapters/ch_test/pages",
      "/api/comics/c_test/chapters/ch_test/pages/0",
      "/get_progress/原版漫画",
      "/static/cover/原版漫画.jpg",
      "/api/comic-pages/原版漫画/ch_test.cbz?page=0",
    ])
      expect((await fullGet(path, cookie)).status, path).toBe(403);
    await bindings.DB.prepare(
      "INSERT INTO group_permissions VALUES ('reader','默认分组')",
    ).run();
    expect(
      ((await (await fullGet("/api/comics", cookie)).json()) as any).items,
    ).toHaveLength(1);
    expect(
      (await fullGet("/api/comics/c_test/chapters/ch_test/pages/0", cookie))
        .status,
    ).toBe(200);
  });
  it("supports Basic Auth clients without exposing unauthenticated files", async () => {
    await comicFixture();
    const headers = { Authorization: "Basic " + btoa("admin:" + password) };
    expect((await SELF.fetch(origin + "/api/comics", { headers })).status).toBe(
      200,
    );
    expect(
      (
        await SELF.fetch(
          origin + "/api/comics/c_test/chapters/ch_test/pages/0",
          { headers },
        )
      ).status,
    ).toBe(200);
    expect(
      (await SELF.fetch(origin + "/api/comics/c_test/chapters/ch_test/pages/0"))
        .status,
    ).toBe(401);
  });
  it("creates users, assigns groups and revokes sessions on password reset", async () => {
    const adminSession = await login(),
      headers = auth(adminSession.cookie, adminSession.payload.data.csrf_token);
    const response = await SELF.fetch(origin + "/api/v1/users", {
      method: "POST",
      headers,
      body: JSON.stringify({
        username: "new-reader",
        password: "new-strong-password",
        role: "user",
        groups: ["默认分组"],
      }),
    });
    expect(response.status, await response.clone().text()).toBe(201);
    const target = ((await response.json()) as any).data.user;
    expect(target.password_hash).toBeUndefined();
    const reader = await login("new-reader", "new-strong-password");
    expect(reader.response.status).toBe(200);
    expect(
      (
        await SELF.fetch(origin + "/users/" + target.id + "/password", {
          method: "POST",
          redirect: "manual",
          headers,
          body: JSON.stringify({
            password: "reset-strong-password",
            confirm_password: "reset-strong-password",
          }),
        })
      ).status,
    ).toBe(303);
    expect((await fullGet("/api/books", reader.cookie)).status).toBe(401);
    expect(
      (await login("new-reader", "reset-strong-password")).response.status,
    ).toBe(200);
  }, 15000);
  it("preserves image pagination contract, actual bytes and CBZ page order", async () => {
    await comicFixture();
    const { cookie } = await login(),
      chapters = (await (
        await fullGet("/api/chapters/c_test", cookie)
      ).json()) as any;
    expect(chapters.chapters[0].filename).toBe("ch_test.cbz");
    const manifest = (await (
      await fullGet("/api/comic-pages/原版漫画/ch_test.cbz", cookie)
    ).json()) as any;
    expect(manifest).toEqual({ pages: 1, dimensions: [[3, 4]] });
    const page = await fullGet(
      "/api/comic-pages/原版漫画/ch_test.cbz?page=0",
      cookie,
    );
    expect(page.headers.get("Content-Type")).toBe("image/png");
    expect(new Uint8Array(await page.arrayBuffer())).toEqual(
      new Uint8Array([137, 80, 78, 71]),
    );
    const file = await fullGet(
      "/api/comics/c_test/chapters/ch_test/file",
      cookie,
    );
    expect(
      Object.keys(unzipSync(new Uint8Array(await file.arrayBuffer()))),
    ).toEqual(["00001.png"]);
  });
  it("indexes EPUB nav and original paragraph order with scripts excluded", async () => {
    await novelFixture();
    const index = await indexNovel(bindings, "123456789");
    expect(index.chapters[0].title).toBe("第一章");
    const chapter = JSON.parse(
      strFromU8(
        gunzipSync(
          new Uint8Array(
            await (await bindings.LIBRARY.get(
              index.chapters[0].key,
            ))!.arrayBuffer(),
          ),
        ),
      ),
    ) as any;
    expect(chapter.paragraphs).toEqual(["第一章", "原文内容"]);
    expect(JSON.stringify(chapter)).not.toContain("bad()");
  });
  it("deduplicates reading time and rejects old comic progress snapshots", async () => {
    await comicFixture();
    const { cookie, payload } = await login(),
      headers = auth(cookie, payload.data.csrf_token),
      stamp = Date.now();
    for (const [seconds, clock, page] of [
      [61, stamp, 2],
      [61, stamp + 1, 2],
      [30, stamp - 1, 1],
      [90, stamp + 2, 3],
    ])
      expect(
        (
          await SELF.fetch(origin + "/save_progress", {
            method: "POST",
            headers,
            body: JSON.stringify({
              comic_name: "原版漫画",
              chapter: 0,
              page,
              scroll_position: 12,
              client_progress_ms: clock,
              reading_time: seconds,
              reading_session_id: "test-session",
            }),
          })
        ).status,
      ).toBe(200);
    const progress = (await (
      await fullGet("/get_progress/原版漫画", cookie)
    ).json()) as any;
    expect(progress.last_page).toBe(3);
    expect(
      (
        await bindings.DB.prepare(
          "SELECT SUM(seconds) AS total FROM reading_time",
        ).first<any>()
      )?.total,
    ).toBe(90);
  });
  it("rechecks download capability and respects adult source controls", async () => {
    const { cookie, payload } = await login(),
      headers = auth(cookie, payload.data.csrf_token);
    expect(
      (
        await SELF.fetch(origin + "/api/v1/downloads", {
          method: "POST",
          headers,
          body: JSON.stringify({
            url: "https://www.kalamanhua.com/comic/100.html",
          }),
        })
      ).status,
    ).toBe(403);
    await bindings.DB.prepare(
      "INSERT INTO users(id,username,password_hash,role,can_download) VALUES ('reader','reader',?,'user',1)",
    )
      .bind(encoded)
      .run();
    const reader = await login("reader");
    expect(
      (
        await SELF.fetch(origin + "/api/v1/downloads", {
          method: "POST",
          headers: auth(reader.cookie, reader.payload.data.csrf_token),
          body: JSON.stringify({ url: "https://baozimh.org/manga/test" }),
        })
      ).status,
    ).toBe(403);
  });
  it("blocks non-admin group, user, settings and cloud compute writes", async () => {
    await bindings.DB.prepare(
      "INSERT INTO users(id,username,password_hash,role) VALUES ('reader','reader',?,'user')",
    )
      .bind(encoded)
      .run();
    const { cookie, payload } = await login("reader"),
      headers = auth(cookie, payload.data.csrf_token);
    for (const path of [
      "/api/v1/groups",
      "/api/v1/users",
      "/api/admin/compute/discover",
    ])
      expect(
        (
          await SELF.fetch(origin + path, {
            method: "POST",
            headers,
            body: "{}",
          })
        ).status,
      ).toBe(403);
    expect((await fullGet("/users", cookie)).status).toBe(403);
    expect((await fullGet("/api/admin/compute/jobs", cookie)).status).toBe(403);
  });
});

describe("comic downloads, cloud compute, migration and remote reading", () => {
  it("runs a durable comic source → pages → private R2 → ready chapter pipeline", async () => {
    const { cookie } = await login();
    const now = Math.floor(Date.now() / 1000),
      sent: any[] = [];
    const testEnv = {
      ...bindings,
      DOWNLOADS: {
        send: async (value: any) => {
          sent.push(value);
        },
      } as any,
    };
    await bindings.DB.prepare(
      "INSERT INTO comic_jobs(id,user_id,kind,payload,created_at,updated_at) VALUES ('source','admin','source',?,?,?)",
    )
      .bind(
        JSON.stringify({
          url: "https://baozimh.org/manga/test",
          provider: "baozimh_org",
          group: "默认分组",
          format: 2,
          max_chapters: 1,
        }),
        now,
        now,
      )
      .run();
    fetchMock
      .get("https://baozimh.org")
      .intercept({ path: "/manga/test" })
      .reply(200, '<h1>队列漫画</h1><div data-mid="42"></div>');
    fetchMock
      .get("https://v2.apikk.top")
      .intercept({ path: "/api/manga/get?mid=42&mode=all" })
      .reply(
        200,
        JSON.stringify({
          code: 200,
          data: {
            title: "队列漫画",
            chapters: [{ id: "7", attributes: { order: 1, title: "第一话" } }],
          },
        }),
      );
    await runJob(testEnv, "source");
    const chapterJob = sent.shift();
    expect(chapterJob.kind).toBe("comic");
    fetchMock
      .get("https://v2.apikk.top")
      .intercept({ path: "/api/v2/chapter/getinfo?m=42&c=7" })
      .reply(
        200,
        JSON.stringify({
          code: 200,
          data: {
            info: {
              images: {
                line: 2,
                images: [
                  { url: "/one.png", order: 1 },
                  { url: "/two.png", order: 2 },
                ],
              },
            },
          },
        }),
      );
    await runJob(testEnv, chapterJob.id);
    const png = Uint8Array.from(
      atob(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aGqkAAAAASUVORK5CYII=",
      ),
      (c) => c.charCodeAt(0),
    );
    for (const path of ["/one.png", "/two.png"])
      fetchMock
        .get("https://c-nd2-1.6wm.top")
        .intercept({ path })
        .reply(200, png, {
          headers: {
            "content-type": "image/png",
            "content-length": String(png.length),
          },
        });
    for (const page of sent) await runJob(testEnv, page.id);
    expect(
      (
        await bindings.DB.prepare(
          "SELECT state FROM comic_jobs WHERE id='source'",
        ).first<any>()
      ).state,
    ).toBe("completed");
    const comic = await bindings.DB.prepare(
        "SELECT id FROM comics",
      ).first<any>(),
      chapter = await bindings.DB.prepare(
        "SELECT id FROM chapters",
      ).first<any>();
    const pages = (await (
      await fullGet(
        `/api/comics/${comic.id}/chapters/${chapter.id}/pages`,
        cookie,
      )
    ).json()) as any;
    expect(pages.total_pages).toBe(2);
    const first = await fullGet(pages.pages[0].image_url, cookie);
    expect(new Uint8Array(await first.arrayBuffer())).toEqual(png);
  });
  it("accepts only raster image bytes and leaves corrupt downloads unpublished", async () => {
    const now = Math.floor(Date.now() / 1000);
    await comicFixture();
    await bindings.DB.prepare(
      "INSERT INTO comic_jobs(id,user_id,comic_id,kind,payload,created_at,updated_at) VALUES ('bad','admin','c_test','page',?,?,?)",
    )
      .bind(
        JSON.stringify({
          url: "https://images.example/evil.svg",
          key: "fixture/evil",
          referer: "https://baozimh.org/",
        }),
        now,
        now,
      )
      .run();
    fetchMock
      .get("https://images.example")
      .intercept({ path: "/evil.svg" })
      .reply(200, "<svg><script>bad()</script></svg>", {
        headers: { "content-type": "image/svg+xml" },
      });
    await runJob(
      { ...bindings, DOWNLOADS: { send: async () => {} } as any },
      "bad",
    );
    expect(await bindings.LIBRARY.head("fixture/evil")).toBeNull();
  });
  it("claims cloud jobs once and rejects incomplete or stale cover outputs", async () => {
    await comicFixture();
    await bindings.LIBRARY.put("fixture/cover", new Uint8Array([1]));
    await bindings.DB.prepare(
      "UPDATE comics SET cover_key='fixture/cover'",
    ).run();
    const { cookie, payload } = await login(),
      headers = auth(cookie, payload.data.csrf_token);
    expect(
      (
        await SELF.fetch(origin + "/api/admin/compute/discover", {
          method: "POST",
          headers,
          body: "{}",
        })
      ).status,
    ).toBe(200);
    const jobs = (
      (await (await fullGet("/api/admin/compute/jobs", cookie)).json()) as any
    ).data;
    const job = jobs.find((j: any) => j.kind === "cover");
    expect(
      (
        await SELF.fetch(origin + `/api/admin/compute/jobs/${job.id}/claim`, {
          method: "POST",
          headers,
          body: "{}",
        })
      ).status,
    ).toBe(200);
    expect(
      (
        await SELF.fetch(origin + `/api/admin/compute/jobs/${job.id}/claim`, {
          method: "POST",
          headers,
          body: "{}",
        })
      ).status,
    ).toBe(409);
    expect(
      (
        await SELF.fetch(
          origin + `/api/admin/compute/jobs/${job.id}/complete`,
          { method: "POST", headers, body: "{}" },
        )
      ).status,
    ).toBe(409);
    for (const variant of ["original", "hero", "mobile"])
      await bindings.LIBRARY.put(
        `compute/${job.id}/${variant}`,
        new Uint8Array([1]),
      );
    await bindings.DB.prepare("UPDATE comics SET cover_key='new-cover'").run();
    expect(
      (
        await SELF.fetch(
          origin + `/api/admin/compute/jobs/${job.id}/complete`,
          { method: "POST", headers, body: "{}" },
        )
      ).status,
    ).toBe(200);
    expect(
      (await bindings.DB.prepare("SELECT hero_key FROM comics").first<any>())
        .hero_key,
    ).toBeNull();
  });
  it("verifies multipart parts and restores only fully uploaded metadata", async () => {
    const { cookie, payload } = await login(),
      headers = auth(cookie, payload.data.csrf_token),
      bytes = new Uint8Array([1, 2, 3, 4]),
      sha = Array.from(
        new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
      )
        .map((b) => b.toString(16).padStart(2, "0"))
        .join("");
    const upload = (await (
        await SELF.fetch(origin + "/api/admin/uploads", {
          method: "POST",
          headers,
          body: JSON.stringify({ size: 4, sha256: sha, extension: "cbz" }),
        })
      ).json()) as any,
      id = upload.data.id;
    expect(
      (
        await SELF.fetch(origin + `/api/admin/uploads/${id}/parts/1`, {
          method: "PUT",
          headers: {
            ...headers,
            "Content-Type": "application/octet-stream",
            "X-Content-SHA256": "0".repeat(64),
          },
          body: bytes,
        })
      ).status,
    ).toBe(422);
    expect(
      (
        await SELF.fetch(origin + `/api/admin/uploads/${id}/parts/1`, {
          method: "PUT",
          headers: {
            ...headers,
            "Content-Type": "application/octet-stream",
            "X-Content-SHA256": sha,
          },
          body: bytes,
        })
      ).status,
    ).toBe(200);
    expect(
      (
        await SELF.fetch(origin + `/api/admin/uploads/${id}/complete`, {
          method: "POST",
          headers,
          body: "{}",
        })
      ).status,
    ).toBe(200);
    expect(
      new Uint8Array(
        await (
          await fullGet(`/api/admin/uploads/${id}/file`, cookie)
        ).arrayBuffer(),
      ),
    ).toEqual(bytes);
    expect(
      (
        await SELF.fetch(origin + "/api/admin/import/metadata", {
          method: "POST",
          headers,
          body: JSON.stringify({
            kind: "novels",
            records: [
              {
                id: "legacy",
                title: "旧小说",
                object_key: "missing",
                size: 4,
                sha256: sha,
              },
            ],
          }),
        })
      ).status,
    ).toBe(409);
  });
  it("encrypts integration credentials at rest and never renders them in settings", async () => {
    const { cookie, payload } = await login(),
      headers = auth(cookie, payload.data.csrf_token);
    const response = await SELF.fetch(origin + "/settings/webdav", {
      method: "POST",
      redirect: "manual",
      headers,
      body: JSON.stringify({
        url: "https://dav.example/dav/",
        username: "reader",
        password: "private-mount-password",
        root: "/books",
        cache_gb: 10,
      }),
    });
    expect(response.status, await response.clone().text()).toBe(303);
    const mount = await bindings.DB.prepare(
      "SELECT * FROM remote_mounts",
    ).first<any>();
    expect(mount.password).not.toContain("private-mount-password");
    const html = await (await fullGet("/settings/webdav", cookie)).text();
    expect(html).not.toContain("private-mount-password");
    expect(html).toContain("密码已保存");
  });
  it("reads remote CBZ central directory and verifies a page with HTTP ranges", async () => {
    const { encrypt } = await import("../src/security");
    const zip = zipSync({
      "002.png": new Uint8Array([137, 80, 78, 71, 2]),
      "001.png": new Uint8Array([137, 80, 78, 71, 1]),
    });
    await bindings.DB.prepare(
      "INSERT INTO remote_mounts(id,url,username,password) VALUES ('test','https://dav.example/books/','reader',?)",
    )
      .bind(await encrypt(bindings, "secret"))
      .run();
    await comicFixture();
    await bindings.DB.prepare(
      "UPDATE chapters SET object_key=NULL,manifest_key=NULL,remote_url='https://dav.example/books/book.cbz',mount_id='test',size=?,format='cbz' WHERE id='ch_test'",
    )
      .bind(zip.length)
      .run();
    fetchMock
      .get("https://dav.example")
      .intercept({
        path: "/books/book.cbz",
        headers: { range: "bytes=0-" + (zip.length - 1) },
      })
      .reply(206, zip);
    const tail = new DataView(zip.buffer),
      end = zip.length - 22,
      offset = tail.getUint32(end + 16, true),
      len = tail.getUint32(end + 12, true);
    fetchMock
      .get("https://dav.example")
      .intercept({
        path: "/books/book.cbz",
        headers: { range: `bytes=${offset}-${offset + len - 1}` },
      })
      .reply(206, zip.subarray(offset, offset + len));
    const { cookie } = await login();
    const list = (await (
      await fullGet("/api/comic-pages/原版漫画/ch_test.cbz", cookie)
    ).json()) as any;
    expect(list.pages).toBe(2);
    const manifest = (await (await bindings.LIBRARY.get(
      (
        await bindings.DB.prepare(
          "SELECT manifest_key FROM chapters",
        ).first<any>()
      ).manifest_key,
    ))!.json()) as any;
    expect(manifest[0].name).toBe("001.png");
    const page = manifest[0],
      header = new DataView(zip.buffer),
      start =
        page.offset +
        30 +
        header.getUint16(page.offset + 26, true) +
        header.getUint16(page.offset + 28, true);
    for (const [from, to] of [
      [page.offset, page.offset + 29],
      [start, start + page.compressed - 1],
    ])
      fetchMock
        .get("https://dav.example")
        .intercept({
          path: "/books/book.cbz",
          headers: { range: `bytes=${from}-${to}` },
        })
        .reply(206, zip.subarray(from, to + 1));
    const image = await fullGet(
      "/api/comic-pages/原版漫画/ch_test.cbz?page=0",
      cookie,
    );
    expect(new Uint8Array(await image.arrayBuffer())).toEqual(
      new Uint8Array([137, 80, 78, 71, 1]),
    );
  });
});

describe("cloud R2 library scan", () => {
  it("indexes a private CBZ prefix without copying its images, and keeps direct reading usable", async () => {
    const raw = zipSync({
      "01.png": new Uint8Array([137, 80, 78, 71, 1]),
      "02.png": new Uint8Array([137, 80, 78, 71, 2]),
    });
    await bindings.LIBRARY.put("scan-fixture/云端扫描漫画/第一话.cbz", raw);
    await bindings.DB.prepare(
      "INSERT INTO settings(key,value) VALUES ('scan_path:test','r2://library/scan-fixture/')",
    ).run();
    const scan = await scanLibrary(bindings, {
      user_id: "admin",
      role: "admin",
      username: "admin",
    } as any);
    expect(scan.task_ids).toHaveLength(1);
    await runJob(bindings, scan.task_ids[0]);
    const comic = await bindings.DB.prepare(
      "SELECT * FROM comics WHERE name='云端扫描漫画'",
    ).first<any>();
    expect(comic.provider).toBe("r2");
    const chapter = await bindings.DB.prepare(
      "SELECT * FROM chapters WHERE comic_id=?",
    )
      .bind(comic.id)
      .first<any>();
    expect(chapter.object_key).toBe("scan-fixture/云端扫描漫画/第一话.cbz");
    expect(chapter.manifest_key).toBeNull();
    const { cookie } = await login();
    const response = await SELF.fetch(
      origin +
        "/api/comic-pages/" +
        encodeURIComponent(comic.name) +
        "/" +
        encodeURIComponent("第一话.cbz") +
        "?page=0",
      { headers: { Cookie: cookie } },
    );
    expect(response.status).toBe(200);
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(
      new Uint8Array([137, 80, 78, 71, 1]),
    );
  });
});
import "./job-loop";
