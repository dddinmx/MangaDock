// Small private control records live in R2. SQL row budgets must not spend one
// Durable Object SQLite write for every remote SQL request they protect.
// The existing DO still serializes callers and runs CPU-heavy application code.
import { DatabasePausedError } from "./database-budget";
class ControlStorageUnavailableError extends DatabasePausedError {
  constructor(cause: unknown, resetUntil = 0) {
    super(
      resetUntil
        ? Math.max(1, Math.ceil((resetUntil - Date.now()) / 1000))
        : 60,
    );
    this.name = "ControlStorageUnavailableError";
    this.cause = cause;
    if (resetUntil)
      this.message =
        "Cloudflare 状态存储已达到今日免费写入额度，将在北京时间 08:00 后自动恢复；已下载的图片和任务已保留";
  }
}
type Document = {
  version: 1;
  values: Record<string, unknown>;
  alarm: number | null;
};
const MAX_BYTES = 1048576;
const HOLD_KEY = "_system/control-quota-hold-v1.json";
export function storageFailureKind(error: unknown) {
  for (let i = 0; i < 5 && error instanceof Error; i++, error = error.cause)
    if (
      /cannot perform I\/O on behalf|different request.*(?:context|I\/O)|outside.*request handler/i.test(
        error.message,
      )
    )
      return "foreign_io_context";
    else if (/conditional|If-None-Match|precondition|etag/i.test(error.message))
      return "invalid_storage_condition";
    else if (
      /quota|SQLITE_FULL|rows?.*(?:writ(?:e|ten)|read).*(?:limit|exceed)|(?:limit|exceed).*(?:SQLite|rows?\s+(?:writ(?:e|ten)|read))|storage.*(?:limit|exceed)/i.test(
        error.message,
      )
    )
      return "durable_storage_quota";
  return "durable_storage_unavailable";
}
const REASON_WORDS = new Set(
  "account exceeded daily maximum number rows row written writes write read reads sqlite durable object objects storage limit limits usage reset disabled unavailable timeout invalid list alarm quota request requests different context perform behalf permitted not supported operation failed free plan".split(
    " ",
  ),
);
function reasonWords(error: unknown) {
  const messages: string[] = [];
  for (let i = 0; i < 5 && error instanceof Error; i++, error = error.cause)
    messages.push(error.message);
  // Fixed vocabulary only: no identifiers, URLs, SQL, credentials or raw messages.
  return messages
    .join(" ")
    .toLowerCase()
    .split(/[^a-z]+/)
    .filter((word) => REASON_WORDS.has(word))
    .slice(0, 48);
}
export class ControlStorage {
  private tail: Promise<unknown> = Promise.resolve();
  private bucket: R2Bucket;
  private legacy: DurableObjectStorage;
  private key: string;
  private operation = "control-read";
  private migrationBlockedUntil = 0;
  private previousReset = 0;
  constructor(bucket: R2Bucket, legacy: DurableObjectStorage, id: string) {
    this.bucket = bucket;
    this.legacy = legacy;
    this.key = `_system/control-v1/${id}.json`;
  }
  private serialize<T>(fn: () => Promise<T>) {
    const next = this.tail.then(fn).catch((error) => {
      if (error instanceof ControlStorageUnavailableError) throw error;
      console.error(
        JSON.stringify({
          component: "control-storage",
          error_code: storageFailureKind(error).toUpperCase(),
          operation: this.operation,
          reason_words: reasonWords(error),
        }),
      );
      throw new ControlStorageUnavailableError(error);
    });
    this.tail = next.catch(() => {});
    return next;
  }
  private async read() {
    if (this.migrationBlockedUntil > Date.now())
      throw new ControlStorageUnavailableError(
        new Error("Legacy SQLite row write quota exceeded"),
        this.migrationBlockedUntil,
      );
    this.operation = "r2-read";
    const object = await this.bucket.get(this.key);
    if (object) {
      if (object.size > MAX_BYTES)
        throw new Error("Control state exceeds capacity");
      const document = await object.json<Document>();
      if (
        document.version !== 1 ||
        !document.values ||
        typeof document.values !== "object"
      )
        throw new Error("Invalid persisted control state");
      return { document, etag: object.etag };
    }
    // Read outside a SQLite write transaction. Preserve all old counters and
    // provider holds exactly; inaccessible counters fail closed, never reset.
    const holdObject = await this.bucket.get(HOLD_KEY);
    if (holdObject) {
      if (holdObject.size > 1024) throw new Error("Invalid control quota hold");
      const hold = await holdObject.json<{
        version: number;
        until: number;
        kind: string;
        reset_at?: number;
      }>();
      if (
        hold.version !== 1 ||
        hold.kind !== "legacy_rows_written" ||
        !Number.isSafeInteger(hold.until)
      )
        throw new Error("Invalid control quota hold");
      this.previousReset = Number(hold.reset_at || hold.until);
      if (hold.until > Date.now()) {
        this.migrationBlockedUntil = hold.until;
        throw new ControlStorageUnavailableError(
          new Error("Legacy SQLite row write quota exceeded"),
          hold.until,
        );
      }
    }
    try {
      this.operation = "legacy-list";
      const old = await this.legacy.list({ limit: 2001 });
      if (old.size > 2000)
        throw new Error("Legacy control state exceeds capacity");
      this.operation = "legacy-alarm";
      const alarm = await this.legacy.getAlarm();
      return {
        document: {
          version: 1,
          values: Object.fromEntries(old),
          alarm,
        } as Document,
        etag: null,
      };
    } catch (error) {
      if (storageFailureKind(error) === "durable_storage_quota") {
        const day = new Date().toISOString().slice(0, 10);
        const nextDay = Date.parse(day + "T00:00:00Z") + 86400000;
        const withinResetGrace =
          this.previousReset > 0 &&
          this.previousReset <= Date.now() &&
          Date.now() < this.previousReset + 900000;
        const until = withinResetGrace
          ? Math.min(Date.now() + 60000, this.previousReset + 900000)
          : nextDay;
        this.migrationBlockedUntil = until;
        console.error(
          JSON.stringify({
            component: "control-storage",
            error_code: "DURABLE_STORAGE_QUOTA",
            operation: this.operation,
            reason_words: reasonWords(error),
          }),
        );
        // This records the provider's hold only. Never create/reset quota counters
        // while the original control state cannot be read.
        await this.bucket
          .put(
            HOLD_KEY,
            JSON.stringify({
              version: 1,
              until,
              kind: "legacy_rows_written",
              reset_at: withinResetGrace ? this.previousReset : nextDay,
            }),
            { httpMetadata: { contentType: "application/json" } },
          )
          .catch(() => null);
        throw new ControlStorageUnavailableError(error, until);
      }
      throw new Error("Existing control counters are unavailable", {
        cause: error,
      });
    }
  }
  transaction<T>(fn: (tx: any) => Promise<T>): Promise<T> {
    return this.serialize(async () => {
      for (let attempt = 0; attempt < 5; attempt++) {
        const { document, etag } = await this.read();
        const before = JSON.stringify(document);
        const values = document.values;
        const tx = {
          get: async (keys: string | string[]) =>
            Array.isArray(keys)
              ? new Map(
                  keys
                    .filter((key) => Object.hasOwn(values, key))
                    .map((key) => [key, structuredClone(values[key])]),
                )
              : structuredClone(values[keys]),
          put: async (
            key: string | Record<string, unknown>,
            value?: unknown,
          ) => {
            if (typeof key === "string") values[key] = structuredClone(value);
            else
              for (const [name, item] of Object.entries(key))
                values[name] = structuredClone(item);
          },
          delete: async (keys: string | string[]) => {
            const names = Array.isArray(keys) ? keys : [keys];
            let count = 0;
            for (const key of names)
              if (Object.hasOwn(values, key)) {
                delete values[key];
                count++;
              }
            return Array.isArray(keys) ? count : count > 0;
          },
        };
        const result = await fn(tx);
        const body = JSON.stringify(document);
        if (etag !== null && body === before) return result;
        if (new TextEncoder().encode(body).byteLength > MAX_BYTES)
          throw new Error("Control state exceeds capacity");
        this.operation = "r2-write";
        const saved = await this.bucket.put(this.key, body, {
          httpMetadata: { contentType: "application/json" },
          onlyIf:
            etag === null ? { etagDoesNotMatch: "*" } : { etagMatches: etag },
        });
        if (saved) return result;
      }
      throw new Error("Control state changed too frequently");
    });
  }
  get<T>(key: string): Promise<T | undefined>;
  get<T>(keys: string[]): Promise<Map<string, T>>;
  get<T>(key: string | string[]): Promise<any> {
    return this.transaction((tx) => tx.get(key));
  }
  put<T>(key: string, value: T): Promise<void>;
  put<T>(values: Record<string, T>): Promise<void>;
  put<T>(key: string | Record<string, T>, value?: T) {
    return this.transaction((tx) => tx.put(key, value));
  }
  delete(key: string): Promise<boolean>;
  delete(keys: string[]): Promise<number>;
  delete(key: string | string[]): Promise<any> {
    return this.transaction((tx) => tx.delete(key));
  }
  list<T>(options: DurableObjectListOptions = {}): Promise<Map<string, T>> {
    return this.serialize(async () => {
      const { document } = await this.read();
      let keys = Object.keys(document.values)
        .sort()
        .filter(
          (key) =>
            (!options.prefix || key.startsWith(options.prefix)) &&
            (!options.start || key >= options.start) &&
            (!options.startAfter || key > options.startAfter) &&
            (!options.end || key < options.end),
        );
      if (options.reverse) keys.reverse();
      keys = keys.slice(0, options.limit ?? 2000);
      return new Map(
        keys.map((key) => [key, structuredClone(document.values[key]) as T]),
      );
    });
  }
  getAlarm() {
    return this.serialize(async () => (await this.read()).document.alarm);
  }
  private async changeAlarm(time: number | null) {
    await this.serialize(async () => {
      for (let attempt = 0; attempt < 5; attempt++) {
        const { document, etag } = await this.read();
        document.alarm = time;
        const result = await this.bucket.put(
          this.key,
          JSON.stringify(document),
          {
            httpMetadata: { contentType: "application/json" },
            onlyIf:
              etag === null ? { etagDoesNotMatch: "*" } : { etagMatches: etag },
          },
        );
        if (result) return;
      }
      throw new Error("Control alarm changed too frequently");
    });
    // A free SQLite write quota can reject alarms too. The durable R2 deadline
    // survives, and the scheduled recovery tick drains it without resetting jobs.
    try {
      if (time === null) await this.legacy.deleteAlarm();
      else await this.legacy.setAlarm(time);
    } catch (error) {
      if (storageFailureKind(error) !== "durable_storage_quota") throw error;
    }
  }
  setAlarm(time: number | Date) {
    return this.changeAlarm(Number(time));
  }
  deleteAlarm() {
    return this.changeAlarm(null);
  }
}
