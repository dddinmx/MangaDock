import test from "node:test";
import assert from "node:assert/strict";
import { computeBudgetSettings } from "../scripts/compute-budget-settings.mjs";
test("compute defaults remain unchanged until explicitly configured", () => {
  assert.deepEqual(computeBudgetSettings({}), {
    DB_COMPUTE_DAILY_REQUEST_LIMIT: "10000",
    DB_COMPUTE_DAILY_STATEMENT_LIMIT: "50000",
  });
});
test("later releases preserve the previously configured compute limits", () => {
  const bindings = [
    {
      type: "plain_text",
      name: "DB_COMPUTE_DAILY_REQUEST_LIMIT",
      text: "200000",
    },
    {
      type: "plain_text",
      name: "DB_COMPUTE_DAILY_STATEMENT_LIMIT",
      text: "200000",
    },
  ];
  const copy = structuredClone(bindings);
  assert.deepEqual(computeBudgetSettings({}, bindings), {
    DB_COMPUTE_DAILY_REQUEST_LIMIT: "200000",
    DB_COMPUTE_DAILY_STATEMENT_LIMIT: "200000",
  });
  assert.deepEqual(bindings, copy);
});
test("an explicit compute selection overrides only its own saved limits", () => {
  assert.deepEqual(
    computeBudgetSettings(
      {
        MANGADOCK_DB_COMPUTE_REQUESTS: "200000",
        MANGADOCK_DB_COMPUTE_STATEMENTS: "200000",
        MANGADOCK_DB_DAILY_REQUESTS: "10000",
      },
      [
        {
          name: "DB_COMPUTE_DAILY_REQUEST_LIMIT",
          type: "plain_text",
          text: "10000",
        },
      ],
    ),
    {
      DB_COMPUTE_DAILY_REQUEST_LIMIT: "200000",
      DB_COMPUTE_DAILY_STATEMENT_LIMIT: "200000",
    },
  );
});
test("invalid or excessive compute budgets fail closed", () => {
  for (const value of ["0", "-1", "200001", "NaN", "1.5", "Infinity"])
    assert.throws(
      () => computeBudgetSettings({ MANGADOCK_DB_COMPUTE_REQUESTS: value }),
      /must be an integer/,
    );
});
