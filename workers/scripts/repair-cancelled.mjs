import { createClient } from "@libsql/client";
import { pathToFileURL } from "node:url";
import { writeFile } from "node:fs/promises";

// Requires an externally verified completion count. Never resumes the task.
export async function repairCancelled(client, { id, expected }, apply = false) {
  if (
    !/^[a-f0-9-]{36}$/.test(id) ||
    !Number.isInteger(expected) ||
    expected < 1 ||
    expected > 10000
  )
    throw new Error("Invalid audited repair request");
  const tx = await client.transaction("write");
  try {
    const root = (
      await tx.execute({
        sql: "SELECT state,finished_at,completed_chapters FROM comic_jobs WHERE id=? AND kind='source'",
        args: [id],
      })
    ).rows[0];
    if (!root || root.state !== "cancelled" || !root.finished_at)
      throw new Error("Repair requires a finished cancelled comic task");
    const current = Number(root.completed_chapters);
    if (current === expected) {
      await tx.commit();
      return { id, state: "cancelled", restored: 0, already_correct: true };
    }
    const completed = Number(
      (
        await tx.execute({
          sql: "SELECT COUNT(*) AS n FROM comic_jobs WHERE parent_id=? AND kind='chapter' AND state='completed'",
          args: [id],
        })
      ).rows[0].n,
    );
    if (completed !== current)
      throw new Error("Existing completion counter is inconsistent");
    const rows = (
      await tx.execute({
        sql: "SELECT j.id FROM comic_jobs j JOIN comic_jobs r ON r.id=j.parent_id JOIN chapters c ON c.id=json_extract(j.payload,'$.chapter_id') AND c.comic_id=r.comic_id WHERE r.id=? AND j.kind='chapter' AND j.state='cancelled' AND j.finished_at<r.finished_at AND c.state='ready'",
        args: [id],
      })
    ).rows;
    if (current + rows.length !== expected)
      throw new Error(
        "Historical ready children do not match the verified completion count; no repair applied",
      );
    if (!apply) {
      await tx.rollback();
      return { id, state: "cancelled", candidates: rows.length, dry_run: true };
    }
    for (const row of rows)
      await tx.execute({
        sql: "UPDATE comic_jobs SET state='completed' WHERE id=? AND parent_id=? AND state='cancelled'",
        args: [row.id, id],
      });
    const after = (
      await tx.execute({
        sql: "SELECT state,completed_chapters FROM comic_jobs WHERE id=?",
        args: [id],
      })
    ).rows[0];
    if (
      after.state !== "cancelled" ||
      Number(after.completed_chapters) !== expected
    )
      throw new Error("Repair verification failed");
    await tx.commit();
    return {
      id,
      state: "cancelled",
      restored: rows.length,
      completed_chapters: expected,
    };
  } catch (error) {
    await tx.rollback();
    throw error;
  } finally {
    tx.close();
  }
}
if (
  process.argv[1] &&
  import.meta.url === pathToFileURL(process.argv[1]).href
) {
  const client = createClient({
    url: process.env.TURSO_DATABASE_URL,
    authToken: process.env.TURSO_AUTH_TOKEN,
    intMode: "number",
  });
  try {
    const result = await repairCancelled(
      client,
      JSON.parse(process.env.CANCEL_REPAIR_JSON || "{}"),
      true,
    );
    await writeFile(
      "cancel-repair-results.json",
      JSON.stringify(result, null, 2),
    );
    console.log(JSON.stringify(result));
  } finally {
    client.close();
  }
}
