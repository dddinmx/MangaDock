import { HttpError, now, type Env, type Identity } from "./index";
import { admin, all, one, run, list, groupName, type Row } from "./library";
export async function passwordHash(env: Env, id: string, password: unknown) {
  if (
    typeof password !== "string" ||
    password.length < 6 ||
    password.length > 512
  )
    throw new HttpError(400, "密码长度必须为 6–512 位");
  const response = await env.AUTH_SERVICE.get(
    env.AUTH_SERVICE.idFromName(id),
  ).fetch("https://auth.internal/hash", {
    method: "POST",
    body: JSON.stringify({ password, action: "hash" }),
  });
  if (!response.ok) throw new HttpError(400, "密码无效");
  return ((await response.json()) as Row).encoded as string;
}
export async function getUser(env: Env, id: string): Promise<Row> {
  let row = await one(
    env,
    "SELECT id,username,role,can_download,can_view_adult FROM users WHERE id=?",
    id,
  );
  if (!row && id === "1")
    row = await one(
      env,
      "SELECT id,username,role,can_download,can_view_adult FROM users WHERE id='admin'",
    );
  if (!row) throw new HttpError(404, "用户不存在");
  return { ...row, is_admin: row.role === "admin" };
}
export async function createUser(env: Env, user: Identity, data: Row) {
  admin(user);
  const username = String(data.username || "").trim();
  if (username.length < 3 || username.length > 80)
    throw new HttpError(400, "用户名长度必须为 3–80 位");
  if (data.confirm_password != null && data.password !== data.confirm_password)
    throw new HttpError(400, "两次输入的密码不一致");
  const role = data.role || "user";
  if (!["admin", "user"].includes(role)) throw new HttpError(400, "无效角色");
  if (await one(env, "SELECT id FROM users WHERE username=?", username))
    throw new HttpError(409, "用户名已存在");
  const id = String(Date.now() * 1000 + Math.floor(Math.random() * 1000)),
    encoded = await passwordHash(env, id, data.password);
  await run(
    env,
    "INSERT INTO users(id,username,password_hash,role) VALUES (?,?,?,?)",
    id,
    username,
    encoded,
    role,
  );
  if (role === "user")
    await saveGroups(env, id, list(data.group_names || data.groups));
  return getUser(env, id);
}
export async function saveGroups(env: Env, id: string, names: any[]) {
  const valid = await all(
      env,
      "SELECT name FROM library_groups WHERE kind='comic'",
    ),
    selected = [...new Set(names.map(groupName))].filter((name) =>
      valid.some((row) => row.name === name),
    );
  await env.DB.batch([
    env.DB.prepare("DELETE FROM group_permissions WHERE user_id=?").bind(id),
    ...selected.map((name) =>
      env.DB.prepare("INSERT INTO group_permissions VALUES (?,?)").bind(
        id,
        name,
      ),
    ),
  ]);
  return selected;
}
export async function changePassword(
  env: Env,
  user: Identity,
  data: Row,
  targetId?: string,
) {
  const target = targetId
    ? await getUser(env, targetId)
    : await getUser(env, user.user_id);
  if (targetId) admin(user);
  else {
    const existing = await one(
      env,
      "SELECT password_hash FROM users WHERE id=?",
      user.user_id,
    );
    const response = await env.AUTH_SERVICE.get(
      env.AUTH_SERVICE.idFromName(user.user_id),
    ).fetch("https://auth.internal/verify", {
      method: "POST",
      body: JSON.stringify({
        password: data.old_password || data.current_password,
        encoded: existing!.password_hash,
      }),
    });
    if (!response.ok || !((await response.json()) as Row).valid)
      throw new HttpError(400, "当前密码错误");
  }
  const next = data.password || data.new_password;
  if (next !== data.confirm_password)
    throw new HttpError(400, "两次输入的密码不一致");
  const encoded = await passwordHash(env, target.id, next);
  await env.DB.batch([
    env.DB.prepare("UPDATE users SET password_hash=? WHERE id=?").bind(
      encoded,
      target.id,
    ),
    env.DB.prepare("DELETE FROM sessions WHERE user_id=?").bind(target.id),
  ]);
}
export async function deleteUser(env: Env, user: Identity, id: string) {
  admin(user);
  const target = await getUser(env, id);
  if (target.id === user.user_id || target.role === "admin")
    throw new HttpError(400, "不能删除当前账号或管理员");
  await env.DB.batch([
    env.DB.prepare(
      "UPDATE comic_jobs SET state='cancelled' WHERE user_id=? AND state IN ('queued','running')",
    ).bind(target.id),
    env.DB.prepare(
      "UPDATE jobs SET state='cancelled' WHERE user_id=? AND state IN ('queued','running')",
    ).bind(target.id),
    ...[
      "comic_jobs",
      "jobs",
      "progress",
      "comic_progress",
      "reading_reports",
      "reading_time",
      "group_permissions",
      "hidden_items",
      "anilist_links",
      "anilist_accounts",
      "sessions",
    ].map((table) =>
      env.DB.prepare(`DELETE FROM ${table} WHERE user_id=?`).bind(target.id),
    ),
    env.DB.prepare("DELETE FROM login_logs WHERE username=?").bind(
      target.username,
    ),
    env.DB.prepare("DELETE FROM users WHERE id=?").bind(target.id),
  ]);
}
