import { randomBytes, randomUUID } from "node:crypto";
import { writeFile, access } from "node:fs/promises";
const path = new URL("../.dev.vars", import.meta.url);
try {
  await access(path);
  throw new Error(".dev.vars 已存在；为避免覆盖凭据，请使用现有配置。");
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}
const instance = randomUUID().replaceAll("-", ""),
  registration = "fqr_" + randomBytes(32).toString("base64url");
const response = await fetch("https://fanqie.dddinmx.cn/v1/register", {
  method: "POST",
  headers: {
    "X-MangaDock-Instance-ID": instance,
    "X-MangaDock-Registration-Key": registration,
    "X-MangaDock-Version": "5.0",
    "X-MangaDock-System": "Cloudflare/Workers",
    "User-Agent": "MangaDock/5.0 Workers/0.1",
  },
  signal: AbortSignal.timeout(25000),
});
if (!response.ok) throw new Error(`来源注册 HTTP ${response.status}`);
const payload = await response.json(),
  token = payload.data?.token;
if (!payload.ok || !/^fqk_[A-Za-z0-9_-]{32,128}$/.test(token || ""))
  throw new Error("来源注册响应无效");
await writeFile(
  path,
  `FANQIE_API_TOKEN=${token}\nFANQIE_INSTANCE_ID=${instance}\nFANQIE_REGISTRATION_KEY=${registration}\n`,
  { mode: 0o600, flag: "wx" },
);
console.log("来源凭据已保存在忽略提交的 .dev.vars；未输出令牌。");
