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
const REBIND_SUFFIXES = ["nip.io", "sslip.io", "localtest.me", "xip.io"];
function authorityHost(raw: string) {
  const start = raw.indexOf("://");
  if (start < 0) return "";
  let auth = raw.slice(start + 3);
  const cut = auth.search(/[/?#]/);
  if (cut >= 0) auth = auth.slice(0, cut);
  const at = auth.lastIndexOf("@");
  if (at >= 0) auth = auth.slice(at + 1);
  if (auth.startsWith("[")) return auth;
  const colon = auth.lastIndexOf(":");
  if (colon >= 0 && /^\d+$/.test(auth.slice(colon + 1)))
    return auth.slice(0, colon);
  return auth;
}
function isIpLiteral(hostname: string) {
  const host = hostname.toLowerCase();
  // WHATWG parsing usually rewrites decimal, octal, and hex to dotted IPv4
  // and keeps IPv6 in brackets. Also match the raw forms when it does not.
  if (host.startsWith("[") || host.includes(":")) return true;
  if (/^(?:\d{1,3}\.){3}\d{1,3}$/.test(host)) return true;
  const parts = host.split(".");
  if (parts.length < 1 || parts.length > 4) return false;
  return parts.every((part) =>
    /^(?:0x[0-9a-f]+|0[0-7]*|[1-9]\d*)$/i.test(part),
  );
}
function blockedHost(raw: string, hostname: string) {
  const host = hostname.toLowerCase();
  const bare = host.endsWith(".") ? host.slice(0, -1) : host;
  return (
    !host ||
    host.endsWith(".") ||
    authorityHost(raw).endsWith(".") ||
    bare === "localhost" ||
    bare.endsWith(".localhost") ||
    bare.endsWith(".local") ||
    bare.endsWith(".internal") ||
    REBIND_SUFFIXES.some(
      (suffix) => bare === suffix || bare.endsWith("." + suffix),
    ) ||
    isIpLiteral(bare)
  );
}
export function hostAllowed(hostname: string, allowedHosts: readonly string[]) {
  const host = hostname.toLowerCase().replace(/\.$/, "");
  return allowedHosts.some((suffix) => {
    const name = suffix.toLowerCase();
    return host === name || host.endsWith("." + name);
  });
}
export function remoteUrl(raw: string) {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new HttpError(400, "远端地址必须为公开 HTTPS 域名");
  }
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    (url.port && url.port !== "443") ||
    blockedHost(raw, url.hostname)
  )
    throw new HttpError(400, "远端地址必须为公开 HTTPS 域名");
  return url;
}
export function allowedRemoteUrl(raw: string, allowedHosts: readonly string[]) {
  const url = remoteUrl(raw);
  if (!hostAllowed(url.hostname, allowedHosts))
    throw new HttpError(400, "远端地址不在来源允许域名内");
  return url;
}
async function fetchChecked(
  raw: string,
  init: RequestInit,
  check: (value: string) => URL,
) {
  let url = check(raw);
  for (let hop = 0; hop < 5; hop++) {
    const response = await fetch(url, {
      ...init,
      redirect: "manual",
      signal: AbortSignal.timeout(25000),
    });
    if (![301, 302, 303, 307, 308].includes(response.status)) return response;
    const location = response.headers.get("Location");
    await response.body?.cancel();
    if (!location) throw new HttpError(502, "来源跳转地址缺失");
    url = check(new URL(location, url).href);
  }
  throw new HttpError(502, "来源跳转过多");
}
export async function remote(raw: string, init: RequestInit = {}) {
  if (new Headers(init.headers).has("Authorization")) {
    const response = await fetch(remoteUrl(raw), {
      ...init,
      redirect: "manual",
      signal: AbortSignal.timeout(25000),
    });
    return response;
  }
  return fetchChecked(raw, init, remoteUrl);
}
export function safeFetch(
  raw: string,
  init: RequestInit = {},
  allowedHosts: readonly string[] = [],
) {
  return fetchChecked(raw, init, (value) =>
    allowedRemoteUrl(value, allowedHosts),
  );
}
