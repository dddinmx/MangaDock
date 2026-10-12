import { env } from 'cloudflare:workers';
import { createExecutionContext, waitOnExecutionContext } from 'cloudflare:test';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import worker from '../src/index';
import { hashPassword, verifyPassword } from '../src/auth';
import { decodeImagePayload } from '../src/baozimh';
import { handleMessage } from '../src/queue';

const E = env as any;
const ORIGIN = 'https://md.test';

async function call(path: string, init: RequestInit = {}, cookie = '') {
  const headers = new Headers(init.headers);
  if (cookie) headers.set('Cookie', cookie);
  if (init.method === 'POST' && !headers.has('Origin')) headers.set('Origin', ORIGIN);
  const ctx = createExecutionContext();
  const res = await worker.fetch(new Request(ORIGIN + path, { ...init, headers, redirect: 'manual' }) as any, E, ctx);
  await waitOnExecutionContext(ctx);
  return res;
}

async function loginCookie(): Promise<string> {
  const res = await call('/login', { method: 'POST', body: new URLSearchParams({ username: 'admin', password: 'pw-123456' }) });
  expect(res.status).toBe(302);
  return res.headers.get('Set-Cookie')!.split(';')[0];
}

beforeAll(async () => {
  const h = await hashPassword('pw-123456', 100000);
  await E.DB.prepare(`INSERT OR REPLACE INTO user (id, username, password_hash, role, can_download, can_view_adult, session_version) VALUES (1,'admin',?,'admin',1,1,7)`).bind(h).run();
});
afterEach(() => vi.restoreAllMocks());

describe('auth', () => {
  it('verifies werkzeug pbkdf2 hashes and flags >100k legacy hashes', async () => {
    // Real vector produced by werkzeug.security.generate_password_hash('hello', 'pbkdf2:sha256:100000'):
    expect(await verifyPassword('pbkdf2:sha256:100000$s5yMCNnP7z7suRXQ$4e5ed25977630c6768afd1b38690f69e36dce7e022b7388634d41455e30af9fd', 'hello')).toBe('ok');
    const h = await hashPassword('secret', 1000);
    expect(await verifyPassword(h, 'secret')).toBe('ok');
    expect(await verifyPassword(h, 'nope')).toBe('bad');
    expect(await verifyPassword('pbkdf2:sha256:260000$abc$00', 'x')).toBe('legacy_unsupported');
  });
  it('rejects wrong password and unauthenticated API access', async () => {
    const bad = await call('/login', { method: 'POST', body: new URLSearchParams({ username: 'admin', password: 'x' }) });
    expect(await bad.text()).toContain('用户名或密码错误');
    expect((await call('/api/chapters/x')).status).toBe(401);
  });
  it('rejects cross-origin POST', async () => {
    const c = await loginCookie();
    const r = await call('/save_progress', { method: 'POST', headers: { Origin: 'https://evil.test', 'Content-Type': 'application/json' }, body: '{}' }, c);
    expect(r.status).toBe(403);
  });
});

describe('reading progress in D1', () => {
  it('saves, reads back and ignores stale client snapshots', async () => {
    const c = await loginCookie();
    const post = (b: object) => call('/save_progress', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) }, c);
    expect((await post({ comic_name: '测试', chapter: 3, page: 9, client_progress_ms: 2000 })).status).toBe(200);
    await post({ comic_name: '测试', chapter: 1, page: 1, client_progress_ms: 1000 });
    const r = (await (await call('/get_progress/' + encodeURIComponent('测试'), {}, c)).json()) as any;
    expect(r).toMatchObject({ status: 'success', chapter: 3, page: 9 });
  });
});

describe('baozimh decode', () => {
  it('round-trips the obfuscated payload format', () => {
    // Build an encoded payload with the inverse of decodeImagePayload.
    const FROM = '_-9876543210abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ';
    const TO = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
    const json = JSON.stringify([{ url: '/a/1.webp', order: 1 }, { url: '/a/2.webp', order: 2 }]);
    const b64 = btoa(String.fromCharCode(...new TextEncoder().encode(json))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
    const translated = [...b64].map((ch) => FROM[TO.indexOf(ch)]).join('');
    let mixed = '';
    for (let i = 0, k = 0; i < translated.length; i += 7, k++) { const c = translated.slice(i, i + 7); mixed += k % 2 ? [...c].reverse().join('') : c; }
    const n = mixed.length, t = Math.floor(n / 3), l = Math.floor((n - t) / 2);
    const trailing = mixed.slice(0, t), leading = mixed.slice(t, t + l), middle = mixed.slice(t + l);
    const enc = 'J7r' + leading + 'kD' + middle + 'W4s' + trailing + 'nQ';
    expect(decodeImagePayload(enc)).toEqual([{ url: '/a/1.webp', order: 1 }, { url: '/a/2.webp', order: 2 }]);
  });
});

describe('closed loop with mocked upstream: download -> R2 -> read pages', () => {
  it('runs source/chapter/pages messages and serves pages from R2', async () => {
    const img = new Uint8Array([0x52, 0x49, 0x46, 0x46, 1, 2, 3]);
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input: any) => {
      const u = String(input instanceof Request ? input.url : input);
      if (u.startsWith('https://baozimh.org/manga/')) return new Response('<div data-mid="42"></div>');
      if (u.includes('/api/manga/get')) return Response.json({ code: 200, data: { title: '测试漫画', cover: 'https://img.mock/c.jpg', chapters: [{ id: '7', attributes: { order: 1, title: '第一话' } }] } });
      if (u.includes('/api/v2/chapter/getinfo')) return Response.json({ code: 200, data: { info: { images: { line: 2, images: [{ url: '/p/1.webp', order: 1 }, { url: '/p/2.webp', order: 2 }, { url: '/p/3.webp', order: 3 }] } } } });
      return new Response(img, { headers: { 'Content-Type': 'image/webp' } });
    });
    const c = await loginCookie();
    const sent: any[] = [];
    const queue = { send: async (b: any) => sent.push(b), sendBatch: async (bs: any[]) => sent.push(...bs.map((x) => x.body)) };
    const local = { ...E, DOWNLOADS: queue };
    const ctx = createExecutionContext();
    const res = await worker.fetch(new Request(ORIGIN + '/api/download', { method: 'POST', headers: { Cookie: c, Origin: ORIGIN, 'Content-Type': 'application/json' }, body: JSON.stringify({ url: 'https://baozimh.org/manga/test-1' }) }) as any, local, ctx);
    expect(res.status).toBe(202);
    while (sent.length) await handleMessage(sent.shift(), local);
    const task = (await (await call('/api/tasks/' + ((await res.json()) as any).task_id, {}, c)).json()) as any;
    expect(task).toMatchObject({ status: 'completed', completed_chapters: 1, total_chapters: 1, progress_percent: 100 });
    const ch = (await (await call('/api/chapters/' + encodeURIComponent('测试漫画'), {}, c)).json()) as any;
    expect(ch.chapters[0]).toMatchObject({ order: 1, pages: 3 });
    const page = await call('/api/comic-pages/' + encodeURIComponent('comics/测试漫画/0001_第一话/002.webp'), {}, c);
    expect(page.status).toBe(200);
    expect(new Uint8Array(await page.arrayBuffer())).toEqual(img);
    const reader = await (await call('/reader/' + encodeURIComponent('测试漫画') + '/1', {}, c)).text();
    expect(reader.match(/class="md-page"/g)?.length).toBe(3);
  });
});
