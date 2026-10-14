import { applyD1Migrations, env } from "cloudflare:test";
import { beforeAll, beforeEach, expect, it } from "vitest";
import { tasks, taskAction } from "../src/downloads";
import { queueCompute } from "../src/compute";
import { now, type Env, type Identity } from "../src/index";

const bindings = env as unknown as Env & {
  TEST_MIGRATIONS: Parameters<typeof applyD1Migrations>[1];
};
const user = { user_id: "timing-user", role: "user" } as Identity;
async function sql(query: string, ...args: unknown[]) {
  return bindings.DB.prepare(query)
    .bind(...args)
    .run();
}
async function detail() {
  return (await tasks(bindings, user, "timing-root"))[0];
}
beforeAll(async () => applyD1Migrations(bindings.DB, bindings.TEST_MIGRATIONS));
beforeEach(async () => {
  await sql("DELETE FROM compute_jobs WHERE id LIKE 'timing-%'");
  await sql("DELETE FROM comic_jobs WHERE user_id='timing-user'");
  await sql("DELETE FROM jobs WHERE user_id='timing-user'");
  await sql(
    "INSERT OR IGNORE INTO users(id,username,password_hash) VALUES ('timing-user','timing-user','x')",
  );
  await sql(
    "INSERT INTO comic_jobs(id,user_id,kind,payload,created_at,updated_at) VALUES ('timing-root','timing-user','source',?,100,100)",
    JSON.stringify({ source_planned: true, source_total: 125 }),
  );
});
it("shows queued tasks with a usable log and no invented execution times", async () => {
  expect(await detail()).toMatchObject({
    status: "pending",
    status_label: "等待启动",
    start_time: null,
    end_time: null,
    log: ["等待启动"],
    total_chapters: 125,
    completed_chapters: 0,
  });
});
it("distinguishes queued cloud work, a live lease, and an expired lease", async () => {
  await sql(
    "UPDATE comic_jobs SET state='waiting',message='正在由云端兼容作业解析来源' WHERE id='timing-root'",
  );
  await sql(
    "INSERT INTO compute_jobs(id,kind,input_key,payload,created_at,updated_at) VALUES ('timing-compute','legacy-source','timing-input','{\"parent_job\":\"timing-root\"}',100,100)",
  );
  expect(await detail()).toMatchObject({
    status: "waiting",
    status_label: "等待云执行器",
    start_time: null,
  });
  expect((await detail()).log).toContain(
    "任务已排队，云执行器尚未开始；定时调度可能延迟，请勿重复提交。",
  );
  await sql(
    "UPDATE compute_jobs SET state='running',lease_until=? WHERE id='timing-compute'",
    now() + 120,
  );
  expect(await detail()).toMatchObject({
    status: "running",
    status_label: "云端处理中",
  });
  await sql("UPDATE compute_jobs SET lease_until=1 WHERE id='timing-compute'");
  expect(await detail()).toMatchObject({
    status: "waiting",
    status_label: "等待云执行器",
  });
});
it("recognizes child cloud jobs and native downloads without recounting chapters", async () => {
  await sql("UPDATE comic_jobs SET state='waiting' WHERE id='timing-root'");
  await sql(
    "INSERT INTO comic_jobs(id,user_id,parent_id,kind,payload,state,created_at,updated_at) VALUES ('timing-child','timing-user','timing-root','chapter','{}','waiting',100,100)",
  );
  await sql(
    "INSERT INTO compute_jobs(id,kind,input_key,payload,created_at,updated_at) VALUES ('timing-compute','legacy-chapter','timing-input','{\"parent_job\":\"timing-child\"}',100,100)",
  );
  expect(await detail()).toMatchObject({
    status: "waiting",
    status_label: "等待云执行器",
  });
  await sql(
    "UPDATE comic_jobs SET state='running',lease_until=? WHERE id='timing-child'",
    now() + 120,
  );
  expect(await detail()).toMatchObject({
    status: "running",
    status_label: "下载中",
  });
});
it("retains the first actual start, freezes terminal time and clears both on retry", async () => {
  await sql("UPDATE comic_jobs SET state='running' WHERE id='timing-root'");
  const start = (await detail()).start_time;
  expect(Date.parse(start)).toBeGreaterThanOrEqual((now() - 2) * 1000);
  await sql("UPDATE comic_jobs SET state='waiting' WHERE id='timing-root'");
  expect((await detail()).start_time).toBe(start);
  await sql("UPDATE comic_jobs SET state='error' WHERE id='timing-root'");
  const finish = (await detail()).end_time;
  expect(finish).toBeTruthy();
  await sql(
    "UPDATE comic_jobs SET state='error',updated_at=999999 WHERE id='timing-root'",
  );
  expect((await detail()).end_time).toBe(finish);
  await sql("UPDATE comic_jobs SET state='queued' WHERE id='timing-root'");
  expect(await detail()).toMatchObject({ start_time: null, end_time: null });
});
it("records novel execution times with the same task API contract", async () => {
  await sql(
    "INSERT INTO jobs(id,user_id,source_id,created_at,updated_at) VALUES ('timing-novel','timing-user','123456789',100,100)",
  );
  await sql("UPDATE jobs SET state='submitting' WHERE id='timing-novel'");
  const task = (await tasks(bindings, user, "timing-novel"))[0];
  expect(task.status).toBe("running");
  expect(task.start_time).toBeTruthy();
  expect(task.end_time).toBeNull();
  await sql("UPDATE jobs SET state='completed' WHERE id='timing-novel'");
  expect(
    (await tasks(bindings, user, "timing-novel"))[0].end_time,
  ).toBeTruthy();
});
it("repairing a deduplicated compute job also wakes the scheduler without creating a duplicate", async () => {
  await sql(
    "INSERT INTO compute_jobs(id,kind,input_key,payload,created_at,updated_at) VALUES ('timing-compute','legacy-source','timing-input','{}',100,100)",
  );
  let wakes = 0;
  const local = {
    ...bindings,
    APPLICATION: {
      idFromName: () => "scheduler",
      get: () => ({
        fetch: async () => {
          wakes++;
          return new Response("{}");
        },
      }),
    },
  } as unknown as Env;
  expect(await queueCompute(local, "legacy-source", "timing-input", {})).toBe(
    "timing-compute",
  );
  expect(wakes).toBe(1);
});

it("expired stored running states wait for recovery or queue reset instead of claiming an active download", async () => {
  await sql(
    "UPDATE comic_jobs SET state='running',lease_until=1 WHERE id='timing-root'",
  );
  expect(await detail()).toMatchObject({
    state: "running",
    status: "pending",
    status_label: "等待任务恢复",
  });
  expect(
    (
      await tasks(
        { ...bindings, QUEUE_SEND_PAUSED_UNTIL: String(now() + 60) },
        user,
        "timing-root",
      )
    )[0],
  ).toMatchObject({ status: "pending", status_label: "等待队列额度重置" });
  await sql(
    "UPDATE comic_jobs SET lease_until=? WHERE id='timing-root'",
    now() + 120,
  );
  expect(await detail()).toMatchObject({
    status: "running",
    status_label: "下载中",
  });
});

it("live page grandchildren report an active download until their lease expires", async () => {
  await sql("UPDATE comic_jobs SET state='waiting' WHERE id='timing-root'");
  await sql(
    "INSERT INTO comic_jobs(id,user_id,parent_id,kind,payload,state,created_at,updated_at) VALUES ('timing-chapter','timing-user','timing-root','chapter','{}','waiting',100,100)",
  );
  await sql(
    "INSERT INTO comic_jobs(id,user_id,parent_id,kind,payload,state,lease_until,created_at,updated_at) VALUES ('timing-page','timing-user','timing-chapter','page','{}','running',?,100,100)",
    now() + 120,
  );
  expect(await detail()).toMatchObject({
    status: "running",
    status_label: "下载中",
  });
  await sql("UPDATE comic_jobs SET lease_until=1 WHERE id='timing-page'");
  expect(await detail()).toMatchObject({
    status: "waiting",
    status_label: "等待章节执行",
  });
  await sql(
    "UPDATE comic_jobs SET state='queued',message='队列免费预算已暂停，等待重置后自动恢复' WHERE id='timing-page'",
  );
  expect(await detail()).toMatchObject({
    status: "waiting",
    status_label: "等待队列额度重置",
  });
  await sql(
    "UPDATE comic_jobs SET state='waiting',message='正在由云端兼容作业补下载图片' WHERE id='timing-page'",
  );
  await sql(
    "INSERT INTO compute_jobs(id,kind,input_key,payload,created_at,updated_at) VALUES ('timing-compute','legacy-page','timing-input','{\"parent_job\":\"timing-page\"}',100,100)",
  );
  expect(await detail()).toMatchObject({
    status: "waiting",
    status_label: "等待云执行器",
  });
  await sql(
    "UPDATE compute_jobs SET state='running',lease_until=? WHERE id='timing-compute'",
    now() + 120,
  );
  expect(await detail()).toMatchObject({
    status: "running",
    status_label: "云端处理中",
  });
});

it("cancellation preserves completed children and counters, and cannot cancel a completed task", async () => {
  await sql(
    "INSERT INTO comic_jobs(id,user_id,parent_id,kind,payload,state,created_at,updated_at) VALUES ('timing-done','timing-user','timing-root','chapter','{}','completed',100,100),('timing-pending','timing-user','timing-root','chapter','{}','queued',100,100)",
  );
  await taskAction(bindings, user, "timing-root", "cancel");
  expect(await detail()).toMatchObject({
    state: "cancelled",
    completed_chapters: 1,
  });
  expect(
    await bindings.DB.prepare(
      "SELECT state FROM comic_jobs WHERE id='timing-done'",
    ).first("state"),
  ).toBe("completed");
  await taskAction(bindings, user, "timing-root", "cancel");
  expect((await detail()).completed_chapters).toBe(1);
  await sql("UPDATE comic_jobs SET state='completed' WHERE id='timing-root'");
  await expect(
    taskAction(bindings, user, "timing-root", "cancel"),
  ).rejects.toThrow("任务已经结束");
  expect((await detail()).completed_chapters).toBe(1);
});

it("shows the actual download-budget pause and recovery time without changing completed tasks", async () => {
  let calls = 0;
  const local = {
    ...bindings,
    DATABASE_PROVIDER: "turso",
    APPLICATION: {
      idFromName: () => "jobs",
      get: () => ({
        fetch: async () => {
          calls++;
          return Response.json({
            database: { paused: true, retry_after: 3600 },
          });
        },
      }),
    },
  } as unknown as Env;
  const pending = (await tasks(local, user, "timing-root"))[0];
  expect(pending.status_label).toBe("等待数据库保护恢复");
  expect(pending.status).toBe("waiting");
  expect(pending.log.join(" ")).toContain("北京时间");
  expect(calls).toBe(1);
  await sql("UPDATE comic_jobs SET state='completed' WHERE id='timing-root'");
  expect((await tasks(local, user, "timing-root"))[0].status_label).toBe(
    "已完成",
  );
  expect(calls).toBe(1);
});
