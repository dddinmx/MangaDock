// Persist the last explicitly configured compute budget across later releases.
// Absent configuration keeps the old defaults; this helper never touches usage.
export function computeBudgetSettings(environment, bindings = []) {
  function value(input, binding, fallback) {
    const saved = bindings.find(
      (item) => item.name === binding && item.type === "plain_text",
    )?.text;
    const result = Number(environment[input] || saved || fallback);
    if (!Number.isSafeInteger(result) || result < 1 || result > 200000)
      throw new Error(input + " must be an integer from 1 to 200000");
    return String(result);
  }
  return {
    DB_COMPUTE_DAILY_REQUEST_LIMIT: value(
      "MANGADOCK_DB_COMPUTE_REQUESTS",
      "DB_COMPUTE_DAILY_REQUEST_LIMIT",
      environment.MANGADOCK_DB_DAILY_REQUESTS || 10000,
    ),
    DB_COMPUTE_DAILY_STATEMENT_LIMIT: value(
      "MANGADOCK_DB_COMPUTE_STATEMENTS",
      "DB_COMPUTE_DAILY_STATEMENT_LIMIT",
      environment.MANGADOCK_DB_DAILY_STATEMENTS || 50000,
    ),
  };
}
