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
  lastFailure?: { kind: string; at: number; status?: number };
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
  retryAfter: number;
  constructor(retryAfter = 3600) {
    super("数据库保护已暂停请求，请稍后重试并检查额度");
    this.name = "DatabasePausedError";
    this.retryAfter = retryAfter;
  }
}
export function databasePaused(error: unknown) {
  for (let i = 0; i < 5 && error instanceof Error; i++) {
    if (error instanceof DatabasePausedError) return true;
    error = error.cause;
  }
  return false;
}
export function databaseRetryAfter(error: unknown) {
  for (let i = 0; i < 5 && error instanceof Error; i++) {
    if (error instanceof DatabasePausedError) return error.retryAfter;
    error = error.cause;
  }
  return 60;
}
export class DatabaseBudget {
  storage: BudgetStorage;
  clock: () => number;
  private pausedUntil = 0;
  private limits: { requests: number; statements: number };
  constructor(
    storage: BudgetStorage,
    clock = Date.now,
    limits: { requests?: number; statements?: number } = {},
  ) {
    this.storage = storage;
    this.clock = clock;
    this.limits = {
      requests: limits.requests ?? 2000,
      statements: limits.statements ?? 10000,
    };
    if (
      !Object.values(this.limits).every(
        (value) => Number.isSafeInteger(value) && value > 0 && value <= 1000000,
      )
    )
      throw new Error("数据库保护上限必须是 1–1000000 的整数");
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
  async snapshot() {
    const time = this.clock(),
      day = new Date(time).toISOString().slice(0, 10);
    const stored = await this.storage.get<BudgetState>(KEY);
    const requests = stored?.day === day ? stored.requests : 0;
    const statements = stored?.day === day ? stored.statements : 0;
    const pausedUntil = Math.max(
      this.pausedUntil,
      stored?.blockedUntil || 0,
      requests >= this.limits.requests || statements >= this.limits.statements
        ? Date.parse(day + "T00:00:00Z") + 86400000
        : 0,
    );
    return {
      day,
      requests,
      statements,
      request_limit: this.limits.requests,
      statement_limit: this.limits.statements,
      paused: pausedUntil > time,
      retry_after: Math.max(0, Math.ceil((pausedUntil - time) / 1000)),
      pause_reason:
        pausedUntil <= time
          ? null
          : requests >= this.limits.requests
            ? "request_limit"
            : statements >= this.limits.statements
              ? "statement_limit"
              : stored?.lastFailure?.kind || "temporary_hold",
      last_failure: stored?.lastFailure || null,
    };
  }
  async reserve(statements: number) {
    if (this.pausedUntil > this.clock())
      throw new DatabasePausedError(
        Math.max(1, Math.ceil((this.pausedUntil - this.clock()) / 1000)),
      );
    if (!Number.isSafeInteger(statements) || statements < 0)
      throw new DatabasePausedError();
    let reserved = false;
    const state = await this.update((state, time) => {
      if (
        state.blockedUntil > time ||
        state.requests >= this.limits.requests ||
        state.statements + statements > this.limits.statements
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
      throw new DatabasePausedError(
        Math.max(1, Math.ceil((this.pausedUntil - this.clock()) / 1000)),
      );
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
          state.lastFailure = {
            kind: "provider_quota",
            at: time,
            status: response.status,
          };
          const date = new Date(time);
          state.blockedUntil = Date.UTC(
            date.getUTCFullYear(),
            date.getUTCMonth() + 1,
            1,
          );
        } else if ([401, 403, 429].includes(response.status)) {
          state.lastFailure = {
            kind: response.status === 429 ? "rate_limit" : "authentication",
            at: time,
            status: response.status,
          };
          state.blockedUntil = Math.max(state.blockedUntil, time + 3600000);
        } else if (response.status >= 500) {
          state.lastFailure = {
            kind: "server_error",
            at: time,
            status: response.status,
          };
          this.failed(state, time);
        } else {
          if (state.failures === 0) return false;
          state.failures = 0;
        }
      });
      this.pausedUntil = state.blockedUntil;
      return response;
    } catch (error) {
      const state = await this.update((state, time) => {
        state.lastFailure = {
          kind:
            error instanceof Error &&
            /too many subrequests|subrequest.*limit/i.test(error.message)
              ? "worker_subrequest_limit"
              : "transport_error",
          at: time,
        };
        this.failed(state, time);
      });
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
