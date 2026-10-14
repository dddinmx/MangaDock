import { HttpError, now, type Env, type QueueBody } from "./index";
import { PAGE_MESSAGE_SIZE } from "./downloads";
import { QueuePausedError } from "./queue-safety";

const PREFIX = "download-hold/",
  COUNT = "download-hold-count",
  MAXIMUM = 1000;
type Held = { id: string; due: number; attempts: number; version: string };
const jobs = (env: Env) =>
  env.APPLICATION.get(env.APPLICATION.idFromName("jobs"));
const validId = (id: unknown): id is string =>
  typeof id === "string" && /^[a-zA-Z0-9:_-]{1,160}$/.test(id);

export async function deferDownloads(
  env: Env,
  ids: string[],
  retryAfter: number,
) {
  const response = await env.APPLICATION.get(
    env.APPLICATION.idFromName("download-recovery"),
  ).fetch("https://recovery.internal/defer", {
    method: "POST",
    body: JSON.stringify({ ids, retry_after: retryAfter }),
  });
  await response.body?.cancel();
  if (!response.ok)
    throw new HttpError(503, "下载恢复记录暂时不可用，等待重试");
}

export async function recoveryRequest(
  request: Request,
  storage: DurableObjectStorage,
) {
  const url = new URL(request.url);
  if (
    url.hostname !== "recovery.internal" ||
    url.pathname !== "/defer" ||
    request.method !== "POST"
  )
    return null;
  const body = await request.json<{ ids: string[]; retry_after: number }>();
  if (
    !Array.isArray(body.ids) ||
    !body.ids.length ||
    body.ids.length > 100 ||
    body.ids.some((id) => !validId(id)) ||
    !Number.isFinite(body.retry_after)
  )
    throw new HttpError(400, "Invalid download recovery batch");
  const ids = [...new Set(body.ids)],
    due = Date.now() + Math.max(1, Math.min(2678400, body.retry_after)) * 1000;
  const earliest = await storage.transaction(async (tx) => {
    const current = await tx.get<Held>(ids.map((id) => PREFIX + id));
    const count = (await tx.get<number>(COUNT)) || 0;
    const added = ids.filter((id) => !current.has(PREFIX + id)).length;
    if (count + added > MAXIMUM)
      throw new HttpError(503, "Download recovery capacity reached");
    const records = Object.fromEntries(
      ids.map((id) => [
        PREFIX + id,
        {
          id,
          due: Math.max(current.get(PREFIX + id)?.due || 0, due),
          attempts: current.get(PREFIX + id)?.attempts || 0,
          version: crypto.randomUUID(),
        },
      ]),
    );
    await tx.put({ ...records, [COUNT]: count + added });
    return Math.min(...Object.values(records).map((record) => record.due));
  });
  const alarm = await storage.getAlarm();
  if (alarm === null || earliest < alarm) await storage.setAlarm(earliest);
  return Response.json({ ok: true });
}

// Runs in the existing jobs object, sharing its metered database and limits.
export async function reclaimDeferred(env: Env, ids: string[]) {
  if (
    !Array.isArray(ids) ||
    !ids.length ||
    ids.length > 100 ||
    ids.some((id) => !validId(id))
  )
    throw new HttpError(400, "Invalid deferred IDs");
  const recovered: { id: string; kind: string }[] = [],
    pending: { id: string; due: number }[] = [];
  for (let offset = 0; offset < ids.length; offset += 90) {
    const part = ids.slice(offset, offset + 90),
      parameters = part.map(() => "?").join(",");
    const ancestry =
      "AND NOT EXISTS(SELECT 1 FROM comic_jobs p WHERE p.id=comic_jobs.parent_id AND p.state IN ('cancelled','error','completed')) AND NOT EXISTS(SELECT 1 FROM comic_jobs p JOIN comic_jobs r ON r.id=p.parent_id WHERE p.id=comic_jobs.parent_id AND r.state IN ('cancelled','error','completed'))";
    const result = await env.DB.prepare(
      `UPDATE comic_jobs SET state='queued',lease_until=0,claim_token=NULL,updated_at=? WHERE id IN (${parameters}) AND state IN ('queued','running') AND attempts<5 AND lease_until<? ${ancestry} RETURNING id,kind`,
    )
      .bind(now(), ...part, now())
      .all<{ id: string; kind: string }>();
    recovered.push(...result.results);
    const held = await env.DB.prepare(
      `SELECT id,lease_until FROM comic_jobs WHERE id IN (${parameters}) AND state IN ('queued','running') AND attempts<5 AND lease_until>=? ${ancestry}`,
    )
      .bind(...part, now())
      .all<{ id: string; lease_until: number }>();
    pending.push(
      ...held.results.map((row) => ({
        id: row.id,
        due: (row.lease_until + 1) * 1000,
      })),
    );
  }
  return { recovered, pending };
}

export async function recoverExpiredNativePages(env: Env) {
  return env.DB.prepare(
    "UPDATE comic_jobs SET state='queued',lease_until=0,claim_token=NULL,updated_at=? WHERE id IN (SELECT j.id FROM comic_jobs j INDEXED BY comic_page_recovery JOIN comic_jobs p ON p.id=j.parent_id JOIN comic_jobs r ON r.id=p.parent_id WHERE j.kind='page' AND j.remote_poll_at IS NULL AND j.state IN ('queued','running') AND j.lease_until<? AND j.attempts<5 AND (j.state='running' OR j.updated_at<?) AND p.kind='chapter' AND p.state IN ('queued','running','waiting') AND r.kind='source' AND r.state IN ('queued','running','waiting') ORDER BY j.state,j.lease_until,j.updated_at,j.id LIMIT 100) RETURNING id,kind",
  )
    .bind(now(), now(), now() - 300)
    .all<{ id: string; kind: string }>();
}

async function change(
  storage: DurableObjectStorage,
  selected: Held[],
  pending: Map<string, number>,
  failed = false,
) {
  await storage.transaction(async (tx) => {
    let count = (await tx.get<number>(COUNT)) || 0;
    for (const record of selected) {
      const key = PREFIX + record.id,
        current = await tx.get<Held>(key);
      if (current?.version !== record.version) continue; // Do not erase a new pause racing this send.
      const attempts = record.attempts + (failed ? 1 : 0);
      const due = pending.get(record.id);
      if ((failed && attempts < 5) || due !== undefined)
        await tx.put(key, {
          ...record,
          attempts,
          due: due ?? Date.now() + Math.min(1800, 60 * 2 ** attempts) * 1000,
        });
      else {
        await tx.delete(key);
        count--;
      }
    }
    await tx.put(COUNT, Math.max(0, count));
  });
}

export async function recoveryAlarm(env: Env, storage: DurableObjectStorage) {
  const records = [
    ...(await storage.list<Held>({ prefix: PREFIX, limit: MAXIMUM })).values(),
  ];
  if (!records.length) {
    await storage.deleteAlarm();
    return;
  }
  const selected = records
    .filter((row) => row.due <= Date.now())
    .sort((a, b) => a.due - b.due || a.id.localeCompare(b.id))
    .slice(0, 100);
  if (!selected.length) {
    await storage.setAlarm(Math.min(...records.map((row) => row.due)));
    return;
  }
  try {
    const snapshot = await jobs(env).fetch(
      "https://jobs.internal/resource-status",
    );
    if (!snapshot.ok) throw new Error("Recovery status unavailable");
    const status = await snapshot.json<{
      database: { paused: boolean; retry_after: number };
    }>();
    if (status.database.paused) {
      await storage.setAlarm(
        Date.now() + Math.max(1, status.database.retry_after) * 1000,
      );
      return;
    }
    const response = await jobs(env).fetch(
      "https://jobs.internal/recover-deferred",
      {
        method: "POST",
        body: JSON.stringify({ ids: selected.map((row) => row.id) }),
      },
    );
    if (response.status === 503 && response.headers.has("Retry-After")) {
      await storage.setAlarm(
        Date.now() +
          Math.max(1, Number(response.headers.get("Retry-After"))) * 1000,
      );
      await response.body?.cancel();
      return;
    }
    if (!response.ok) throw new Error("Recovery claim failed");
    const { recovered, pending } =
      await response.json<Awaited<ReturnType<typeof reclaimDeferred>>>();
    const pages = recovered
      .filter((row) => row.kind === "page")
      .map((row) => row.id);
    const messages: MessageSendRequest<QueueBody>[] = recovered
      .filter((row) => row.kind !== "page")
      .map((row) => ({ body: { kind: "comic", id: row.id } }));
    for (let offset = 0; offset < pages.length; offset += PAGE_MESSAGE_SIZE)
      messages.push({
        body: {
          kind: "comic-pages",
          ids: pages.slice(offset, offset + PAGE_MESSAGE_SIZE),
        },
      });
    if (messages.length) await env.DOWNLOADS.sendBatch(messages);
    await change(
      storage,
      selected,
      new Map(pending.map((row) => [row.id, row.due])),
    );
  } catch (error) {
    if (error instanceof QueuePausedError) {
      await storage.setAlarm(Date.now() + error.retryAfter * 1000);
      return;
    }
    await change(storage, selected, new Map(), true);
  }
  const remaining = [
    ...(await storage.list<Held>({ prefix: PREFIX, limit: MAXIMUM })).values(),
  ];
  if (remaining.length)
    await storage.setAlarm(
      Math.max(
        Date.now() + 30000,
        Math.min(...remaining.map((row) => row.due)),
      ),
    );
  else await storage.deleteAlarm();
}
