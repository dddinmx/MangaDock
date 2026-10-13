import { HttpError, now, type Env } from "./index";
import { one, type Row } from "./library";
// Attach the execution fence to mutations themselves, not a separate check that
// can race with cancellation or lease reclaim on another Durable Object.
export function fencedEnv(
  env: Env,
  table: "comic_jobs" | "compute_jobs",
  job: Row,
): Env {
  const args = [
    job.id,
    job.claim_token,
    table === "comic_jobs" ? job.id : JSON.parse(job.payload).parent_job || "",
    table === "comic_jobs" ? job.id : JSON.parse(job.payload).parent_job || "",
  ];
  return {
    ...env,
    DB: {
      batch: env.DB.batch.bind(env.DB),
      prepare(sql: string) {
        const update =
          /^UPDATE\s+(comic_jobs|compute_jobs|chapters|comics|books)\b/i.test(
            sql,
          );
        const insert =
          /^INSERT\s+INTO\s+(comic_jobs|compute_jobs|chapters|comics)\b/i.test(
            sql,
          );
        if (!update && !insert) return env.DB.prepare(sql);
        const stmt = (...values: unknown[]) => {
          const failure =
            /^UPDATE\s+(comic_jobs|compute_jobs)\s+SET\s+state='error'/i.test(
              sql,
            ) ||
            (/^UPDATE\s+comic_jobs\s+SET\s+state=\?/i.test(sql) &&
              values[0] === "error");
          const states = failure
            ? "execution.state!='cancelled'"
            : "execution.state NOT IN ('cancelled','error')";
          const guard = `EXISTS(SELECT 1 FROM ${table} execution WHERE execution.id=? AND execution.claim_token=? AND ${states}${table === "compute_jobs" ? " AND (execution.state='completed' OR execution.lease_until>unixepoch())" : ""}) AND NOT EXISTS(SELECT 1 FROM comic_jobs parent WHERE parent.id IN (${table === "comic_jobs" ? "SELECT parent_id FROM comic_jobs WHERE id=? UNION SELECT p.parent_id FROM comic_jobs j JOIN comic_jobs p ON p.id=j.parent_id WHERE j.id=?" : "SELECT id FROM comic_jobs WHERE id=? UNION SELECT parent_id FROM comic_jobs WHERE id=?"}) AND parent.state IN (${failure ? "'cancelled'" : "'cancelled','error'"}))`;
          const targetFence = /^UPDATE\s+comic_jobs\b/i.test(sql)
            ? failure
              ? " AND state NOT IN ('cancelled','completed')"
              : " AND state!='cancelled'"
            : "";
          let guarded: string;
          if (update) {
            const where = sql.search(/\sWHERE\s/i),
              returning = sql.search(/\sRETURNING\s/i);
            if (where < 0) throw new Error("Fenced mutation requires WHERE");
            const end = returning < 0 ? sql.length : returning;
            guarded =
              sql.slice(0, where) +
              " WHERE (" +
              sql.slice(where, end).replace(/^\sWHERE\s/i, "") +
              ") AND " +
              guard +
              targetFence +
              sql.slice(end);
          } else {
            guarded = sql.replace(
              /VALUES\s*\(([^()]*)\)/i,
              (_all, columns) => "SELECT " + columns + " WHERE " + guard,
            );
            if (guarded === sql)
              throw new Error("Unsupported unfenced insertion");
          }
          return env.DB.prepare(guarded).bind(...values, ...args);
        };
        return {
          bind: stmt,
          first: () => stmt().first(),
          all: () => stmt().all(),
          run: () => stmt().run(),
        };
      },
    },
  };
}
export async function assertComicActive(env: Env, job: Row) {
  if (
    !(await one(
      env,
      "SELECT 1 FROM comic_jobs j WHERE j.id=? AND j.claim_token=? AND j.state NOT IN ('cancelled','error') AND NOT EXISTS(SELECT 1 FROM comic_jobs p WHERE p.id IN (j.parent_id,(SELECT parent_id FROM comic_jobs WHERE id=j.parent_id)) AND p.state IN ('cancelled','error'))",
      job.id,
      job.claim_token,
    ))
  )
    throw new HttpError(409, "任务已停止或执行已过期");
}
export async function assertComputeLease(
  env: Env,
  id: string,
  token: string | null,
) {
  const job = token
    ? await one(
        env,
        "SELECT * FROM compute_jobs WHERE id=? AND claim_token=? AND state='running' AND lease_until>?",
        id,
        token,
        now(),
      )
    : null;
  if (!job) throw new HttpError(409, "计算执行已取消或租约已过期");
  const data = JSON.parse(job.payload);
  if (data.parent_job) {
    const parent = await one(
      env,
      "SELECT p.state,r.state AS root_state FROM comic_jobs p LEFT JOIN comic_jobs r ON r.id=p.parent_id WHERE p.id=?",
      data.parent_job,
    );
    if (
      !parent ||
      ["cancelled", "error", "completed"].includes(parent.state) ||
      ["cancelled", "error"].includes(parent.root_state)
    )
      throw new HttpError(409, "下载任务已停止");
  }
  return job;
}
export async function cancelComputeForTask(env: Env, id: string) {
  await env.DB.prepare(
    "UPDATE compute_jobs SET state='cancelled',claim_token=NULL,lease_until=0 WHERE state IN ('queued','running') AND (json_extract(payload,'$.parent_job') IN (SELECT id FROM comic_jobs WHERE id=? OR parent_id=? OR parent_id IN (SELECT id FROM comic_jobs WHERE parent_id=?)) OR json_extract(payload,'$.comic_id')=(SELECT comic_id FROM comic_jobs WHERE id=?) OR (json_extract(payload,'$.media_id')=(SELECT comic_id FROM comic_jobs WHERE id=?) AND json_extract(payload,'$.kind')='comic'))",
  )
    .bind(id, id, id, id, id)
    .run();
}
