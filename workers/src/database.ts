import {
  createClient,
  type Client,
  type InValue,
  type ResultSet,
} from "@libsql/client/web";
import { DatabaseBudget } from "./database-budget.ts";

export type QueryMeta = {
  changes: number;
  last_row_id?: number;
  duration?: number;
  rows_read?: number;
  rows_written?: number;
};
export type QueryResult<T> = {
  results: T[];
  success: boolean;
  meta: QueryMeta;
};
export interface Statement {
  bind(...values: unknown[]): Statement;
  first<T = Record<string, unknown>>(columnName?: string): Promise<T | null>;
  all<T = Record<string, unknown>>(): Promise<QueryResult<T>>;
  run<T = Record<string, unknown>>(): Promise<QueryResult<T>>;
}
export interface Database {
  prepare(sql: string): Statement;
  batch<T = unknown>(statements: Statement[]): Promise<QueryResult<T>[]>;
}
export type DatabaseBindings = {
  DB?: Database;
  DATABASE_PROVIDER?: string;
  TURSO_DATABASE_URL?: string;
  TURSO_AUTH_TOKEN?: string;
};

function safeInteger(value: bigint | number) {
  const number = Number(value);
  if (!Number.isSafeInteger(number))
    throw new RangeError("数据库整数超出安全范围");
  return number;
}
function value(input: unknown): InValue {
  if (input === null || typeof input === "string" || typeof input === "boolean")
    return input;
  if (typeof input === "number" && Number.isFinite(input)) {
    if (Number.isInteger(input) && !Number.isSafeInteger(input))
      throw new RangeError("数据库参数整数超出安全范围");
    return input;
  }
  if (input instanceof ArrayBuffer || input instanceof Uint8Array) return input;
  if (typeof input === "bigint") return safeInteger(input);
  throw new TypeError("数据库参数类型无效");
}
function result<T>(set: ResultSet): QueryResult<T> {
  return {
    success: true,
    results: set.rows.map(
      (row) =>
        Object.fromEntries(
          set.columns.map((column, i) => {
            const field = row[i];
            return [
              column,
              field instanceof ArrayBuffer
                ? Array.from(new Uint8Array(field))
                : typeof field === "bigint"
                  ? safeInteger(field)
                  : field,
            ];
          }),
        ) as T,
    ),
    // The libSQL client does not expose scanned-row billing counters. Do not
    // invent rows_read/rows_written from returned rows or affected-row counts.
    meta: {
      changes: set.rowsAffected,
      ...(set.lastInsertRowid === undefined
        ? {}
        : { last_row_id: safeInteger(set.lastInsertRowid) }),
    },
  };
}
class TursoStatement implements Statement {
  db: TursoDatabase;
  sql: string;
  args: InValue[];
  constructor(db: TursoDatabase, sql: string, args: InValue[] = []) {
    this.db = db;
    this.sql = sql;
    this.args = args;
  }
  bind(...values: unknown[]) {
    return new TursoStatement(this.db, this.sql, values.map(value));
  }
  async first<T = Record<string, unknown>>(
    columnName?: string,
  ): Promise<T | null> {
    const set = await this.db.client.execute({
      sql: this.sql,
      args: this.args,
    });
    if (columnName !== undefined && !set.columns.includes(columnName))
      throw new Error("查询列不存在");
    const row = result<Record<string, unknown>>(set).results[0];
    return row
      ? ((columnName === undefined ? row : row[columnName]) as T)
      : null;
  }
  async all<T = Record<string, unknown>>() {
    return result<T>(
      await this.db.client.execute({ sql: this.sql, args: this.args }),
    );
  }
  async run<T = Record<string, unknown>>() {
    return this.all<T>();
  }
}
export class TursoDatabase implements Database {
  client: Client;
  constructor(client: Client) {
    this.client = client;
  }
  prepare(sql: string) {
    return new TursoStatement(this, sql);
  }
  async batch<T = unknown>(statements: Statement[]) {
    const inputs = statements.map((statement) => {
      if (!(statement instanceof TursoStatement) || statement.db !== this)
        throw new TypeError("事务不能混用不同数据库的语句");
      return { sql: statement.sql, args: statement.args };
    });
    if (!inputs.length) return [];
    // A real write transaction: a failed statement must roll back all prior
    // statements (sessions, reading-time deduplication and imports rely on it).
    return (await this.client.batch(inputs, "write")).map(result<T>);
  }
}
export function tursoUrl(raw: string) {
  const url = new URL(raw);
  if (
    !["libsql:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.port ||
    url.search ||
    url.hash ||
    (url.pathname !== "" && url.pathname !== "/") ||
    !url.hostname.endsWith(".turso.io")
  )
    throw new Error("请使用 Turso 控制台提供的 Database URL");
  return new URL(`https://${url.hostname}`).href;
}
const databases = new WeakMap<object, Database>();
export function databaseEnv<T extends DatabaseBindings>(
  env: T,
  budget?: DatabaseBudget,
): T & { DB: Database } {
  if (!env.DATABASE_PROVIDER || env.DATABASE_PROVIDER === "d1") {
    if (!env.DB) throw new Error("D1 数据库绑定缺失");
    return env as T & { DB: Database };
  }
  if (env.DATABASE_PROVIDER !== "turso") throw new Error("数据库类型无效");
  if (!env.TURSO_DATABASE_URL || !env.TURSO_AUTH_TOKEN)
    throw new Error("Turso 数据库 URL 或 Token 尚未配置");
  let db = budget ? undefined : databases.get(env);
  if (!db) {
    db = new TursoDatabase(
      createClient({
        url: tursoUrl(env.TURSO_DATABASE_URL),
        authToken: env.TURSO_AUTH_TOKEN,
        intMode: "number",
        concurrency: 4,
        fetch: async (input: RequestInfo, init: RequestInit = {}) => {
          const response = await (
            budget ? budget.guardedFetch.bind(budget) : fetch
          )(input, {
            ...init,
            // workerd only implements follow/manual. Never follow redirects
            // with the database's Authorization header.
            redirect: "manual",
            signal: AbortSignal.timeout(25000),
          });
          if (response.status >= 300 && response.status < 400) {
            await response.body?.cancel();
            throw new Error("Turso HTTP redirect refused");
          }
          return response;
        },
      }),
    );
    if (!budget) databases.set(env, db);
  }
  return { ...env, DB: db };
}
