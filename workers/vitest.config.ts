import { pbkdf2Sync } from "node:crypto";
import { readFile } from "node:fs/promises";
import { unstable_splitSqlQuery } from "wrangler";
import {
  defineWorkersConfig,
  readD1Migrations,
} from "@cloudflare/vitest-pool-workers/config";
export default defineWorkersConfig(async () => ({
  test: {
    include: ["test/**/*.test.ts"],
    poolOptions: {
      workers: {
        isolatedStorage: false,
        singleWorker: true,
        wrangler: { configPath: "./wrangler.jsonc" },
        miniflare: {
          bindings: {
            TEST_LEGACY_HASH:
              "pbkdf2:sha256:260000$legacy-salt$" +
              pbkdf2Sync(
                "test-password-strong-2026",
                "legacy-salt",
                260000,
                32,
                "sha256",
              ).toString("hex"),
            TEST_STRONG_HASH:
              "pbkdf2:sha256:600000$strong-salt$" +
              pbkdf2Sync(
                "test-password-strong-2026",
                "strong-salt",
                600000,
                32,
                "sha256",
              ).toString("hex"),
            // The pool pins an older Wrangler whose SQL splitter truncates
            // triggers containing CASE...END. Use our current CLI's splitter.
            TEST_MIGRATIONS: await Promise.all(
              (await readD1Migrations("./migrations")).map(
                async (migration) => ({
                  ...migration,
                  queries: unstable_splitSqlQuery(
                    await readFile("./migrations/" + migration.name, "utf8"),
                  ),
                }),
              ),
            ),
            COMPUTE_WAKE_TOKEN: "test-wake-token",
            COMPUTE_REF: "a".repeat(40),
            SETTINGS_ENCRYPTION_KEY: "test-integration-key-only",
            FANQIE_API_TOKEN: "test-token",
            FANQIE_INSTANCE_ID: "test-instance",
          },
        },
      },
    },
  },
}));
