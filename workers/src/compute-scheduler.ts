import { HttpError, json, now, type Env } from "./index";
// Wakes are DO-local: idle GitHub timer probes never access the metered DB.
export async function notifyCompute(env: Env, at = now()) {
  const response = await env.APPLICATION.get(
    env.APPLICATION.idFromName("scheduler"),
  ).fetch("https://scheduler.internal/wake", {
    method: "POST",
    body: JSON.stringify({ at }),
  });
  if (!response.ok) throw new Error("Cannot wake cloud compute scheduler");
}
type Wake = {
  sequence: number;
  pending: boolean;
  day: string;
  runs: number;
  next: number;
  /** Sequence held by a probe. The daily run is counted only after dispatch succeeds. */
  leasedSequence?: number;
};
export async function schedulerRequest(
  request: Request,
  env: Env,
  storage: DurableObjectStorage,
): Promise<Response | null> {
  const url = new URL(request.url),
    internal = url.hostname === "scheduler.internal";
  if (!internal && !url.pathname.startsWith("/api/compute-ready")) return null;
  if (!internal) {
    const expected = env.COMPUTE_WAKE_TOKEN,
      supplied =
        request.headers.get("Authorization")?.replace(/^Bearer /, "") || "";
    if (!expected) throw new HttpError(503, "自动云转换凭据尚未配置");
    let diff = expected.length ^ supplied.length;
    for (let i = 0; i < expected.length; i++)
      diff |= expected.charCodeAt(i) ^ (supplied.charCodeAt(i) || 0);
    if (diff) throw new HttpError(401, "认证失败");
  }
  if (
    request.method === "POST" &&
    (internal || url.pathname === "/api/compute-ready/wake")
  ) {
    const data = await request.json<{ at?: number }>(),
      at = Number(data.at || now());
    if (!Number.isFinite(at) || at > now() + 172800)
      throw new HttpError(400, "唤醒时间无效");
    if (at > now()) {
      const alarm = await storage.getAlarm();
      if (!alarm || alarm > at * 1000) await storage.setAlarm(at * 1000);
    } else
      await storage.transaction(async (tx) => {
        const state = (await tx.get<Wake>("compute-wake")) || {
          sequence: 0,
          pending: false,
          day: "",
          runs: 0,
          next: 0,
        };
        state.sequence++;
        state.pending = true;
        await tx.put("compute-wake", state);
      });
    return json({ ok: true });
  }
  if (request.method === "GET" && url.pathname === "/api/compute-ready") {
    // Only authenticated probes may recover an R2 deadline when SQLite alarm
    // writes are unavailable. Authentication above must precede storage I/O.
    const due = await storage.getAlarm();
    if (due !== null && due <= Date.now()) {
      await storage.deleteAlarm();
      await notifyCompute(env);
    }
    const data = await storage.transaction(async (tx) => {
      const state = await tx.get<Wake>("compute-wake");
      if (!state?.pending) return { pending: false };
      const day = new Date().toISOString().slice(0, 10);
      let dirty = false;
      if (state.day !== day) {
        state.day = day;
        state.runs = 0;
        dirty = true;
      }
      // `next` only stops a second probe. An abandoned hold expires with it
      // and does not spend one of the 24 daily runs.
      if (state.leasedSequence && state.next <= now()) {
        state.leasedSequence = 0;
        dirty = true;
      }
      if (state.runs >= 24 || state.next > now()) {
        if (dirty) await tx.put("compute-wake", state);
        return { pending: false };
      }
      state.leasedSequence = state.sequence;
      state.next = now() + 900;
      await tx.put("compute-wake", state);
      return {
        pending: true,
        sequence: state.sequence,
        ref: env.COMPUTE_REF,
        limit: 5,
      };
    });
    return json({
      ok: true,
      data: {
        ref: env.COMPUTE_REF,
        ...data,
        queue_resume_after: Number(env.QUEUE_SEND_PAUSED_UNTIL || 0),
      },
    });
  }
  if (
    request.method === "POST" &&
    url.pathname === "/api/compute-ready/finished"
  ) {
    const data = await request.json<{
      sequence: number;
      pending: boolean;
      ok?: boolean;
    }>();
    await storage.transaction(async (tx) => {
      const state = await tx.get<Wake>("compute-wake");
      if (!state) return;
      const failed = data.ok === false;
      if (state.leasedSequence && state.leasedSequence === data.sequence) {
        if (failed) state.next = 0;
        else {
          const day = new Date().toISOString().slice(0, 10);
          if (state.day !== day) {
            state.day = day;
            state.runs = 0;
          }
          state.runs++;
        }
        state.leasedSequence = 0;
      }
      // A failed dispatch keeps the wake so a later probe can retry.
      if (!failed && state.sequence === data.sequence && !data.pending)
        state.pending = false;
      await tx.put("compute-wake", state);
    });
    return json({ ok: true });
  }
  throw new HttpError(405, "自动计算接口操作无效");
}
