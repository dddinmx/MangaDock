// Password + session handling compatible with the Flask app.
// Werkzeug hash format: "pbkdf2:sha256:<iterations>$<salt>$<hex>".
// Cloudflare Workers' WebCrypto rejects PBKDF2 with > 100,000 iterations, so
// hashes created by the Flask app (260,000 iterations) cannot be verified here;
// users must get a new hash via scripts/set-password.mjs (also werkzeug-compatible).
import type { Env } from './env';

const enc = new TextEncoder();
export const WORKERS_PBKDF2_MAX = 100_000;

function toHex(buf: ArrayBuffer): string {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

async function pbkdf2Hex(password: string, salt: string, iterations: number, hash = 'SHA-256'): Promise<string> {
  const key = await crypto.subtle.importKey('raw', enc.encode(password), 'PBKDF2', false, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits({ name: 'PBKDF2', salt: enc.encode(salt), iterations, hash }, key, 256);
  return toHex(bits);
}

export type VerifyResult = 'ok' | 'bad' | 'legacy_unsupported';

export async function verifyPassword(stored: string, password: string): Promise<VerifyResult> {
  const m = /^pbkdf2:(sha256):(\d+)\$([^$]+)\$([0-9a-f]+)$/.exec(stored || '');
  if (!m) return 'bad';
  const iterations = Number(m[2]);
  if (iterations > WORKERS_PBKDF2_MAX) return 'legacy_unsupported';
  const got = await pbkdf2Hex(password, m[3], iterations);
  return timingSafeEqual(got, m[4]) ? 'ok' : 'bad';
}

export async function hashPassword(password: string, iterations: number): Promise<string> {
  const it = Math.min(iterations, WORKERS_PBKDF2_MAX);
  const saltBytes = crypto.getRandomValues(new Uint8Array(12));
  const salt = [...saltBytes].map((b) => 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'[b % 62]).join('');
  return `pbkdf2:sha256:${it}$${salt}$${await pbkdf2Hex(password, salt, it)}`;
}

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

// ---- Signed cookie session: base64url(json).base64url(hmac) ----
export interface Session { uid: number; ver: number; exp: number }

const b64u = (bytes: Uint8Array) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const unb64u = (s: string) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/')), (c) => c.charCodeAt(0));

async function hmacKey(secret: string) {
  return crypto.subtle.importKey('raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify']);
}

export async function signSession(env: Env, s: Session): Promise<string> {
  if (!env.SESSION_SECRET) throw new Error('SESSION_SECRET is not configured');
  const body = b64u(enc.encode(JSON.stringify(s)));
  const sig = new Uint8Array(await crypto.subtle.sign('HMAC', await hmacKey(env.SESSION_SECRET), enc.encode(body)));
  return `${body}.${b64u(sig)}`;
}

export async function readSession(env: Env, req: Request): Promise<Session | null> {
  const cookie = req.headers.get('Cookie') || '';
  const m = /(?:^|;\s*)md_session=([^;]+)/.exec(cookie);
  if (!m || !env.SESSION_SECRET) return null;
  const [body, sig] = m[1].split('.');
  if (!body || !sig) return null;
  try {
    const ok = await crypto.subtle.verify('HMAC', await hmacKey(env.SESSION_SECRET), unb64u(sig), enc.encode(body));
    if (!ok) return null;
    const s = JSON.parse(new TextDecoder().decode(unb64u(body))) as Session;
    return s.exp > Date.now() ? s : null;
  } catch {
    return null;
  }
}

export function sessionCookie(value: string, maxAge = 30 * 86400): string {
  return `md_session=${value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=${maxAge}`;
}

export interface User { id: number; username: string; role: string; can_download: number; session_version: number; password_hash: string }

export async function currentUser(env: Env, req: Request): Promise<User | null> {
  const s = await readSession(env, req);
  if (!s) return null;
  const u = await env.DB.prepare('SELECT id, username, role, can_download, session_version, password_hash FROM user WHERE id = ?').bind(s.uid).first<User>();
  if (!u || Number(u.session_version || 1) !== s.ver) return null;
  return u;
}

/** CSRF defence for cookie-authenticated POSTs: require same-origin Origin/Referer. */
export function sameOrigin(req: Request): boolean {
  const origin = req.headers.get('Origin') || req.headers.get('Referer');
  if (!origin) return false;
  try {
    return new URL(origin).host === new URL(req.url).host;
  } catch {
    return false;
  }
}
