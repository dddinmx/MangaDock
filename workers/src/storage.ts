import { notifyCompute } from "./compute-scheduler";
import { HttpError, now, type Env } from "./index";
import { all, one, run, reserve, type Row } from "./library";
export async function releaseReservation(
  env: Env,
  key: string,
  owner: string | null,
) {
  if (!owner) return;
  // A failed PUT can still have committed its object. Reconcile before releasing.
  const object = await env.LIBRARY.head(key);
  if (object) {
    await run(
      env,
      "UPDATE storage_ledger SET state='stored',size=?,owner=NULL,lease_until=0 WHERE key=? AND owner=?",
      object.size,
      key,
      owner,
    );
    return;
  }
  await run(
    env,
    "DELETE FROM storage_ledger WHERE key=? AND owner=? AND state='reserved' AND NOT EXISTS(SELECT 1 FROM uploads WHERE object_key=? AND state='uploading')",
    key,
    owner,
    key,
  );
}
export async function putStream(
  env: Env,
  key: string,
  body: ReadableStream<Uint8Array>,
  length: number,
  mime: string,
  sha256?: string,
) {
  let owner: string | null;
  try {
    owner = await reserve(env, key, length);
  } catch (error) {
    await body.cancel().catch(() => {});
    throw error;
  }
  if (!owner) {
    await body.cancel();
    return;
  }
  const abort = new AbortController();
  let fixed: FixedLengthStream | undefined;
  let transferring = true;
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    fixed = new FixedLengthStream(length);
    // Fail immediately if R2 rejects without draining the readable side.
    // allSettled could wait forever on the backpressured pipe in that case.
    await Promise.race([
      Promise.all([
        body.pipeTo(fixed.writable, { signal: abort.signal }),
        env.LIBRARY.put(key, fixed.readable, {
          httpMetadata: { contentType: mime },
          ...(sha256 ? { sha256 } : {}),
        }),
      ]),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          abort.abort();
          reject(new HttpError(502, "文件传输超时"));
        }, 120000);
      }),
    ]);
    transferring = false;
    await run(
      env,
      "UPDATE storage_ledger SET state='stored',owner=NULL,lease_until=0 WHERE key=? AND owner=?",
      key,
      owner,
    );
  } catch (error) {
    abort.abort();
    // A rejected upload may not have locked its readable stream at all.
    await fixed?.readable.cancel().catch(() => {});
    await releaseReservation(env, key, owner).catch(() => {});
    if (transferring) throw new HttpError(502, "文件传输失败或长度不一致");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}
const owned = (key: string) => /^(comics|compute|imports|fanqie)\//.test(key);
export async function queueGarbage(env: Env, key: string, prefix = false) {
  if (!owned(key)) return;
  await run(
    env,
    "INSERT INTO storage_gc(key,prefix,created_at) VALUES (?,?,?) ON CONFLICT(key) DO NOTHING",
    key,
    prefix ? 1 : 0,
    now(),
  );
  await notifyCompute(env);
}
export async function collectComicGarbage(env: Env, comic: Row) {
  // Persist the entire cleanup plan before deleting metadata; survive failures.
  const media =
    ("cover_key" in comic
      ? comic
      : await one(
          env,
          "SELECT cover_key,hero_key,mobile_hero_key,banner_key FROM comics WHERE id=?",
          comic.id,
        )) || {};
  const chapters = await all(
    env,
    "SELECT object_key,manifest_key FROM chapters WHERE comic_id=?",
    comic.id,
  );
  const parents = await all(
    env,
    "SELECT json_extract(payload,'$.source_key') AS key FROM comic_jobs WHERE comic_id=? AND json_extract(payload,'$.source_key') IS NOT NULL",
    comic.id,
  );
  const sources = [
    ...Object.values(media)
      .filter((value) => typeof value === "string")
      .map((key) => ({ key })),
    ...chapters.flatMap((row) => [
      { key: row.object_key },
      { key: row.manifest_key },
    ]),
    ...parents,
  ];
  const jobs = await all(
    env,
    `SELECT id,input_key FROM compute_jobs WHERE json_extract(payload,'$.comic_id')=? OR (json_extract(payload,'$.media_id')=? AND json_extract(payload,'$.kind')='comic') OR json_extract(payload,'$.parent_job') IN (SELECT id FROM comic_jobs WHERE comic_id=?) OR json_extract(payload,'$.chapter_id') IN (SELECT id FROM chapters WHERE comic_id=?)`,
    ...Array(4).fill(comic.id),
  );
  const targets = new Map<string, boolean>([[`comics/${comic.id}/`, true]]);
  for (const { key } of sources)
    if (key && owned(key))
      targets.set(
        key.startsWith("compute/")
          ? key.split("/").slice(0, 2).join("/") + "/"
          : key,
        key.startsWith("compute/"),
      );
  for (const job of jobs) {
    targets.set(`compute/${job.id}/`, true);
    if (owned(job.input_key)) targets.set(job.input_key, false);
  }
  // Chunk to avoid oversized batches / D1's parameter ceiling.
  const entries = [...targets];
  for (let i = 0; i < entries.length; i += 50)
    await env.DB.batch(
      entries
        .slice(i, i + 50)
        .map(([key, prefix]) =>
          env.DB.prepare(
            "INSERT INTO storage_gc(key,prefix,created_at) VALUES (?,?,?) ON CONFLICT(key) DO NOTHING",
          ).bind(key, prefix ? 1 : 0, now()),
        ),
    );
  // Invalidate ALL linked jobs, including completed rows whose inputs will be collected.
  await notifyCompute(env);
  for (let i = 0; i < jobs.length; i += 50)
    await env.DB.batch(
      jobs
        .slice(i, i + 50)
        .map((job) =>
          env.DB.prepare(
            "UPDATE compute_jobs SET state='cancelled',claim_token=NULL,lease_until=0 WHERE id=?",
          ).bind(job.id),
        ),
    );
}
async function referenced(env: Env, key: string, prefix: boolean) {
  const upper = prefix
    ? key.slice(0, -1) + String.fromCharCode(key.charCodeAt(key.length - 1) + 1)
    : key;
  const condition = prefix ? "key>=? AND key<?" : "key=?";
  const args: string[] = [];
  const has = (table: string, columns: string[], extra = "") =>
    `EXISTS(SELECT 1 FROM ${table} WHERE (${columns
      .map((column) => {
        args.push(key);
        if (prefix) args.push(upper);
        return condition.replaceAll("key", column);
      })
      .join(" OR ")}) ${extra} LIMIT 1)`;
  const query = [
    has("comics", ["cover_key", "hero_key", "mobile_hero_key", "banner_key"]),
    has("chapters", ["object_key", "manifest_key"]),
    has("books", [
      "object_key",
      "cover_key",
      "hero_key",
      "mobile_hero_key",
      "index_key",
    ]),
    has("uploads", ["object_key"], "AND state='uploading'"),
    has(
      "comic_jobs",
      ["json_extract(payload,'$.source_key')"],
      "AND state IN ('queued','running','waiting')",
    ),
    has(
      "compute_jobs",
      ["input_key"],
      "AND state IN ('queued','running','completed')",
    ),
  ].join(" OR ");
  return !!(await one(env, "SELECT 1 WHERE " + query, ...args));
}
export async function cleanupStorage(env: Env, maximum = 100) {
  let removed = 0;
  for (const item of await all(
    env,
    "SELECT * FROM storage_gc ORDER BY created_at LIMIT 5",
  )) {
    if (await referenced(env, item.key, !!item.prefix)) {
      await run(env, "DELETE FROM storage_gc WHERE key=?", item.key);
      continue;
    }
    const objects = item.prefix
      ? (
          await env.LIBRARY.list({
            prefix: item.key,
            limit: Math.max(1, maximum - removed),
          })
        ).objects
      : [{ key: item.key }];
    const keys = objects.map((o) => o.key);
    if (keys.length) {
      await env.LIBRARY.delete(keys);
      await run(
        env,
        `DELETE FROM storage_ledger WHERE key IN (${keys.map(() => "?").join(",")})`,
        ...keys,
      );
      removed += keys.length;
    }
    if (
      !item.prefix ||
      !(await env.LIBRARY.list({ prefix: item.key, limit: 1 })).objects.length
    )
      await run(env, "DELETE FROM storage_gc WHERE key=?", item.key);
    if (removed >= maximum) break;
  }
  return removed;
}
export async function cleanupUploads(env: Env) {
  for (const row of await all(
    env,
    "SELECT * FROM uploads WHERE (state='uploading' AND created_at<?) OR state='aborted' LIMIT 10",
    now() - 86400,
  )) {
    if (row.upload_id)
      await env.LIBRARY.resumeMultipartUpload(
        row.object_key,
        row.upload_id,
      ).abort();
    await run(env, "UPDATE uploads SET state='cleaned' WHERE id=?", row.id);
    await releaseReservation(env, row.object_key, row.id);
    if (row.object_key.startsWith("compute/"))
      await queueGarbage(
        env,
        row.object_key.split("/").slice(0, 2).join("/") + "/",
        true,
      );
  }
  for (const row of await all(
    env,
    "SELECT key,owner FROM storage_ledger s WHERE state='reserved' AND created_at<? AND NOT EXISTS(SELECT 1 FROM uploads u WHERE u.object_key=s.key AND u.state='uploading') LIMIT 20",
    now() - 86400,
  )) {
    await releaseReservation(env, row.key, row.owner);
    // Reservations made before ownership was introduced.
    if (!row.owner) {
      if (await env.LIBRARY.head(row.key))
        await run(
          env,
          "UPDATE storage_ledger SET state='stored' WHERE key=? AND owner IS NULL",
          row.key,
        );
      else
        await run(
          env,
          "DELETE FROM storage_ledger WHERE key=? AND owner IS NULL AND state='reserved'",
          row.key,
        );
    }
  }
}
