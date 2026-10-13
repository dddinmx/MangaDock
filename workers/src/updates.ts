import { now, type Env, type Identity } from "./index";
import {
  all,
  one,
  run,
  setting,
  setSetting,
  decodeLibraryCursor,
  encodeLibraryCursor,
  type Row,
} from "./library";
import { loadComic } from "./providers";
import { createDownload, enqueue } from "./downloads";
/** Comics enqueued by one paging message. One queue message then checks one comic. */
export const UPDATE_PAGE_BUDGET = 20;
export type UpdateMessage = {
  kind?: string;
  cursor?: string;
  budget?: number;
  generation?: string;
  userId?: string;
  automatic?: boolean;
  comicId?: string;
};
const GENERATION = "update_check_generation";
const CANDIDATES = "update_candidates";
function storedCandidates(raw: string): Row[] {
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}
function pageBudget(value: unknown) {
  const budget = Math.trunc(Number(value));
  if (!Number.isFinite(budget)) return UPDATE_PAGE_BUDGET;
  return Math.min(50, Math.max(1, budget));
}
async function actor(env: Env, userId: string): Promise<Identity | null> {
  const row = await one(
    env,
    "SELECT id AS user_id,username,role,can_download,can_view_adult FROM users WHERE id=?",
    userId,
  );
  if (!row) return null;
  return {
    user_id: String(row.user_id),
    username: String(row.username),
    role: String(row.role),
    can_download: Number(row.can_download) || 0,
    can_view_adult: Number(row.can_view_adult) || 0,
    csrf: "",
    token_hash: "",
  };
}
function visibleSql() {
  return "(?='admin' OR EXISTS(SELECT 1 FROM group_permissions gp WHERE gp.user_id=? AND gp.group_name=c.group_name))";
}
async function stillCurrent(env: Env, generation: string) {
  return (await setting(env, GENERATION, "")) === generation;
}
async function recordCandidate(env: Env, generation: string, candidate: Row) {
  for (let attempt = 0; attempt < 5; attempt++) {
    if (!(await stillCurrent(env, generation))) return;
    const current = await setting(env, CANDIDATES, "[]");
    const list = storedCandidates(current).filter((row) => row.id !== candidate.id);
    list.push(candidate);
    const written = await run(
      env,
      "UPDATE settings SET value=? WHERE key=? AND value=? AND (SELECT value FROM settings WHERE key=?)=?",
      JSON.stringify(list),
      CANDIDATES,
      current,
      GENERATION,
      generation,
    );
    if (written.meta.changes) return;
  }
  throw new Error("更新结果写入冲突");
}
function candidateBase(comic: Row): Row {
  return {
    ...comic,
    comic_name: comic.name,
    group: comic.group_name,
    comic_format: comic.comic_format || 2,
  };
}
async function processUpdateComic(
  env: Env,
  user: Identity,
  message: UpdateMessage,
) {
  if (!message.comicId || !message.generation) return;
  const comic = await one(
    env,
    `SELECT c.* FROM comics c WHERE c.id=? AND ${visibleSql()}`,
    message.comicId,
    user.role,
    user.user_id,
  );
  if (!comic || !(await stillCurrent(env, message.generation))) return;
  try {
    const remote = await loadComic(comic.source_url, env),
      local = await all(
        env,
        "SELECT source_id FROM chapters WHERE comic_id=? AND state='ready'",
        comic.id,
      ),
      pending = remote.chapters.filter(
        (ch) => !local.some((row) => row.source_id === ch.id),
      );
    if (!(await stillCurrent(env, message.generation))) return;
    if (!pending.length) return;
    await recordCandidate(env, message.generation, {
      ...candidateBase(comic),
      status: "completed",
      pending_chapters: pending.length,
      remote_total_chapters: remote.chapters.length,
      local_total_chapters: local.length,
      latest_chapter_title: pending.at(-1)?.title,
    });
    if (message.automatic)
      try {
        await createDownload(env, user, {
          url: comic.source_url,
          group: comic.group_name,
          format: comic.comic_format || 2,
          is_update: true,
        });
      } catch {}
  } catch {
    if (!(await stillCurrent(env, message.generation))) return;
    await recordCandidate(env, message.generation, {
      ...candidateBase(comic),
      status: "error",
      error_message: "来源暂时不可用",
    });
  }
}
async function enqueueUpdatePage(
  env: Env,
  user: Identity,
  message: UpdateMessage,
) {
  if (!message.generation || !message.userId) return;
  const budget = pageBudget(message.budget);
  const cursor = message.cursor ? decodeLibraryCursor(message.cursor) : null;
  if (message.cursor && !cursor) return;
  const args: unknown[] = [user.role, user.user_id];
  let cursorSql = "";
  if (cursor) {
    cursorSql = " AND (c.updated_at<? OR (c.updated_at=? AND c.id<?))";
    args.push(cursor.updated_at, cursor.updated_at, cursor.id);
  }
  const rows = await all(
    env,
    `SELECT c.* FROM comics c WHERE ${visibleSql()}${cursorSql} ORDER BY c.updated_at DESC, c.id DESC LIMIT ?`,
    ...args,
    budget + 1,
  );
  if (!(await stillCurrent(env, message.generation))) return;
  const page = rows.slice(0, budget);
  const bodies: UpdateMessage[] = page.map((comic) => ({
    kind: "update-comic",
    comicId: String(comic.id),
    generation: message.generation,
    userId: message.userId,
    automatic: !!message.automatic,
  }));
  if (rows.length > budget)
    bodies.push({
      kind: "update-page",
      cursor: encodeLibraryCursor(page.at(-1)!),
      budget,
      generation: message.generation,
      userId: message.userId,
      automatic: !!message.automatic,
    });
  else await setSetting(env, "updates_checked_at", String(now()));
  if (bodies.length)
    await env.DOWNLOADS.sendBatch(bodies.map((body) => ({ body })));
}
/** Queue consumer entry: one comic, or a page that enqueues the next budget plus a cursor. */
export async function dispatchUpdate(env: Env, message: UpdateMessage) {
  if (message.kind !== "update-page" && message.kind !== "update-comic") return;
  if (!message.generation || !message.userId) return;
  if (!(await stillCurrent(env, message.generation))) return;
  const user = await actor(env, message.userId);
  if (!user) return;
  if (message.kind === "update-page") await enqueueUpdatePage(env, user, message);
  else await processUpdateComic(env, user, message);
}
export async function checkUpdates(
  env: Env,
  user: Identity,
  automatic = false,
) {
  // Same array the update API already returns. The scan itself is queued.
  const previous = storedCandidates(await setting(env, CANDIDATES, "[]"));
  const generation = crypto.randomUUID();
  await setSetting(env, GENERATION, generation);
  await setSetting(env, CANDIDATES, "[]");
  await env.DOWNLOADS.send({
    kind: "update-page",
    cursor: "",
    budget: UPDATE_PAGE_BUDGET,
    generation,
    userId: user.user_id,
    automatic,
  });
  return previous;
}
export async function scheduleUpdates(env: Env) {
  if ((await setting(env, "comic_update_mode", "manual")) !== "auto") return;
  const last = Number(await setting(env, "auto_update_requested_at", "0"));
  if (now() - last < 43200) return;
  const user = await one(
    env,
    "SELECT id FROM users WHERE role='admin' ORDER BY username LIMIT 1",
  );
  if (
    !user ||
    (await one(
      env,
      "SELECT id FROM comic_jobs WHERE kind='update-check' AND state IN ('queued','running') LIMIT 1",
    ))
  )
    return;
  await enqueue(env, user.id, "update-check", { automatic: true });
  await setSetting(env, "auto_update_requested_at", String(now()));
}
