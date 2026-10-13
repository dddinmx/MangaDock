// Nunjucks compiles at build time; only its interpreter runs in a SQLite Durable Object.
// @ts-ignore the browser runtime deliberately has no filesystem or dynamic compiler
import nunjucks from "./generated/runtime";
import templates from "./generated/templates";
import routes from "../legacy-routes.json";
const engine = new nunjucks.Environment(
  new nunjucks.PrecompiledLoader(templates),
  { autoescape: true },
);
nunjucks.installJinjaCompat();
function pybool(value: any) {
  if (value == null || value === false || value === 0 || value === "")
    return false;
  if (Array.isArray(value)) return value.length > 0;
  if (value instanceof Map || value instanceof Set) return value.size > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}
engine.addGlobal("__pybool", pybool);
engine.addFilter("pybool", pybool);
engine.addFilter(
  "default",
  (value: any, fallback: any = "", boolean = false) =>
    value === undefined || (boolean && !pybool(value)) ? fallback : value,
);
engine.addFilter(
  "tojson",
  (value: unknown) =>
    new nunjucks.runtime.SafeString(
      JSON.stringify(value ?? null)
        .replaceAll("<", "\\u003c")
        .replaceAll("\u2028", "\\u2028")
        .replaceAll("\u2029", "\\u2029"),
    ),
);
engine.addFilter(
  "sliceitems",
  (value: any, start: number, end: number) => value?.slice(start, end) ?? [],
);
engine.addFilter(
  "selectattr",
  (items: any[], key: string, op: string, value: unknown) =>
    items.filter((item) =>
      op === "equalto" ? item[key] === value : !!item[key],
    ),
);
const endpoints = new Map(
  routes
    .filter((r) => !r.path.startsWith("/api/v1"))
    .map((r) => [r.endpoint, r.path]),
);
export function urlFor(
  endpoint: string,
  options: Record<string, unknown> = {},
) {
  if (endpoint === "static") return "/static/" + String(options.filename || "");
  const pattern = endpoints.get(endpoint);
  if (!pattern) throw new Error("Unknown original endpoint " + endpoint);
  const used = new Set<string>();
  const path = pattern.replace(/<(?:(?:int|path):)?(\w+)>/g, (_, key) => {
    used.add(key);
    return encodeURIComponent(String(options[key] ?? ""));
  });
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(options))
    if (
      !used.has(key) &&
      value !== undefined &&
      value !== null &&
      key !== "__keywords"
    )
      query.set(key, String(value));
  return path + (query.size ? "?" + query : "");
}
export function date(seconds: number | null | undefined) {
  if (seconds == null) return null;
  const d = new Date(seconds * 1000);
  return {
    strftime: (format: string) => {
      const parts = new Intl.DateTimeFormat("en-CA", {
          timeZone: "Asia/Shanghai",
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          hourCycle: "h23",
        }).formatToParts(d),
        v = Object.fromEntries(parts.map((p) => [p.type, p.value]));
      return format.replace(
        /%[YmdHM]/g,
        (key) =>
          v[
            (
              {
                "%Y": "year",
                "%m": "month",
                "%d": "day",
                "%H": "hour",
                "%M": "minute",
              } as Record<string, string>
            )[key]
          ],
      );
    },
    timestamp: () => seconds,
  };
}
export function render(
  name: string,
  context: Record<string, unknown>,
  status = 200,
) {
  return new Response(
    engine.render(name, {
      asset_url: (path: string) => "/static/" + path,
      url_for: urlFor,
      display_reading_minutes: (minutes: number) => {
        const safe = Math.max(0, Math.trunc(Number(minutes) || 0)),
          hours = Math.floor(safe / 60),
          rest = safe % 60;
        return hours
          ? `${hours}小时${rest ? ` ${rest}分钟` : ""}`
          : `${rest}分钟`;
      },
      cover_image_url: (name: string) =>
        "/static/cover/" + encodeURIComponent(name) + ".jpg",
      cover_hero_image_url: (name: string) =>
        "/static/cover/hero/" + encodeURIComponent(name) + ".jpg",
      cover_hero_mobile_image_url: (name: string) =>
        "/static/cover/hero/mobile/" + encodeURIComponent(name) + ".jpg",
      is_adult_comic: (comic: any) => !!comic.adult,
      ...context,
    }),
    {
      status,
      headers: {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "private, no-store",
      },
    },
  );
}
export function pagination(items: any[], raw: string | null, size = 24) {
  const pages = Math.max(1, Math.ceil(items.length / size)),
    page = Math.min(pages, Math.max(1, Number.parseInt(raw || "1") || 1));
  const candidates = [
    ...new Set([
      1,
      pages,
      ...Array.from({ length: 5 }, (_, i) => page - 2 + i).filter(
        (i) => i >= 1 && i <= pages,
      ),
    ]),
  ].sort((a, b) => a - b);
  const links: (number | null)[] = [];
  let previous = 0;
  for (const candidate of candidates) {
    if (previous && candidate - previous > 1) links.push(null);
    links.push(candidate);
    previous = candidate;
  }
  return {
    items: items.slice((page - 1) * size, page * size),
    pagination: {
      page,
      pages,
      total: items.length,
      per_page: size,
      total_items: items.length,
      total_pages: pages,
      page_links: links,
      has_prev: page > 1,
      has_next: page < pages,
      prev_num: page - 1,
      next_num: page + 1,
      prev_page: page - 1,
      next_page: page + 1,
      iter_pages: () => links,
    },
  };
}
