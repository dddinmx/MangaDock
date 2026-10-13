import { HttpError } from "./errors";
import { now, type Env, type QueueBody } from "./index";

export const QUEUE_PAUSED_MESSAGE = "队列免费预算已暂停，等待重置后自动恢复";
export class QueuePausedError extends HttpError {
  constructor(public retryAfter: number) {
    super(429, QUEUE_PAUSED_MESSAGE);
  }
}
type Budget = { day: number; sent: number; pausedUntil: number };
export class QueueBudget {
  constructor(
    private storage: DurableObjectStorage,
    private clock = Date.now,
    private limit = 1000,
    private pauseUntil = 0,
  ) {
    if (!Number.isInteger(limit) || limit < 1 || limit > 1000)
      throw new Error("Queue budget must be 1–1000 messages per UTC day");
  }
  async reserve(count: number) {
    if (!Number.isInteger(count) || count < 1 || count > 100)
      throw new HttpError(400, "Invalid queue batch");
    const seconds = Math.floor(this.clock() / 1000),
      day = Math.floor(seconds / 86400),
      reset = (day + 1) * 86400;
    return this.storage.transaction(async (tx) => {
      const old = await tx.get<Budget>("queue-budget");
      const state: Budget = {
        day,
        sent: old?.day === day ? old.sent : 0,
        pausedUntil: Math.max(old?.pausedUntil || 0, this.pauseUntil),
      };
      if (state.pausedUntil > seconds || state.sent + count > this.limit) {
        // Rejected sends do not repeatedly write storage or call Cloudflare Queues.
        if (state.pausedUntil !== (old?.pausedUntil || 0))
          await tx.put("queue-budget", state);
        return {
          allowed: false,
          retryAfter: Math.max(1, Math.max(reset, state.pausedUntil) - seconds),
        };
      }
      state.sent += count;
      await tx.put("queue-budget", state);
      return { allowed: true, retryAfter: 0 };
    });
  }
}
export function safeQueue(env: Env): Queue<QueueBody> {
  const sendBatch = async (messages: MessageSendRequest<QueueBody>[]) => {
    if (!messages.length) return;
    const response = await env.APPLICATION.get(
      env.APPLICATION.idFromName("queue-budget"),
    ).fetch("https://queue.internal/send", {
      method: "POST",
      body: JSON.stringify(messages),
    });
    await response.body?.cancel();
    if (response.status === 429)
      throw new QueuePausedError(
        Number(response.headers.get("Retry-After")) || 86400,
      );
    if (!response.ok) throw new HttpError(503, "队列暂时不可用，任务已保留");
  };
  return {
    send: async (body: QueueBody, options?: QueueSendOptions) =>
      sendBatch([{ body, ...options }]),
    sendBatch,
    metrics: (...args: Parameters<Queue<QueueBody>["metrics"]>) =>
      env.DOWNLOADS.metrics(...args),
  } as unknown as Queue<QueueBody>;
}
export async function queueSend(
  request: Request,
  env: Env,
  storage: DurableObjectStorage,
  raw: Queue<QueueBody>,
) {
  const messages = await request.json<MessageSendRequest<QueueBody>[]>();
  if (!Array.isArray(messages)) throw new HttpError(400, "Invalid queue batch");
  if (
    messages.some(
      (message) =>
        new TextEncoder().encode(JSON.stringify(message)).byteLength > 16384,
    )
  )
    throw new HttpError(413, "Queue message exceeds the free-budget unit");
  const quota = new QueueBudget(
    storage,
    Date.now,
    Number(env.QUEUE_DAILY_MESSAGE_LIMIT || 1000),
    Number(env.QUEUE_SEND_PAUSED_UNTIL || 0),
  );
  const reservation = await quota.reserve(messages.length);
  if (!reservation.allowed)
    return new Response(QUEUE_PAUSED_MESSAGE, {
      status: 429,
      headers: { "Retry-After": String(reservation.retryAfter) },
    });
  // Count reservations even if the provider fails; ambiguous sends must not bypass the cap.
  await raw.sendBatch(messages);
  return new Response("{}", {
    headers: { "Content-Type": "application/json" },
  });
}
export async function holdComic(env: Env, id: string, error: unknown) {
  if (!(error instanceof QueuePausedError)) return false;
  await env.DB.prepare(
    "UPDATE comic_jobs SET message=?,lease_until=MAX(lease_until,?),updated_at=? WHERE id=? AND state IN ('queued','waiting')",
  )
    .bind(QUEUE_PAUSED_MESSAGE, now() + error.retryAfter, now(), id)
    .run();
  return true;
}
