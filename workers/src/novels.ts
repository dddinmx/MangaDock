import { strFromU8, gunzipSync } from "fflate";
import { HttpError, type Env } from "./index";
import { queueCompute } from "./compute";
import { one, run } from "./library";
export async function indexNovel(env: Env, id: string) {
  const book = await one(env, "SELECT * FROM books WHERE id=?", id);
  if (!book) throw new HttpError(404, "小说不存在");
  if (book.index_key) {
    const cached = await env.LIBRARY.get(book.index_key);
    if (cached) return cached.json<any>();
    await run(
      env,
      "UPDATE books SET index_key=NULL WHERE id=? AND index_key=?",
      id,
      book.index_key,
    );
  }
  const latest = await one(
    env,
    "SELECT state FROM compute_jobs WHERE kind='index-epub' AND input_key=? ORDER BY created_at DESC,rowid DESC LIMIT 1",
    book.object_key,
  );
  if (latest?.state === "error")
    throw new HttpError(422, "章节索引生成失败，请管理员检查任务后重试");
  await queueCompute(env, "index-epub", book.object_key, {
    book_id: book.id,
    sha256: book.sha256,
  });
  throw new HttpError(503, "章节索引正在由云端生成，请稍后重试");
}
export async function novelChapter(env: Env, id: string, index: number) {
  const info = await indexNovel(env, id);
  if (
    !Number.isSafeInteger(index) ||
    index < 0 ||
    index >= info.chapters.length
  )
    throw new HttpError(404, "章节不存在");
  const object = await env.LIBRARY.get(info.chapters[index].key);
  if (!object) throw new HttpError(404, "章节文件不存在");
  return info.chapters[index].gzip
    ? JSON.parse(
        strFromU8(gunzipSync(new Uint8Array(await object.arrayBuffer()))),
      )
    : object.json();
}
