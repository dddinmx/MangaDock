const usernamePattern = /^md-verify-[a-f0-9]{8}-[a-f0-9]{3}$/;
const validId = (id) => /^\d{1,20}$/.test(String(id)) && Number(id) > 1;
function requireUsername(username) {
  if (!usernamePattern.test(username))
    throw new Error("Refusing to manage a non-validation account");
}
export async function createValidationReader(request, input) {
  requireUsername(input.username);
  const data = (await (await request("/api/v1/users", "POST", input)).json())
    .data;
  const user = data?.user;
  if (
    !user ||
    user.role !== "user" ||
    user.username !== input.username ||
    !validId(user.id) ||
    String(data.id) !== String(user.id) ||
    user.can_download ||
    user.can_view_adult
  )
    throw new Error(
      "Created validation account is not the expected ordinary reader",
    );
  return user;
}
export async function removeValidationReader(request, identity) {
  requireUsername(identity.username);
  const users = async () => {
    const items = (await (await request("/api/v1/users")).json()).data?.items;
    if (!Array.isArray(items))
      throw new Error("Cannot verify validation account cleanup");
    return items.filter((user) => user.username === identity.username);
  };
  const matches = await users();
  if (!matches.length) return;
  const user = matches[0];
  if (
    matches.length !== 1 ||
    user.role !== "user" ||
    !validId(user.id) ||
    (identity.id != null && String(identity.id) !== String(user.id))
  )
    throw new Error("Refusing to delete an unexpected account");
  // Mutations use the application's JSON contract, including bodyless-looking DELETEs.
  await request(`/api/v1/users/${user.id}`, "DELETE", {});
  if ((await users()).length)
    throw new Error("Validation account still exists after deletion");
}
export async function cleanFailedRunReader(request) {
  // Run 38659948800 created its isolated reader immediately before failing at
  // 02:13:58 UTC. IDs encode creation milliseconds; never sweep other dates/users.
  const start = Date.parse("2026-10-15T02:13:55Z"),
    end = Date.parse("2026-10-15T02:14:00Z");
  const items = (await (await request("/api/v1/users")).json()).data?.items;
  if (!Array.isArray(items))
    throw new Error("Cannot inspect failed-run reader cleanup");
  const candidates = items.filter(
    (user) =>
      usernamePattern.test(user.username) &&
      user.role === "user" &&
      validId(user.id) &&
      Math.floor(Number(user.id) / 1000) >= start &&
      Math.floor(Number(user.id) / 1000) < end,
  );
  if (candidates.length > 1)
    throw new Error("Failed-run reader cleanup is ambiguous");
  for (const user of candidates) await removeValidationReader(request, user);
  return candidates.length;
}
