import { HttpError, now, type Env, type Identity } from "./index";
import {
  one,
  all,
  run,
  comicById,
  bookById,
  novelPermissionsEnforced,
  type Row,
} from "./library";
export function serializeProgress(row: Row | null) {
  return row
    ? {
        last_chapter: row.chapter,
        last_page: row.page,
        scroll_position: row.scroll,
        client_progress_ms: row.client_ms,
        anchor_paragraph: row.anchor_paragraph,
        anchor_offset: row.anchor_offset,
        last_read_at: new Date(row.updated_at * 1000).toISOString(),
      }
    : null;
}
export function apiProgress(row: Row | null) {
  return row
    ? {
        chapter_index: row.chapter,
        page_index: row.page,
        scroll_position: row.scroll,
        total_chapters: row.total_chapters || 0,
        total_pages: row.total_pages || 0,
        updated_at: new Date(row.updated_at * 1000).toISOString(),
      }
    : null;
}
export async function saveProgress(
  env: Env,
  user: Identity,
  data: Row,
  mediaId?: string,
) {
  const name = mediaId || String(data.comic_name || ""),
    novel = name.startsWith("novel:"),
    id = novel ? name.slice(6) : name;
  const media = novel
    ? await bookById(env, user, id)
    : await comicById(env, user, id);
  if (!media) throw new HttpError(404, "作品不存在");
  const chapter = Number(data.chapter),
    page = Number(data.page || 0),
    scroll = Number(data.scroll_position || 0),
    stamp = Number(data.client_progress_ms || Date.now());
  for (const value of [chapter, page, scroll, stamp])
    if (!Number.isSafeInteger(value) || value < 0)
      throw new HttpError(400, "阅读进度无效");
  if (
    chapter > 100000 ||
    page > 100000 ||
    scroll > 100000000 ||
    stamp > Date.now() + 300000
  )
    throw new HttpError(400, "阅读进度超出范围");
  const table = novel ? "progress" : "comic_progress",
    column = novel ? "book_id" : "comic_id";
  await run(
    env,
    `INSERT INTO ${table}(user_id,${column},chapter,page,scroll,client_ms,updated_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id,${column}) DO UPDATE SET chapter=excluded.chapter,page=excluded.page,scroll=excluded.scroll,client_ms=excluded.client_ms,updated_at=excluded.updated_at WHERE excluded.client_ms>${table}.client_ms`,
    user.user_id,
    media.id,
    chapter,
    page,
    scroll,
    stamp,
    now(),
  );
  if (!novel) {
    const totalChapters = Number(data.total_chapters || 0),
      totalPages = Number(data.total_pages || 0);
    if (
      Number.isSafeInteger(totalChapters) &&
      Number.isSafeInteger(totalPages) &&
      totalChapters >= 0 &&
      totalChapters <= 100000 &&
      totalPages >= 0 &&
      totalPages <= 100000
    )
      await run(
        env,
        "UPDATE comic_progress SET total_chapters=?,total_pages=? WHERE user_id=? AND comic_id=? AND client_ms=?",
        totalChapters,
        totalPages,
        user.user_id,
        media.id,
        stamp,
      );
  }
  if (
    novel &&
    Number.isSafeInteger(data.anchor_paragraph) &&
    Number.isSafeInteger(data.anchor_offset) &&
    data.anchor_paragraph >= 0 &&
    data.anchor_offset >= 0
  )
    await run(
      env,
      "UPDATE progress SET anchor_paragraph=?,anchor_offset=? WHERE user_id=? AND book_id=? AND client_ms=?",
      data.anchor_paragraph,
      data.anchor_offset,
      user.user_id,
      media.id,
      stamp,
    );
  const seconds = Number(
      data.reading_time ||
        data.reading_time_seconds ||
        data.reading_seconds ||
        0,
    ),
    session = String(data.reading_session_id || "");
  if (
    session &&
    session.length <= 128 &&
    Number.isSafeInteger(seconds) &&
    seconds > 0 &&
    seconds <= 86400
  ) {
    const day = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date());
    await env.DB.batch([
      env.DB.prepare(
        `INSERT INTO reading_time(user_id,media_id,day,seconds) SELECT ?,?,?,MAX(0,?-COALESCE((SELECT seconds FROM reading_reports WHERE user_id=? AND media_id=? AND session_id=?),0)) ON CONFLICT(user_id,media_id,day) DO UPDATE SET seconds=seconds+excluded.seconds`,
      ).bind(
        user.user_id,
        novel ? "novel:" + media.id : media.id,
        day,
        seconds,
        user.user_id,
        novel ? "novel:" + media.id : media.id,
        session,
      ),
      env.DB.prepare(
        "INSERT INTO reading_reports VALUES (?,?,?,?) ON CONFLICT(user_id,media_id,session_id) DO UPDATE SET seconds=MAX(seconds,excluded.seconds)",
      ).bind(
        user.user_id,
        novel ? "novel:" + media.id : media.id,
        session,
        seconds,
      ),
      env.DB.prepare(
        "UPDATE reading_time SET media_name=? WHERE user_id=? AND media_id=? AND day=?",
      ).bind(
        novel ? name.slice(6) : media.name,
        user.user_id,
        novel ? "novel:" + media.id : media.id,
        day,
      ),
    ]);
  }
  if (
    !session &&
    Number.isSafeInteger(seconds) &&
    seconds > 0 &&
    seconds <= 86400
  ) {
    const day = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date());
    await run(
      env,
      "INSERT INTO reading_time(user_id,media_id,day,seconds,media_name) VALUES (?,?,?,?,?) ON CONFLICT(user_id,media_id,day) DO UPDATE SET seconds=seconds+excluded.seconds,media_name=excluded.media_name",
      user.user_id,
      novel ? "novel:" + media.id : media.id,
      day,
      seconds,
      novel ? name.slice(6) : media.name,
    );
  }
}
export function timeDisplay(minutes: number) {
  const value = Math.max(0, Math.floor(minutes)),
    hours = Math.floor(value / 60),
    remaining = value % 60;
  return hours
    ? remaining
      ? `${hours}小时 ${remaining}分钟`
      : `${hours}小时`
    : `${remaining}分钟`;
}
export async function statistics(
  env: Env,
  user: Identity,
  rawYear: string | null,
  _mode: string,
  api = false,
) {
  const today = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).format(new Date()),
    currentYear = Number(today.slice(0, 4)),
    requested = Number(rawYear),
    year =
      Number.isInteger(requested) &&
      requested >= 2000 &&
      requested <= currentYear
        ? requested
        : currentYear;
  const days = await all(
    env,
    "SELECT day,SUM(seconds) AS seconds FROM reading_time WHERE user_id=? AND media_id NOT LIKE 'drama:%' AND substr(day,1,4)=? GROUP BY day",
    user.user_id,
    String(year),
  );
  const total = await one(
    env,
    "SELECT SUM(seconds) AS seconds FROM reading_time WHERE user_id=? AND media_id NOT LIKE 'drama:%'",
    user.user_id,
  );
  const monthly = Array(12).fill(0);
  for (const row of days)
    monthly[Number(row.day.slice(5, 7)) - 1] += row.seconds;
  const maximum = Math.max(
      0,
      ...days.map((row) => Math.ceil(row.seconds / 60)),
    ),
    lookup = new Map(days.map((row) => [row.day, Math.ceil(row.seconds / 60)]));
  const start = new Date(Date.UTC(year, 0, 1));
  start.setUTCDate(start.getUTCDate() - ((start.getUTCDay() + 6) % 7));
  const last = new Date(Date.UTC(year, 11, 31)),
    todayDate = new Date(today + "T00:00:00Z"),
    end = api ? last : new Date(Math.min(last.getTime(), todayDate.getTime()));
  const weeks: Row[][] = [],
    labels: string[] = [];
  for (let cursor = start; cursor <= end;) {
    const cells: Row[] = [];
    let label = weeks.length ? "" : "1月";
    for (let i = 0; i < 7; i++) {
      const key = cursor.toISOString().slice(0, 10),
        duration = lookup.get(key) || 0;
      cells.push({
        date: key,
        day: cursor.getUTCDate(),
        duration,
        level: duration ? Math.ceil((duration / Math.max(maximum, 1)) * 4) : 0,
        is_in_range: cursor.getUTCFullYear() === year && cursor <= end,
        is_current_year: cursor.getUTCFullYear() === year,
        is_today: key === today,
      });
      if (cursor.getUTCDate() === 1) label = cursor.getUTCMonth() + 1 + "月";
      cursor = new Date(cursor.getTime() + 86400000);
    }
    weeks.push(cells);
    labels.push(label);
  }
  const rows = await all(
      env,
      `SELECT r.media_id,r.media_name,r.duration,c.name AS comic_name,b.title AS novel_title,b.author AS novel_author,
      CASE WHEN c.id IS NOT NULL THEN 1 ELSE 0 END AS comic_exists
      FROM (SELECT media_id,MAX(media_name) AS media_name,SUM(seconds) AS duration FROM reading_time
        WHERE user_id=? AND media_id NOT LIKE 'drama:%' GROUP BY media_id) r
      LEFT JOIN comics c ON c.id=r.media_id
      LEFT JOIN books b ON r.media_id LIKE 'novel:%' AND b.id=substr(r.media_id,7)
      WHERE (c.id IS NULL OR ?='admin' OR EXISTS(SELECT 1 FROM group_permissions p WHERE p.user_id=? AND p.group_name=c.group_name))
      AND (b.id IS NULL OR ?='admin' OR ?=0 OR EXISTS(SELECT 1 FROM novel_group_permissions np WHERE np.user_id=? AND np.group_name=b.group_name))
      ORDER BY r.duration DESC LIMIT 1000`,
      user.user_id,
      user.role,
      user.user_id,
      user.role,
      novelPermissionsEnforced(env) ? 1 : 0,
      user.user_id,
    ),
    rank: Row[] = [];
  for (const item of rows) {
    const novel = item.media_id.startsWith("novel:"),
      media = novel
        ? item.novel_title
          ? { title: item.novel_title, author: item.novel_author }
          : null
        : item.comic_exists
          ? { name: item.comic_name }
          : null;
    if (api && (novel || !media)) continue;
    const name =
      (novel ? media?.title : media?.name) || item.media_name || "已移除作品";
    rank.push({
      ...item,
      comic_id: novel ? null : item.media_id,
      title: name,
      comic_name: name,
      subtitle: novel ? media?.author || "小说" : "漫画",
      media_type: novel ? "小说" : "漫画",
      total_duration: Math.ceil(item.duration / 60),
      cover_url: novel
        ? "/novel/" + encodeURIComponent(item.media_id.slice(6)) + "/cover"
        : "/static/cover/" + encodeURIComponent(name) + ".jpg",
    });
  }
  const rankMax = Math.max(0, ...rank.map((row) => row.total_duration));
  for (const item of rank)
    item.ratio = rankMax ? (item.total_duration / rankMax) * 100 : 0;
  const months = monthly.map((seconds) => Math.ceil(seconds / 60)),
    totalMinutes = Math.ceil((total?.seconds || 0) / 60);
  if (api)
    return {
      year,
      total_time_minutes: totalMinutes,
      total_time_display: timeDisplay(totalMinutes),
      monthly: {
        labels: Array.from({ length: 12 }, (_, i) => i + 1 + "月"),
        durations: months,
        max_duration: Math.max(0, ...months),
      },
      heatmap: { month_labels: labels, weeks, max_daily_duration: maximum },
      rankings: rank.map((item, i) => ({
        rank: i + 1,
        comic_id: item.comic_id,
        title: item.title,
        cover_url: item.cover_url,
        total_time_minutes: item.total_duration,
        total_time_display: timeDisplay(item.total_duration),
      })),
    };
  return {
    stats_year: year,
    total_time: totalMinutes,
    reading_time_rank: rank,
    monthly_data: {
      months: Array.from({ length: 12 }, (_, i) => i + 1 + "月"),
      durations: months,
    },
    max_duration: Math.max(0, ...months),
    heatmap_weeks: weeks,
    heatmap_month_labels: labels,
    max_daily_duration: maximum,
  };
}
