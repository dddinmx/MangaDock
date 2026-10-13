// Stored in the existing SQLite Durable Object, never in the metered database.
// Reserve before every outbound request, including the driver's own retries.
export interface BudgetTransaction {
  get<T>(key: string): Promise<T | undefined>;
  put<T>(key: string, value: T): Promise<void>;
}
export interface BudgetStorage extends BudgetTransaction {
  transaction<T>(fn: (tx: BudgetTransaction) => Promise<T>): Promise<T>;
}
type BudgetState = {
  day: string;
  requests: number;
  statements: number;
  blockedUntil: number;
  failures: number;
  failureWindow: number;
};
type RemoteError = { code?: string; message?: string };
type PipelineResponse = {
  error?: RemoteError;
  results?: {
    type?: string;
    error?: RemoteError;
    response?: { result?: { step_errors?: (RemoteError | null)[] } };
  }[];
};
const KEY = "database-outbound-budget-v1";
export class DatabasePausedError extends Error {
  constructor() {
    super("数据库保护已暂停请求，请稍后重试并检查额度");
    this.name = "DatabasePausedError";
  }
}
export class DatabaseBudget {
  storage: BudgetStorage;
  clock: () => number;
  private pausedUntil = 0;
  constructor(storage: BudgetStorage, clock = Date.now) {
    this.storage = storage;
    this.clock = clock;
  }
  private async update(fn: (state: BudgetState, time: number) => void | false) {
    return this.storage.transaction(async (tx) => {
      const time = this.clock(),
        day = new Date(time).toISOString().slice(0, 10);
      let state = await tx.get<BudgetState>(KEY);
      if (!state)
        state = {
          day,
          requests: 0,
          statements: 0,
          blockedUntil: 0,
          failures: 0,
          failureWindow: time,
        };
      if (state.day !== day) {
        state.day = day;
        state.requests = 0;
        state.statements = 0;
      }
      if (fn(state, time) !== false) await tx.put(KEY, state);
      return state;
    });
  }
  async reserve(statements: number) {
    if (this.pausedUntil > this.clock()) throw new DatabasePausedError();
    if (!Number.isSafeInteger(statements) || statements < 0)
      throw new DatabasePausedError();
    let reserved = false;
    const state = await this.update((state, time) => {
      if (
        state.blockedUntil > time ||
        state.requests >= 2000 ||
        state.statements + statements > 10000
      )
        return false;
      state.requests++;
      state.statements += statements;
      reserved = true;
    });
    if (!reserved) {
      this.pausedUntil =
        state.blockedUntil > this.clock()
          ? state.blockedUntil
          : Date.parse(state.day + "T00:00:00Z") + 86400000;
      throw new DatabasePausedError();
    }
  }
  async guardedFetch(input: RequestInfo, init: RequestInit = {}) {
    const request = new Request(input, init);
    const body = (await request.clone().json()) as {
      requests?: { type: string; batch?: { steps?: unknown[] } }[];
    };
    const statements = (body.requests || []).reduce(
      (count, item) =>
        count +
        (item.type === "execute"
          ? 1
          : item.type === "batch"
            ? item.batch?.steps?.length || 1
            : 0),
      0,
    );
    await this.reserve(statements);
    try {
      const response = await fetch(request);
      // The protocol can report SQL errors in an HTTP-200 pipeline response.
      const payload = await response
        .clone()
        .json<PipelineResponse>()
        .catch(() => null);
      const errors = [
        payload?.error,
        ...(payload?.results || []).flatMap((item) => [
          item.type === "error" ? item.error : null,
          ...(item.response?.result?.step_errors || []),
        ]),
      ].filter((error): error is RemoteError => Boolean(error));
      const quota = errors.some(
        (error: { code?: string; message?: string }) =>
          /QUOTA|USAGE_LIMIT|SQLITE_FULL|STORAGE_LIMIT|MONTHLY_LIMIT/i.test(
            error.code || "",
          ) ||
          /quota|usage limit|monthly limit|row (read|write).*(exceeded|limit)/i.test(
            error.message || "",
          ),
      );
      const state = await this.update((state, time) => {
        if (quota) {
          const date = new Date(time);
          state.blockedUntil = Date.UTC(
            date.getUTCFullYear(),
            date.getUTCMonth() + 1,
            1,
          );
        } else if ([401, 403, 429].includes(response.status)) {
          state.blockedUntil = Math.max(state.blockedUntil, time + 3600000);
        } else if (response.status >= 500) {
          this.failed(state, time);
        } else {
          state.failures = 0;
        }
      });
      this.pausedUntil = state.blockedUntil;
      return response;
    } catch (error) {
      const state = await this.update((state, time) =>
        this.failed(state, time),
      );
      this.pausedUntil = state.blockedUntil;
      throw error;
    }
  }
  private failed(state: BudgetState, time: number) {
    if (time - state.failureWindow > 60000) {
      state.failureWindow = time;
      state.failures = 0;
    }
    if (++state.failures >= 3)
      state.blockedUntil = Math.max(state.blockedUntil, time + 600000);
  }
}
