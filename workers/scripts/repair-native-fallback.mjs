import { readFile } from "node:fs/promises";
const id = process.env.REPAIR_NATIVE_TASK_ID;
if (!/^[a-f0-9-]{36}$/.test(id || ""))
  throw new Error("An exact existing task UUID is required");
if (process.env.DEPLOY_TARGET !== "staging")
  throw new Error("This one-time repair is staging only");
const { url } = JSON.parse(
  await readFile(new URL("../deployment.json", import.meta.url), "utf8"),
);
const response = await fetch(url + "/api/v1/auth/login", {
  method: "POST",
  headers: { Origin: url, "Content-Type": "application/json" },
  body: JSON.stringify({
    username: "admin",
    password: process.env.MANGADOCK_ADMIN_PASSWORD,
  }),
  signal: AbortSignal.timeout(30000),
});
if (!response.ok) throw new Error("Repair login failed: " + response.status);
const cookie = response.headers.get("set-cookie")?.split(";")[0];
const auth = await response.json();
const result = await fetch(
  url + `/api/admin/downloads/${id}/repair-native-fallback`,
  {
    method: "POST",
    headers: {
      Origin: url,
      Cookie: cookie,
      "Content-Type": "application/json",
      "X-CSRFToken": auth.data.csrf_token,
    },
    body: "{}",
    signal: AbortSignal.timeout(60000),
  },
);
const report = await result.json();
if (!result.ok || !report.ok)
  throw new Error(
    "Native fallback repair failed: " +
      result.status +
      " " +
      JSON.stringify(report),
  );
console.log(JSON.stringify(report));
