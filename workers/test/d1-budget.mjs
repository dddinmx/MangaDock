import test from "node:test";
import assert from "node:assert/strict";
import { checkD1Budget } from "../scripts/d1-budget.mjs";
const env = {
  CLOUDFLARE_API_TOKEN: "fixture",
  CLOUDFLARE_ACCOUNT_ID: "fixture",
};
const reply = (rows) => async () =>
  Response.json({
    data: {
      viewer: {
        accounts: [
          {
            d1AnalyticsAdaptiveGroups: rows.map(([rowsRead, rowsWritten]) => ({
              sum: { rowsRead, rowsWritten },
            })),
          },
        ],
      },
    },
  });
test("preflight sums usage across all databases and rejects exhausted read or write budgets", async () => {
  await assert.rejects(
    checkD1Budget(
      env,
      reply([
        [2000000, 100],
        [2000001, 100],
      ]),
    ),
    /safety budget reached/,
  );
  await assert.rejects(
    checkD1Budget(env, reply([[10, 80000]])),
    /safety budget reached/,
  );
  assert.deepEqual(
    await checkD1Budget(
      env,
      reply([
        [50, 10],
        [10, 5],
      ]),
    ),
    { read: 60, written: 15 },
  );
});
test("missing analytics fails closed instead of running unbudgeted remote SQL", async () => {
  await assert.rejects(
    checkD1Budget(env, async () =>
      Response.json({ errors: [{ message: "permission denied" }] }),
    ),
    /unavailable/,
  );
});
