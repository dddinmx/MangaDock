import { now, type Env, type Identity } from "./index";
import {
  all,
  one,
  run,
  setting,
  setSetting,
  library,
  type Row,
} from "./library";
import { loadComic } from "./providers";
import { createDownload, enqueue } from "./downloads";
export async function checkUpdates(
  env: Env,
  user: Identity,
  automatic = false,
) {
  const data = await library(env, user, true),
    candidates: Row[] = [];
  for (const comic of data.comics) {
    try {
      const remote = await loadComic(comic.source_url, env),
        local = await all(
          env,
          "SELECT source_id FROM chapters WHERE comic_id=? AND state='ready'",
          comic.id,
        ),
        pending = remote.chapters.filter(
          (ch) => !local.some((row) => row.source_id === ch.id),
        );
      if (pending.length) {
        candidates.push({
          ...comic,
          pending_chapters: pending.length,
          remote_total_chapters: remote.chapters.length,
          local_total_chapters: local.length,
          latest_chapter_title: pending.at(-1)?.title,
        });
        if (automatic)
          try {
            await createDownload(env, user, {
              url: comic.source_url,
              group: comic.group_name,
              format: comic.comic_format || 2,
              is_update: true,
            });
          } catch {}
      }
    } catch {
      candidates.push({
        ...comic,
        status: "error",
        error_message: "来源暂时不可用",
      });
    }
  }
  await setSetting(env, "update_candidates", JSON.stringify(candidates));
  await setSetting(env, "updates_checked_at", String(now()));
  return candidates;
}
export async function scheduleUpdates(env: Env) {
  if ((await setting(env, "comic_update_mode", "manual")) !== "auto") return;
  const last = Number(await setting(env, "auto_update_requested_at", "0"));
  if (now() - last < 43200) return;
  const user = await one(
    env,
    "SELECT id FROM users WHERE role='admin' ORDER BY username LIMIT 1",
  );
  if (
    !user ||
    (await one(
      env,
      "SELECT id FROM comic_jobs WHERE kind='update-check' AND state IN ('queued','running') LIMIT 1",
    ))
  )
    return;
  await enqueue(env, user.id, "update-check", { automatic: true });
  await setSetting(env, "auto_update_requested_at", String(now()));
}
