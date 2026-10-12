export interface Env {
  DB: D1Database;
  LIBRARY: R2Bucket;
  DOWNLOADS: Queue<DownloadMessage>;
  ASSETS: Fetcher;
  SESSION_SECRET: string;
  PBKDF2_ITERATIONS: string;
  MAX_CHAPTERS_PER_TASK: string;
  PAGES_PER_MESSAGE: string;
  /** Optional override for tests (points upstream fetches at a mock). */
  BAOZIMH_API_BASE?: string;
}

export type DownloadMessage =
  | { type: 'source'; taskId: string; url: string; maxChapters?: number }
  | { type: 'chapter'; taskId: string; comicName: string; mid: number; chapterId: string; order: number; title: string; filenameBase: string }
  | { type: 'pages'; taskId: string; comicName: string; order: number; jobs: { url: string; key: string }[] };
