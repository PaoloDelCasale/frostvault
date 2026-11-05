export function problemLink(vaultId: number | null, params: Record<string, unknown>): string {
  const query = new URLSearchParams();
  if (vaultId) query.set("vault_id", String(vaultId));
  const path = typeof params.path === "string" ? params.path : "";
  if (path) {
    query.set("directory", path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "");
    query.set("file", path);
  } else query.set("settings", "1");
  return vaultId ? `/?${query}` : "/general";
}
