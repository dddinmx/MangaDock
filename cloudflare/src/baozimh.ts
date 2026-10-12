// Port of mangadock/services/providers/baozimh_org.py (JSON API, no browser needed).
export const BAOZIMH_API_BASE = 'https://v2.apikk.top';
const IMAGE_HOST = 'https://c-nd2-1.6wm.top';
const DEFAULT_IMAGE_HOST = 'https://c-nd3-1.6wm.top';
const FROM = '_-9876543210abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ';
const TO = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36';

export const URL_PATTERN = /^https:\/\/(?:www\.)?baozimh\.org\/manga\/[^/?#]+\/?$/;

export function headers(referer = 'https://baozimh.org/'): HeadersInit {
  return { 'User-Agent': UA, Referer: referer, Accept: '*/*' };
}

export function sanitizeFilename(name: string): string {
  return (name || '').replace(/[\\/:*?"<>|\x00-\x1f]/g, '_').trim().slice(0, 200) || '未知漫画';
}

export function decodeImagePayload(encoded: string): { url: string; order?: number }[] {
  const prefix = 'J7r', inner = 'kD', split = 'W4s', suffix = 'nQ', chunk = 7;
  if (typeof encoded !== 'string' || !encoded.startsWith(prefix) || !encoded.endsWith(suffix)) throw new Error('章节图片数据格式无效');
  const body = encoded.slice(prefix.length, -suffix.length);
  const payloadLen = body.length - inner.length - split.length;
  if (payloadLen <= 0) throw new Error('章节图片数据格式无效');
  const trailingLen = Math.floor(payloadLen / 3);
  const leadingLen = Math.floor((payloadLen - trailingLen) / 2);
  const middleLen = payloadLen - trailingLen - leadingLen;
  const leading = body.slice(0, leadingLen);
  const innerS = body.slice(leadingLen, leadingLen + inner.length);
  const ms = leadingLen + inner.length;
  const middle = body.slice(ms, ms + middleLen);
  const ss = ms + middleLen;
  const splitS = body.slice(ss, ss + split.length);
  const trailing = body.slice(ss + split.length);
  if (innerS !== inner || splitS !== split || trailing.length !== trailingLen) throw new Error('章节图片数据格式无效');
  const mixed = trailing + leading + middle;
  let restored = '';
  for (let i = 0, idx = 0; i < mixed.length; i += chunk, idx++) {
    const c = mixed.slice(i, i + chunk);
    restored += idx % 2 ? [...c].reverse().join('') : c;
  }
  let translated = '';
  for (const ch of restored) {
    const p = FROM.indexOf(ch);
    translated += p >= 0 ? TO[p] : ch;
  }
  const b64 = translated.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (translated.length % 4)) % 4);
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  const images = JSON.parse(new TextDecoder().decode(bytes));
  if (!Array.isArray(images)) throw new Error('章节图片数据格式无效');
  return images;
}

export interface Source { title: string; cover: string | null; description: string; mid: number; chapters: { order: number; title: string; filenameBase: string; chapterId: string }[] }

export async function loadSource(url: string, apiBase = BAOZIMH_API_BASE): Promise<Source> {
  const page = await fetch(url, { headers: headers() });
  if (!page.ok) throw new Error(`详情页 HTTP ${page.status}`);
  const html = await page.text();
  const mid = /data-mid="([0-9]+)"/.exec(html);
  if (!mid) throw new Error('未找到 baozimh.org 漫画 ID');
  const res = await fetch(`${apiBase}/api/manga/get?mid=${mid[1]}&mode=all`, { headers: headers() });
  if (!res.ok) throw new Error(`漫画信息 HTTP ${res.status}`);
  const info = (await res.json()) as any;
  if (info.code !== 200) throw new Error('获取 baozimh.org 漫画信息失败');
  const data = info.data || {};
  const chapters = [...(data.chapters || [])]
    .sort((a: any, b: any) => Number(a.attributes?.order || 0) - Number(b.attributes?.order || 0))
    .map((c: any, i: number) => {
      const order = Number(c.attributes?.order ?? i + 1);
      const title = c.attributes?.title || `第${order}话`;
      return { order, title, filenameBase: `${String(order).padStart(4, '0')}_${sanitizeFilename(title)}`, chapterId: String(c.id) };
    });
  return { title: sanitizeFilename(data.title || '未知漫画'), cover: data.cover || null, description: (data.desc || data.description || '').trim(), mid: Number(mid[1]), chapters };
}

export async function loadChapterImages(mid: number, chapterId: string, apiBase = BAOZIMH_API_BASE): Promise<string[]> {
  const res = await fetch(`${apiBase}/api/v2/chapter/getinfo?m=${mid}&c=${chapterId}`, { headers: headers() });
  if (!res.ok) throw new Error(`章节信息 HTTP ${res.status}`);
  const info = (await res.json()) as any;
  if (info.code !== 200) throw new Error('获取章节图片信息失败');
  const imageData = info.data?.info?.images || {};
  let raw = imageData.images || [];
  if (typeof raw === 'string') raw = decodeImagePayload(raw);
  const host = Number(imageData.line) === 2 ? IMAGE_HOST : DEFAULT_IMAGE_HOST;
  return (raw as any[])
    .filter((x) => x && typeof x === 'object' && x.url)
    .map((x, i) => ({ order: Number(x.order || i + 1), url: /^https?:\/\//.test(x.url) ? x.url : host + x.url }))
    .sort((a, b) => a.order - b.order)
    .map((x) => x.url);
}

export function extOf(url: string, fallback = '.webp'): string {
  const m = /\.(jpe?g|png|webp|gif|avif)(?:$|\?)/i.exec(new URL(url).pathname);
  return m ? `.${m[1].toLowerCase()}` : fallback;
}
