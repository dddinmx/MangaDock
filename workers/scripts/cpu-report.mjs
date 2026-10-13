import { readFile, writeFile, appendFile } from "node:fs/promises";
const deployment = JSON.parse(await readFile("deployment.json", "utf8")),
  name = new URL(deployment.url).hostname.split(".")[0];
const since = new Date(Date.now() - 1800000).toISOString();
const query = `query($accountTag: string, $filter: WorkersInvocationsAdaptiveFilter_InputObject) { viewer { accounts(filter: {accountTag: $accountTag}) { workersInvocationsAdaptive(limit: 1000, filter: $filter) { dimensions { scriptName status } sum { requests errors } quantiles { cpuTimeP50 cpuTimeP90 cpuTimeP99 } max { cpuTime } } } } }`;
let report;
try {
  const response = await fetch("https://api.cloudflare.com/client/v4/graphql", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${process.env.CLOUDFLARE_API_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      query,
      variables: {
        accountTag: process.env.CLOUDFLARE_ACCOUNT_ID,
        filter: {
          scriptName: name,
          datetime_geq: since,
          datetime_leq: new Date().toISOString(),
        },
      },
    }),
    signal: AbortSignal.timeout(30000),
  });
  const payload = await response.json();
  if (!response.ok || payload.errors?.length)
    report = {
      verified: false,
      reason:
        "Cloudflare CPU analytics unavailable; token may need Account Analytics:Read, or query schema may need updating.",
      api_errors: (payload.errors || []).map((error) => ({
        message: String(error.message || "")
          .replaceAll(
            process.env.CLOUDFLARE_ACCOUNT_ID || "__no_account__",
            "[account]",
          )
          .replaceAll(
            process.env.CLOUDFLARE_API_TOKEN || "__no_token__",
            "[token]",
          ),
      })),
    };
  else {
    const samples =
      payload.data?.viewer?.accounts?.[0]?.workersInvocationsAdaptive || [];
    report = {
      verified:
        samples.reduce((n, row) => n + row.sum.requests, 0) >= 20 &&
        samples.every(
          (row) =>
            row.sum.errors === 0 &&
            typeof row.max.cpuTime === "number" &&
            row.max.cpuTime >= 0 &&
            row.max.cpuTime <= 10000,
        ),
      unit: "microseconds (Cloudflare Workers GraphQL)",
      worker_http_limit_us: 10000,
      window_start: since,
      samples,
      reason: samples.length
        ? "Review HTTP/Queue CPU and outcomes; Durable Object CPU has a separate 30s limit."
        : "Metrics not yet available; not proof of free CPU compliance.",
    };
  }
} catch {
  report = { verified: false, reason: "CPU analytics network request failed." };
}
await writeFile("cpu-results.json", JSON.stringify(report, null, 2));
console.log(JSON.stringify(report, null, 2));
console.log(
  "::notice::Workers CPU report " +
    JSON.stringify(report)
      .replaceAll("%", "%25")
      .replaceAll("\n", "%0A")
      .replaceAll("\r", "%0D"),
);
if (process.env.GITHUB_STEP_SUMMARY)
  await appendFile(
    process.env.GITHUB_STEP_SUMMARY,
    `\nCPU 自动验证：${report.verified ? "通过" : "待核验"}；详见 cpu-results.json。\n`,
  );
