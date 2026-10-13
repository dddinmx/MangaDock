import { HttpError, type Env } from "./index";
export async function encryptionKey(env: Env) {
  const secret = (env as Env & { SETTINGS_ENCRYPTION_KEY?: string })
    .SETTINGS_ENCRYPTION_KEY;
  if (!secret) throw new HttpError(503, "尚未配置集成凭据加密密钥");
  return crypto.subtle.importKey(
    "raw",
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(secret)),
    "AES-GCM",
    false,
    ["encrypt", "decrypt"],
  );
}
export async function encrypt(env: Env, value: string) {
  const iv = crypto.getRandomValues(new Uint8Array(12)),
    bytes = new Uint8Array(
      await crypto.subtle.encrypt(
        { name: "AES-GCM", iv },
        await encryptionKey(env),
        new TextEncoder().encode(value),
      ),
    );
  return (
    "v1." +
    btoa(String.fromCharCode(...iv)) +
    "." +
    btoa(String.fromCharCode(...bytes))
  );
}
export async function decrypt(env: Env, value: string) {
  const parts = value.split(".");
  if (parts[0] !== "v1" || parts.length !== 3)
    throw new HttpError(503, "集成凭据格式无效");
  return new TextDecoder().decode(
    await crypto.subtle.decrypt(
      {
        name: "AES-GCM",
        iv: Uint8Array.from(atob(parts[1]), (c) => c.charCodeAt(0)),
      },
      await encryptionKey(env),
      Uint8Array.from(atob(parts[2]), (c) => c.charCodeAt(0)),
    ),
  );
}
export function remoteUrl(raw: string) {
  const url = new URL(raw);
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    (url.port && url.port !== "443") ||
    url.hostname === "localhost" ||
    url.hostname.endsWith(".local") ||
    url.hostname.endsWith(".internal") ||
    url.hostname.includes(":") ||
    /^\d+(?:\.\d+){3}$/.test(url.hostname)
  )
    throw new HttpError(400, "远端地址必须为公开 HTTPS 域名");
  return url;
}
export async function remote(raw: string, init: RequestInit = {}) {
  let url = remoteUrl(raw);
  const authenticated = new Headers(init.headers).has("Authorization");
  for (let count = 0; count < 5; count++) {
    const response = await fetch(url, {
      ...init,
      redirect: "manual",
      signal: AbortSignal.timeout(25000),
    });
    if (authenticated || ![301, 302, 303, 307, 308].includes(response.status))
      return response;
    const location = response.headers.get("Location");
    await response.body?.cancel();
    if (!location) throw new HttpError(502, "来源跳转地址缺失");
    url = remoteUrl(new URL(location, url).href);
  }
  throw new HttpError(502, "来源跳转过多");
}
