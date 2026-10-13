import { HttpError, now, type Env, type Identity } from "./index";
import { databasePaused } from "./database-budget";
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
const visibleSql =
  "(?='admin' OR EXISTS(SELECT 1 FROM group_permissions gp WHERE gp.user_id=? AND gp.group_name=c.group_name))";
async function actor(env: Env, id: string): Promise<Identity | null> {
  const row = await one(
    env,
    "SELECT id AS user_id,username,role,can_download,can_view_adult FROM users WHERE id=?",
    id,
  );
  return row ? ({ ...row, csrf: "", token_hash: "" } as Identity) : null;
}
async function finishCheck(env: Env, id: string) {
  await run(
    env,
    "UPDATE update_checks SET state='completed',finished_at=?,updated_at=? WHERE id=? AND state='checking' AND paging_done=1 AND completed+failed=total",
    now(),
    now(),
    id,
  );
}
export async function updateStatus(
  env: Env,
  user: Identity,
  after = "",
  includeCandidates = true,
) {
  const check = await one(
    env,
    "SELECT * FROM update_checks WHERE user_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
    user.user_id,
  );
  const candidates =
    check && includeCandidates
      ? await all(
          env,
          `SELECT i.comic_id,i.result FROM update_check_items i JOIN comics c ON c.id=i.comic_id WHERE i.check_id=? AND i.state IN ('completed','error') AND i.result IS NOT NULL AND i.comic_id>? AND ${visibleSql} ORDER BY i.comic_id LIMIT 201`,
          check.id,
          after.slice(0, 200),
          user.role,
          user.user_id,
        )
      : [];
  return {
    check_id: check?.id || null,
    checking: check?.state === "checking",
    state: check?.state || "idle",
    total: check?.total || 0,
    completed: check?.completed || 0,
    failed: check?.failed || 0,
    finished_at: check?.finished_at || 0,
    last_checked_at: check?.finished_at
      ? new Date(check.finished_at * 1000).toISOString()
      : null,
    next_cursor: candidates.length > 200 ? candidates[199].comic_id : null,
    candidates: candidates.slice(0, 200).map((row) => JSON.parse(row.result)),
  };
}
export async function checkUpdates(
  env: Env,
  user: Identity,
  automatic = false,
) {
  const known = await one(
    env,
    "SELECT id FROM update_checks WHERE user_id=? AND state='checking'",
    user.user_id,
  );
  if (known) return [];
  const id = crypto.randomUUID();
  const inserted = await run(
    env,
    "INSERT INTO update_checks(id,user_id,automatic,created_at,updated_at) VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
    id,
    user.user_id,
    automatic ? 1 : 0,
    now(),
    now(),
  );
  if (inserted.meta.changes)
    await env.DOWNLOADS.send({
      kind: "update-page",
      cursor: "",
      budget: UPDATE_PAGE_BUDGET,
      generation: id,
      userId: user.user_id,
      automatic,
    });
  return [];
}
async function dispatchPage(
  env: Env,
  user: Identity,
  check: Row,
  message: UpdateMessage,
) {
  const cursorText = message.cursor || "";
  if (check.paging_done || check.cursor !== cursorText) return;
  const cursor = cursorText ? decodeLibraryCursor(cursorText) : null;
  if (cursorText && !cursor) throw new HttpError(400, "更新游标无效");
  const budget = Math.min(
    50,
    Math.max(1, Math.trunc(Number(message.budget)) || UPDATE_PAGE_BUDGET),
  );
  // IDs remain stable while automatic downloads modify updated_at.
  const rows = await all(
    env,
    `SELECT c.id,c.updated_at FROM comics c WHERE ${visibleSql} AND c.source_url!='' AND c.id>? ORDER BY c.id LIMIT ?`,
    user.role,
    user.user_id,
    cursor?.id || "",
    budget + 1,
  );
  const page = rows.slice(0, budget);
  if (page.length)
    await env.DB.batch(
      page.map((comic) =>
        env.DB.prepare(
          "INSERT INTO update_check_items(check_id,comic_id,updated_at) VALUES (?,?,?) ON CONFLICT DO NOTHING",
        ).bind(check.id, comic.id, now()),
      ),
    );
  const bodies: UpdateMessage[] = page.map((comic) => ({
    kind: "update-comic",
    comicId: comic.id,
    generation: check.id,
    userId: user.user_id,
    automatic: !!check.automatic,
  }));
  const more = rows.length > budget;
  const next = more ? encodeLibraryCursor(page.at(-1)!) : cursorText;
  if (more)
    bodies.push({
      kind: "update-page",
      cursor: next,
      budget,
      generation: check.id,
      userId: user.user_id,
      automatic: !!check.automatic,
    });
  // Persist the cursor after successful send: a failed send may safely resend
  // deterministic item IDs; finished items cannot be claimed a second time.
  if (bodies.length)
    await env.DOWNLOADS.sendBatch(bodies.map((body) => ({ body })));
  await run(
    env,
    "UPDATE update_checks SET cursor=?,paging_done=?,updated_at=? WHERE id=? AND state='checking' AND cursor=?",
    next,
    more ? 0 : 1,
    now(),
    check.id,
    cursorText,
  );
  await finishCheck(env, check.id);
}
async function dispatchComic(
  env: Env,
  user: Identity,
  check: Row,
  message: UpdateMessage,
) {
  if (!message.comicId) return;
  const token = crypto.randomUUID();
  const claimed = await one(
    env,
    "UPDATE update_check_items SET state='running',claim_token=?,lease_until=?,attempts=attempts+1,updated_at=? WHERE check_id=? AND comic_id=? AND state IN ('queued','running') AND lease_until<? AND attempts<3 RETURNING attempts",
    token,
    now() + 120,
    now(),
    check.id,
    message.comicId,
    now(),
  );
  if (!claimed) return;
  const comic = await one(
    env,
    `SELECT c.* FROM comics c WHERE c.id=? AND ${visibleSql}`,
    message.comicId,
    user.role,
    user.user_id,
  );
  let result: Row | null = null,
    state = "completed";
  try {
    if (comic) {
      const source = await loadComic(comic.source_url, env);
      if (
        !(await one(
          env,
          "SELECT 1 FROM update_check_items i JOIN update_checks c ON c.id=i.check_id WHERE i.check_id=? AND i.comic_id=? AND i.claim_token=? AND i.state='running' AND i.lease_until>? AND c.state='checking'",
          check.id,
          message.comicId,
          token,
          now(),
        ))
      )
        return;
      const local = await all(
        env,
        "SELECT source_id FROM chapters WHERE comic_id=? AND state='ready'",
        comic.id,
      );
      const ready = new Set(local.map((row) => String(row.source_id)));
      const pending = source.chapters.filter((ch) => !ready.has(ch.id));
      if (pending.length)
        result = {
          id: comic.id,
          comic_name: comic.name,
          group: comic.group_name,
          status: "completed",
          pending_chapters: pending.length,
          remote_total_chapters: source.chapters.length,
          local_total_chapters: local.length,
          latest_chapter_title: pending.at(-1)?.title,
        };
      if (pending.length && check.automatic) {
        try {
          await createDownload(env, user, {
            url: comic.source_url,
            group: comic.group_name,
            format: comic.comic_format || 2,
            is_update: true,
          });
        } catch (error) {
          if (databasePaused(error)) throw error;
          if (!(error instanceof HttpError && error.status === 409))
            throw error;
        }
      }
    }
  } catch (error) {
    if (databasePaused(error)) throw error;
    if (
      !(error instanceof HttpError && error.status === 400) &&
      claimed.attempts < 3
    ) {
      await run(
        env,
        "UPDATE update_check_items SET state='queued',lease_until=0,claim_token=NULL,updated_at=? WHERE check_id=? AND comic_id=? AND claim_token=?",
        now(),
        check.id,
        message.comicId,
        token,
      );
      throw error;
    }
    state = "error";
    if (comic)
      result = {
        id: comic.id,
        comic_name: comic.name,
        group: comic.group_name,
        status: "error",
        error_message: "来源暂时不可用",
      };
  }
  await run(
    env,
    "UPDATE update_check_items SET state=?,result=?,claim_token=NULL,lease_until=0,updated_at=? WHERE check_id=? AND comic_id=? AND claim_token=? AND EXISTS(SELECT 1 FROM update_checks WHERE id=? AND state='checking')",
    state,
    result ? JSON.stringify(result) : null,
    now(),
    check.id,
    message.comicId,
    token,
    check.id,
  );
  await finishCheck(env, check.id);
}
export async function dispatchUpdate(env: Env, message: UpdateMessage) {
  if (
    !message.generation ||
    !message.userId ||
    !["update-page", "update-comic"].includes(message.kind || "")
  )
    return;
  const check = await one(
    env,
    "SELECT * FROM update_checks WHERE id=? AND user_id=? AND state='checking'",
    message.generation,
    message.userId,
  );
  if (!check) return;
  const user = await actor(env, message.userId);
  if (!user || (user.role !== "admin" && !user.can_download)) {
    await run(
      env,
      "UPDATE update_checks SET state='error',finished_at=?,updated_at=? WHERE id=?",
      now(),
      now(),
      check.id,
    );
    return;
  }
  if (message.kind === "update-page")
    await dispatchPage(env, user, check, message);
  else await dispatchComic(env, user, check, message);
}
export async function recoverUpdates(env: Env) {
  // Completion can be lost after the final item commits (e.g. a budget pause).
  await run(
    env,
    "UPDATE update_checks SET state='completed',finished_at=?,updated_at=? WHERE id IN (SELECT id FROM update_checks WHERE state='checking' AND paging_done=1 AND completed+failed=total LIMIT 20)",
    now(),
    now(),
  );
  // Bounded retention: these are transient scan results, not library/progress data.
  await run(
    env,
    "DELETE FROM update_checks WHERE id IN (SELECT id FROM update_checks WHERE state!='checking' AND finished_at<? LIMIT 20)",
    now() - 604800,
  );
  const expired = await all(
    env,
    "UPDATE update_check_items SET state='error',claim_token=NULL,lease_until=0,updated_at=? WHERE state IN ('queued','running') AND attempts>=3 AND lease_until<? RETURNING check_id",
    now(),
    now(),
  );
  for (const id of new Set(expired.map((row) => row.check_id)))
    await finishCheck(env, id);
  await run(
    env,
    "UPDATE update_checks SET state='error',finished_at=?,updated_at=? WHERE state='checking' AND created_at<?",
    now(),
    now(),
    now() - 86400,
  );
  const pages = await all(
    env,
    "SELECT id,user_id,cursor FROM update_checks WHERE state='checking' AND paging_done=0 AND updated_at<? LIMIT 10",
    now() - 900,
  );
  const items = await all(
    env,
    "SELECT i.check_id,i.comic_id,c.user_id FROM update_check_items i JOIN update_checks c ON c.id=i.check_id WHERE i.state IN ('queued','running') AND i.lease_until<? AND i.updated_at<? AND i.attempts<3 AND c.state='checking' LIMIT 20",
    now(),
    now() - 900,
  );
  const bodies: UpdateMessage[] = [
    ...pages.map((row) => ({
      kind: "update-page",
      generation: row.id,
      userId: row.user_id,
      cursor: row.cursor,
      budget: UPDATE_PAGE_BUDGET,
    })),
    ...items.map((row) => ({
      kind: "update-comic",
      generation: row.check_id,
      comicId: row.comic_id,
      userId: row.user_id,
    })),
  ];
  if (bodies.length)
    await env.DOWNLOADS.sendBatch(bodies.map((body) => ({ body })));
  if (pages.length)
    await env.DB.batch(
      pages.map((row) =>
        env.DB.prepare(
          "UPDATE update_checks SET updated_at=? WHERE id=? AND state='checking'",
        ).bind(now(), row.id),
      ),
    );
  if (items.length)
    await env.DB.batch(
      items.map((row) =>
        env.DB.prepare(
          "UPDATE update_check_items SET updated_at=? WHERE check_id=? AND comic_id=? AND state IN ('queued','running')",
        ).bind(now(), row.check_id, row.comic_id),
      ),
    );
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
