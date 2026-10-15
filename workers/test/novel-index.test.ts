import { applyD1Migrations, env } from "cloudflare:test";
import { beforeAll, beforeEach, expect, it } from "vitest";
import { computeApi } from "../src/compute";
import { now, type Env, type Identity } from "../src/index";
const bindings = env as unknown as Env & {
  TEST_MIGRATIONS: Parameters<typeof applyD1Migrations>[1];
};
const prefix = "compute/novel-index-test/lease";
const admin = {
  user_id: "novel-index-admin",
  username: "admin",
  role: "admin",
  can_download: 1,
  can_view_adult: 1,
  csrf: "",
  token_hash: "",
} as Identity;
beforeAll(async () => {
  await applyD1Migrations(bindings.DB, bindings.TEST_MIGRATIONS);
});
beforeEach(async () => {
  await bindings.DB.prepare("DELETE FROM compute_jobs").run();
  await bindings.DB.prepare("DELETE FROM books").run();
  await bindings.DB.prepare(
    "INSERT INTO books(id,source_id,title,author,object_key,size,sha256,created_at) VALUES ('book','source','Title','Author','input.epub',1,'sha',1)",
  ).run();
  await bindings.DB.prepare(
    "INSERT INTO compute_jobs(id,kind,input_key,payload,state,attempts,claim_token,lease_until,created_at,updated_at) VALUES ('novel-index-test','index-epub','input.epub',?,'running',1,'lease',?,1,1)",
  )
    .bind(JSON.stringify({ book_id: "book", sha256: "sha" }), now() + 1800)
    .run();
});
const complete = (custom: Env = bindings) =>
  computeApi(
    new Request(
      "https://fixture.invalid/api/admin/compute/jobs/novel-index-test/complete",
      {
        method: "POST",
        headers: {
          "X-Compute-Lease": "lease",
          "Content-Type": "application/json",
        },
        body: "{}",
      },
    ),
    custom,
    admin,
  );
async function index(chapters: any[], extra: Record<string, unknown> = {}) {
  await bindings.LIBRARY.put(
    prefix + "/index",
    JSON.stringify({ chapters, total_chapters: chapters.length, ...extra }),
  );
}
const chapter = (i: number) => ({
  index: i,
  title: `Chapter ${i}`,
  key: prefix + `/chapter/${i}`,
  gzip: true,
});
it("publishes a long novel with bounded real R2 listings, metadata and its embedded cover", async () => {
  const chapters = Array.from({ length: 1001 }, (_, i) => chapter(i));
  await index(chapters, {
    metadata: { description: "原书简介" },
    cover_key: prefix + "/original",
  });
  await bindings.LIBRARY.put(
    prefix + "/original",
    new Uint8Array([255, 216, 255]),
    { httpMetadata: { contentType: "image/jpeg" } },
  );
  let listings = 0,
    heads = 0;
  const custom = {
    ...bindings,
    LIBRARY: {
      get: bindings.LIBRARY.get.bind(bindings.LIBRARY),
      head: async (key: string) => {
        heads++;
        return bindings.LIBRARY.head(key);
      },
      list: async (options: R2ListOptions) => {
        listings++;
        expect(options.prefix).toBe(prefix + "/chapter/");
        return options.cursor
          ? {
              objects: [{ key: chapters[1000].key, size: 1 }],
              truncated: false,
            }
          : {
              objects: chapters
                .slice(0, 1000)
                .map((c) => ({ key: c.key, size: 1 })),
              truncated: true,
              cursor: "next",
            };
      },
    },
  } as Env;
  expect((await complete(custom))?.status).toBe(200);
  expect(listings).toBe(2);
  expect(heads).toBe(1);
  expect(
    await bindings.DB.prepare(
      "SELECT chapter_count,description,cover_key,index_key FROM books WHERE id='book'",
    ).first(),
  ).toMatchObject({
    chapter_count: 1001,
    description: "原书简介",
    cover_key: prefix + "/original",
    index_key: prefix + "/index",
  });
});
it("rejects actual missing or empty chapter objects without publishing readiness", async () => {
  await index([chapter(0), chapter(1)]);
  const custom = {
    ...bindings,
    LIBRARY: {
      get: bindings.LIBRARY.get.bind(bindings.LIBRARY),
      list: async () => ({
        objects: [
          { key: chapter(0).key, size: 1 },
          { key: chapter(1).key, size: 0 },
        ],
        truncated: false,
      }),
    },
  } as Env;
  await expect(complete(custom)).rejects.toMatchObject({ status: 409 });
  expect(
    (
      await bindings.DB.prepare(
        "SELECT index_key FROM books WHERE id='book'",
      ).first<any>()
    ).index_key,
  ).toBeNull();
});
it("rejects duplicate ordinals and cross-lease chapter paths", async () => {
  for (const chapters of [
    [chapter(0), chapter(0)],
    [{ ...chapter(0), key: "compute/other/lease/chapter/0" }],
  ]) {
    await index(chapters);
    await expect(complete()).rejects.toMatchObject({ status: 400 });
  }
});
it("preserves an existing custom cover and description", async () => {
  await bindings.DB.prepare(
    "UPDATE books SET description='自定义简介',cover_key='custom-cover' WHERE id='book'",
  ).run();
  await bindings.LIBRARY.put(prefix + "/chapter/0", "real chapter");
  await index([chapter(0)], { metadata: { description: "EPUB简介" } });
  expect((await complete())?.status).toBe(200);
  expect(
    await bindings.DB.prepare(
      "SELECT description,cover_key FROM books WHERE id='book'",
    ).first(),
  ).toEqual({ description: "自定义简介", cover_key: "custom-cover" });
});

it("explicit novel repair preserves failed job attempts and deduplicates new index work", async () => {
  await bindings.DB.prepare(
    "UPDATE compute_jobs SET state='error',attempts=3,lease_until=0 WHERE id='novel-index-test'",
  ).run();
  const repair = (title = "Title") =>
    computeApi(
      new Request("https://fixture.invalid/api/admin/compute/novel-index", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ book_id: "book", expected_title: title }),
      }),
      bindings,
      admin,
    );
  await expect(repair("Wrong title")).rejects.toMatchObject({ status: 404 });
  const first = await (await repair())!.json<any>();
  const second = await (await repair())!.json<any>();
  expect(second.data.job_id).toBe(first.data.job_id);
  expect(
    await bindings.DB.prepare(
      "SELECT state,attempts FROM compute_jobs WHERE id='novel-index-test'",
    ).first(),
  ).toEqual({ state: "error", attempts: 3 });
  expect(
    (
      await bindings.DB.prepare(
        "SELECT COUNT(*) n FROM compute_jobs WHERE kind='index-epub' AND state='queued'",
      ).first<any>()
    ).n,
  ).toBe(1);
  expect(
    (
      await bindings.DB.prepare(
        "SELECT COUNT(*) n FROM comic_jobs",
      ).first<any>()
    ).n,
  ).toBe(0);
});
