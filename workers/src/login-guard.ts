import type { BudgetStorage } from "./database-budget";
import { hash, HttpError, type Env } from "./index";
// Local DO storage only: refused authentication must not touch Turso/D1.
type State = {
  day: string;
  attempts: number;
  window: number;
  users: Record<string, number>;
  ips: Record<string, number>;
  verified: Record<string, number>;
};
export class LoginGuard {
  constructor(
    private storage: BudgetStorage,
    private clock = Date.now,
  ) {}
  async check(username: string, ip: string, credential?: string) {
    const user = await hash(username),
      address = await hash(ip),
      valid = credential ? await hash(credential) : "";
    const allowed = await this.storage.transaction(async (tx) => {
      const time = this.clock(),
        day = new Date(time).toISOString().slice(0, 10),
        window = Math.floor(time / 900000);
      let state = await tx.get<State>("login-guard-v1");
      if (!state || state.day !== day)
        state = { day, attempts: 0, window, users: {}, ips: {}, verified: {} };
      if (state.window !== window) {
        state.window = window;
        state.users = {};
        state.ips = {};
      }
      if (valid && (state.verified[valid] || 0) > time) return true;
      if (
        state.attempts >= 1000 ||
        (state.users[user] || 0) >= 5 ||
        (state.ips[address] || 0) >= 30
      )
        return false;
      state.attempts++;
      state.users[user] = (state.users[user] || 0) + 1;
      state.ips[address] = (state.ips[address] || 0) + 1;
      state.verified = Object.fromEntries(
        Object.entries(state.verified).filter(([, until]) => until > time),
      );
      await tx.put("login-guard-v1", state);
      return true;
    });
    if (!allowed) throw new HttpError(429, "登录尝试过多，请稍后重试");
  }
  async success(username: string, credential?: string) {
    const user = await hash(username),
      valid = credential ? await hash(credential) : "";
    await this.storage.transaction(async (tx) => {
      const state = await tx.get<State>("login-guard-v1");
      if (!state) return;
      delete state.users[user];
      if (valid) state.verified[valid] = this.clock() + 300000;
      await tx.put("login-guard-v1", state);
    });
  }
}
export async function loginGuard(
  env: Env,
  action: "check" | "success",
  username: string,
  request: Request,
  credential?: string,
) {
  const ip = request.headers.get("CF-Connecting-IP") || "local";
  if (env.LOGIN_GUARD)
    return action === "check"
      ? env.LOGIN_GUARD.check(username, ip, credential)
      : env.LOGIN_GUARD.success(username, credential);
  const response = await env.APPLICATION.get(
    env.APPLICATION.idFromName("login"),
  ).fetch("https://login.internal/" + action, {
    method: "POST",
    body: JSON.stringify({ username, ip, credential }),
  });
  if (!response.ok)
    throw new HttpError(response.status, "登录尝试过多，请稍后重试");
}
