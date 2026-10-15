import { createClient } from "@libsql/client/web";
import { writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { tursoUrl } from "../src/database.ts";
export function dropOrder(tables, foreignKeys) {
  const remaining = new Set(tables),
    order = [];
  while (remaining.size) {
    const ready = [...remaining].filter(
      (name) =>
        ![...remaining].some(
          (other) =>
            other !== name && (foreignKeys.get(other) || []).includes(name),
        ),
    );
    if (!ready.length)
      throw new Error("Database schema has an unexpected foreign-key cycle");
    for (const name of ready) {
      order.push(name);
      remaining.delete(name);
    }
  }
  return order;
}
const quote = (name) => '"' + name.replaceAll('"', '""') + '"';
export async function clearDatabase(client) {
  const schema = (
    await client.execute(
      "SELECT name,type FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','view','trigger')",
    )
  ).rows;
  const tables = schema.filter((x) => x.type === "table").map((x) => x.name);
  if (
    tables.length &&
    !["mangadock_migrations", "users", "books", "comics", "comic_jobs"].every(
      (name) => tables.includes(name),
    )
  )
    throw new Error("Database fingerprint is not MangaDock");
  const keys = new Map();
  for (const table of tables)
    keys.set(
      table,
      (
        await client.execute("PRAGMA foreign_key_list(" + quote(table) + ")")
      ).rows.map((x) => x.table),
    );
  const ordered = dropOrder(tables, keys);
  const statements = [
    ...schema
      .filter((x) => x.type === "trigger")
      .map((x) => "DROP TRIGGER " + quote(x.name)),
    ...schema
      .filter((x) => x.type === "view")
      .map((x) => "DROP VIEW " + quote(x.name)),
    ...ordered.map((name) => "DROP TABLE " + quote(name)),
  ];
  if (statements.length) await client.batch(statements, "write");
  const remaining = (
    await client.execute(
      "SELECT name,type FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','view','trigger')",
    )
  ).rows;
  if (remaining.length)
    throw new Error("MangaDock database still contains user objects");
  return {
    data_cleared: true,
    user_objects_deleted: schema.length,
    user_objects_remaining: 0,
    database_container_deleted: false,
  };
}
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  let client;
  try {
    const url = tursoUrl(process.env.TURSO_DATABASE_URL);
    if (
      new URL(url).hostname !==
      "mangadock-staging-dddinmx.aws-ap-northeast-1.turso.io"
    )
      throw new Error("Refusing to clear a different database");
    if (!process.env.TURSO_AUTH_TOKEN)
      throw new Error("Turso cleanup credential is missing");
    client = createClient({
      url,
      authToken: process.env.TURSO_AUTH_TOKEN,
      intMode: "number",
      fetch: (input, init = {}) =>
        fetch(input, {
          ...init,
          redirect: "error",
          signal: AbortSignal.timeout(30000),
        }),
    });
    const report = {
      observed_at: new Date().toISOString(),
      ...(await clearDatabase(client)),
    };
    await writeFile(
      "turso-cleanup-results.json",
      JSON.stringify(report, null, 2),
    );
    console.log(JSON.stringify(report));
  } catch (error) {
    console.error("::error::Turso cleanup failed: " + error.name);
    process.exitCode = 1;
  } finally {
    client?.close();
  }
}
