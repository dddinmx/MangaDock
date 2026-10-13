export function publicUser(row: {
  id?: string;
  user_id?: string;
  username: string;
  role: string;
  can_download?: number;
  can_view_adult?: number;
}) {
  const internal = row.id || row.user_id || "",
    id =
      internal === "admin"
        ? 1
        : /^\d+$/.test(internal)
          ? Number(internal)
          : internal;
  return {
    id,
    username: row.username,
    role: row.role,
    is_admin: row.role === "admin",
    can_download: !!row.can_download,
    can_view_adult: !!row.can_view_adult,
  };
}
