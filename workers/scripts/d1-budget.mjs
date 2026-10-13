// Analytics API only. Run before any remote D1 migration or seed statement.
export async function checkD1Budget(env = process.env, request = fetch) {
  const day = new Date().toISOString().slice(0, 10),
    account = env.CLOUDFLARE_ACCOUNT_ID;
  const response = await request(
    "https://api.cloudflare.com/client/v4/graphql",
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.CLOUDFLARE_API_TOKEN}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        query:
          "query($account:string,$day:Date){viewer{accounts(filter:{accountTag:$account}){d1AnalyticsAdaptiveGroups(limit:1000,filter:{date_geq:$day,date_leq:$day}){sum{rowsRead rowsWritten}}}}}",
        variables: { account, day },
      }),
      signal: AbortSignal.timeout(30000),
    },
  );
  const payload = await response.json(),
    groups = payload.data?.viewer?.accounts?.[0]?.d1AnalyticsAdaptiveGroups;
  if (!response.ok || payload.errors?.length || !Array.isArray(groups))
    throw new Error(
      "D1 quota analytics unavailable; remote SQL is blocked until usage can be checked.",
    );
  const usage = groups.reduce(
    (total, row) => ({
      read: total.read + Number(row.sum.rowsRead || 0),
      written: total.written + Number(row.sum.rowsWritten || 0),
    }),
    { read: 0, written: 0 },
  );
  if (usage.read >= 4000000 || usage.written >= 80000)
    throw new Error(
      `D1 account safety budget reached (${usage.read} rows read, ${usage.written} rows written); remote migrations, seed and live tests are blocked. Keep migration traffic paused.`,
    );
  console.log(
    `D1 account preflight: ${usage.read} rows read / ${usage.written} written today; analytics only, no SQL executed.`,
  );
  return usage;
}
