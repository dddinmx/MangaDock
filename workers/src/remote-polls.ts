import { HttpError, json, now, type Env } from "./index";
import { runJob } from "./downloads";
import { databasePaused, databaseRetryAfter } from "./database-budget";

// One existing SQLite DO owns these alarms. No Queue message is needed to wait
// for an upstream comic archive. Idle alarms stop instead of polling the DB.
export const REMOTE_POLL_BATCH = 5;
export const REMOTE_POLL_INTERVAL = 60;
const KEY = "remote-poll-failures";
const GENERATION = "remote-poll-generation";
const NOT_BEFORE = "remote-poll-not-before";

export async function wakeRemotePolls(env: Env, at: number) {
  const response = await env.APPLICATION.get(
    env.APPLICATION.idFromName("remote-polls"),
  ).fetch("https://remote-polls.internal/wake", {
    method: "POST",
    body: JSON.stringify({ at }),
  });
  if (!response.ok) throw new Error("Cannot wake remote comic polling");
  await response.body?.cancel();
}

export async function remotePollRequest(
  request: Request,
  storage: DurableObjectStorage,
): Promise<Response | null> {
  const url = new URL(request.url);
  if (url.hostname !== "remote-polls.internal") return null;
  if (url.pathname !== "/wake" || request.method !== "POST")
    throw new HttpError(405, "Invalid remote poll wake");
  const { at } = await request.json<{ at: number }>();
  if (!Number.isSafeInteger(at) || at < 0 || at > now() + 172800)
    throw new HttpError(400, "Invalid remote poll time");
  // Serialize competing wakes: a later request must not overwrite an earlier one.
  await storage.transaction(async (tx) => {
    const alarm = await tx.getAlarm(),
      blocked = (await tx.get<number>(NOT_BEFORE)) || 0,
      requested = Math.max(at, blocked, now() + REMOTE_POLL_INTERVAL) * 1000;
    await tx.put(GENERATION, ((await tx.get<number>(GENERATION)) || 0) + 1);
    if (!alarm || alarm > requested) await tx.setAlarm(requested);
  });
  return json({ ok: true });
}

export async function recoverRemotePolls(env: Env) {
  const next = await env.DB.prepare(
    "SELECT remote_poll_at FROM comic_jobs WHERE remote_poll_at IS NOT NULL ORDER BY remote_poll_at,id LIMIT 1",
  ).first<{ remote_poll_at: number }>();
  if (next) await wakeRemotePolls(env, next.remote_poll_at);
}

export async function remotePollAlarm(env: Env, storage: DurableObjectStorage) {
  // Leave a watchdog before doing network work. A terminated alarm can recover
  // its claimed rows after the five-minute execution lease expires.
  const watchdog = (now() + 300) * 1000;
  const generation = await storage.transaction(async (tx) => {
    const current = (await tx.get<number>(GENERATION)) || 0;
    await tx.setAlarm(watchdog);
    return current;
  });
  try {
    const due = await env.DB.prepare(
      "SELECT id FROM comic_jobs WHERE remote_poll_at IS NOT NULL AND remote_poll_at<=? ORDER BY remote_poll_at,id LIMIT ?",
    )
      .bind(now(), REMOTE_POLL_BATCH)
      .all<{ id: string }>();
    const results = await Promise.allSettled(
      due.results.map(({ id }) => runJob(env, id)),
    );
    const failed = results.find((result) => result.status === "rejected");
    if (failed?.status === "rejected") throw failed.reason;
    const next = await env.DB.prepare(
      "SELECT remote_poll_at FROM comic_jobs WHERE remote_poll_at IS NOT NULL ORDER BY remote_poll_at,id LIMIT 1",
    ).first<{ remote_poll_at: number }>();
    // Wakes may arrive while the sweep waits for upstream HTTP. Preserve an
    // earlier alarm; only delete our own watchdog when the indexed outbox is empty.
    await storage.transaction(async (tx) => {
      const alarm = await tx.getAlarm();
      const changed = ((await tx.get<number>(GENERATION)) || 0) !== generation;
      if (next) {
        const requested =
          Math.max(next.remote_poll_at, now() + REMOTE_POLL_INTERVAL) * 1000;
        if (!changed || !alarm || alarm > requested)
          await tx.setAlarm(requested);
      } else if (!changed && alarm === watchdog) {
        await tx.deleteAlarm();
      }
      await tx.delete(KEY);
      await tx.delete(NOT_BEFORE);
    });
  } catch (error) {
    const failures = Math.min(8, ((await storage.get<number>(KEY)) || 0) + 1);
    const delay = databasePaused(error)
      ? Math.max(60, databaseRetryAfter(error))
      : Math.min(3600, 60 * 2 ** (failures - 1));
    await storage.transaction(async (tx) => {
      await tx.put(KEY, failures);
      await tx.put(NOT_BEFORE, now() + delay);
      await tx.setAlarm((now() + delay) * 1000);
    });
    // Avoid Cloudflare's automatic alarm retries amplifying an outage. The
    // persisted database circuit and bounded backoff own recovery instead.
    console.warn(
      JSON.stringify({ component: "remote-polls", retry_after: delay }),
    );
  }
}
