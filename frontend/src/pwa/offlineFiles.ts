import type { FilesQuery, FilesResponse } from "@/api/types";

const STORAGE_PREFIX = "frostvault.files.cache.v1:";

function cacheKey(query: FilesQuery): string {
  return [
    query.directory ?? "",
    query.q ?? "",
    query.state ?? "",
    String(query.page ?? 1),
    String(query.page_size ?? 100),
  ].join("|");
}

export type CachedFilesListing = {
  data: FilesResponse;
  savedAt: string;
};

/** Persist the last successful file listing for offline reopen. */
export function saveCachedFilesListing(
  query: FilesQuery,
  data: FilesResponse,
  storage: Storage = localStorage,
): void {
  const payload: CachedFilesListing = {
    data,
    savedAt: new Date().toISOString(),
  };
  storage.setItem(STORAGE_PREFIX + cacheKey(query), JSON.stringify(payload));
}

/** Load a previously cached listing, or null when missing/corrupt. */
export function loadCachedFilesListing(
  query: FilesQuery,
  storage: Storage = localStorage,
): CachedFilesListing | null {
  const raw = storage.getItem(STORAGE_PREFIX + cacheKey(query));
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as CachedFilesListing;
    if (!parsed?.data || !Array.isArray(parsed.data.items)) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function isBrowserOffline(): boolean {
  return typeof navigator !== "undefined" && navigator.onLine === false;
}
