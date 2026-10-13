import { createClient } from "@libsql/client/web";
import { readFile, readdir } from "node:fs/promises";
import { pbkdf2Sync, randomBytes, createHash } from "node:crypto";
import { fileURLToPath } from "node:url";
import { tursoUrl } from "../src/database.ts";

export async function migrateTurso(
  client,
  { directory = new URL("../migrations/", import.meta.url), password } = {},
) {
  if (!password || password.length < 6 || password.length > 512)
    throw new Error("MANGADOCK_ADMIN_PASSWORD is missing or invalid");
  await client.execute(
    "CREATE TABLE IF NOT EXISTS mangadock_migrations(name TEXT PRIMARY KEY,sha256 TEXT NOT NULL,applied_at INTEGER NOT NULL)",
  );
  const records = await client.execute(
    "SELECT name,sha256 FROM mangadock_migrations",
  );
  const known = new Map(records.rows.map((row) => [row.name, row.sha256]));
  const files = (await readdir(directory))
      .filter((file) => /^\d+.*\.sql$/.test(file))
      .sort(),
    applied = [];
  for (const name of files) {
    const source = await readFile(new URL(name, directory), "utf8"),
      sha = createHash("sha256").update(source).digest("hex");
    if (known.has(name)) {
      if (known.get(name) !== sha)
        throw new Error("Applied SQL migration changed: " + name);
      continue;
    }
    const transaction = await client.transaction("write");
    try {
      await transaction.executeMultiple(
        source.replace(/^PRAGMA foreign_keys = ON;\s*$/m, ""),
      );
      await transaction.execute({
        sql: "INSERT INTO mangadock_migrations VALUES (?,?,?)",
        args: [name, sha, Math.floor(Date.now() / 1000)],
      });
      await transaction.commit();
      applied.push(name);
    } catch (error) {
      await transaction.rollback().catch(() => {});
      throw new Error("Turso migration failed: " + name, { cause: error });
    } finally {
      transaction.close();
    }
  }
  const salt = randomBytes(16).toString("hex"),
    encoded = `pbkdf2:sha256:600000$${salt}$${pbkdf2Sync(password, salt, 600000, 32, "sha256").toString("hex")}`;
  await client.execute({
    sql: "INSERT INTO users(id,username,password_hash,role) VALUES ('admin','admin',?,'admin') ON CONFLICT(username) DO NOTHING",
    args: [encoded],
  });
  return { applied, administrator_preserved: true };
}
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  let client;
  try {
    const token = process.env.TURSO_AUTH_TOKEN || process.env.TURSO_TOKEN;
    if (!process.env.TURSO_DATABASE_URL || !token)
      throw new Error("TURSO_DATABASE_URL / TURSO_TOKEN Secrets are missing");
    client = createClient({
      url: tursoUrl(process.env.TURSO_DATABASE_URL),
      authToken: token,
      intMode: "number",
      fetch: (input, init = {}) =>
        fetch(input, {
          ...init,
          redirect: "error",
          signal: AbortSignal.timeout(30000),
        }),
    });
    console.log(
      JSON.stringify(
        await migrateTurso(client, {
          password: process.env.MANGADOCK_ADMIN_PASSWORD,
        }),
      ),
    );
  } catch (error) {
    const code = error.cause?.code || error.code;
    console.error(
      "::error::Turso initialization failed: " +
        (error.message?.startsWith("TURSO_") ||
        error.message?.startsWith("MANGADOCK_ADMIN_PASSWORD")
          ? error.message
          : error.name) +
        (typeof code === "string" && /^[A-Z0-9_]{1,60}$/.test(code)
          ? " [" + code + "]"
          : ""),
    );
    process.exitCode = 1;
  } finally {
    client?.close();
  }
}
