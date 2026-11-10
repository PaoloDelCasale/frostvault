import { vaultFilesUrl, vaultSettingsUrl } from "@/router";

/** Where a problem or notification is remedied: the file, else the Vault settings. */
export function problemLink(vaultId: number | null, params: Record<string, unknown>): string {
  if (!vaultId) return "/system";
  const path = typeof params.path === "string" ? params.path : "";
  if (!path) return vaultSettingsUrl(vaultId);
  return vaultFilesUrl(vaultId, { directory: path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "", file: path });
}
