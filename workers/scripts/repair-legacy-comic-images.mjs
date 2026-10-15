import { readFile, writeFile } from "node:fs/promises";
const { url } = JSON.parse(await readFile("deployment.json", "utf8"));
const task = "c6fe02d4-c454-4111-b962-495120175313";
if (
  new URL(url).hostname !== "mangadock-turso-staging.dddinmx.workers.dev" ||
  process.env.DIAGNOSTIC_TASK_ID !== task
)
  throw new Error(
    "Historical image repair requires the exact existing staging task",
  );
const login = await fetch(url + "/api/v1/auth/login", {
  method: "POST",
  headers: { Origin: url, "Content-Type": "application/json" },
  body: JSON.stringify({
    username: "admin",
    password: process.env.MANGADOCK_ADMIN_PASSWORD,
  }),
  signal: AbortSignal.timeout(30000),
  redirect: "error",
});
if (!login.ok) throw new Error("Integrity repair login HTTP " + login.status);
const cookie = login.headers.get("set-cookie")?.split(";")[0] || "";
const { data } = await login.json();
const response = await fetch(
  url + `/api/admin/downloads/${task}/repair-integrity`,
  {
    method: "POST",
    redirect: "error",
    signal: AbortSignal.timeout(120000),
    headers: {
      Origin: url,
      Cookie: cookie,
      "Content-Type": "application/json",
      "X-CSRFToken": data.csrf_token,
    },
    body: JSON.stringify({
      comic_id: "c_ee02de2c2194",
      chapter_ids: [
        "ch_1343b6318ac0b40a75eabb13",
        "ch_3955c68f8c5560c5b02ab4a2",
        "ch_f8f9024b7bd195b0dd7e2f5d",
        "ch_6a87accba4ed80dfd9fd775b",
        "ch_e2ee13b58739e5c778bb910d",
        "ch_2fe93e6f0e7d7e0f4e202c5f",
        "ch_ae317b56917dcbc8e93b3fb4",
        "ch_eb761c3675d7040107e62892",
      ],
    }),
  },
);
const report = await response.json();
await writeFile(
  "comic-integrity-repair-results.json",
  JSON.stringify(report, null, 2),
);
if (!response.ok || !report.ok)
  throw new Error("Historical image repair HTTP " + response.status);
console.log(JSON.stringify(report));
