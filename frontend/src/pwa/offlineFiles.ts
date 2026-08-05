import type { FilesQuery, FilesResponse } from "@/api/types";

const LEGACY_STORAGE_PREFIX = "frostvault.files.cache.v1:";
const STORAGE_PREFIX = "frostvault.files.cache.v2:";

/** Runtime cache names are scoped separately from the URL keys within them. */
export const OFFLINE_FILE_SERVICE_WORKER_CACHE_PREFIX =
  "frostvault-file-listing-v2:";
export const OFFLINE_FILE_CACHE_CONTEXT_MESSAGE =
  "frostvault.offline-file-cache-context";
export const CLEAR_OFFLINE_FILE_CACHE_MESSAGE =
  "frostvault.clear-offline-file-cache";
export const OFFLINE_FILE_CACHE_INVALIDATED_EVENT =
  "frostvault.offline-file-cache-invalidated";

/** The complete authorization scope required to read an offline file listing. */
export type OfflineCacheContext = Readonly<{
  userId: number;
  vaultId: number;
}>;

export type CachedFilesListing = {
  data: FilesResponse;
  savedAt: string;
};

type OfflineFileCacheMessage =
  | {
      type: typeof OFFLINE_FILE_CACHE_CONTEXT_MESSAGE;
      context: OfflineCacheContext;
    }
  | { type: typeof CLEAR_OFFLINE_FILE_CACHE_MESSAGE };

let serviceWorkerMessageGeneration = 0;

function isPositiveSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

export function isOfflineCacheContext(
  value: unknown,
): value is OfflineCacheContext {
  if (!value || typeof value !== "object") return false;
  const context = value as {
    userId?: unknown;
    vaultId?: unknown;
  };
  return (
    isPositiveSafeInteger(context.userId) &&
    isPositiveSafeInteger(context.vaultId)
  );
}

function cacheScope(context: OfflineCacheContext): string {
  return `user-${context.userId}:vault-${context.vaultId}`;
}

/** CacheStorage names must include both parts of the authorization context. */
export function offlineFileServiceWorkerCacheName(
  context: OfflineCacheContext,
): string {
  return `${OFFLINE_FILE_SERVICE_WORKER_CACHE_PREFIX}${cacheScope(context)}`;
}

function cacheKey(context: OfflineCacheContext, query: FilesQuery): string {
  return [
    cacheScope(context),
    query.directory ?? "",
    query.q ?? "",
    query.state ?? "",
    String(query.page ?? 1),
    String(query.page_size ?? 100),
  ]
    .map((part) => encodeURIComponent(part))
    .join("|");
}

function removeEntriesWithPrefixes(storage: Storage, prefixes: string[]): void {
  for (let index = storage.length - 1; index >= 0; index -= 1) {
    const key = storage.key(index);
    if (key && prefixes.some((prefix) => key.startsWith(prefix))) {
      storage.removeItem(key);
    }
  }
}

/** Remove v1 URL/query-only entries before they can be considered for offline UI. */
export function invalidateLegacyCachedFilesListings(
  storage: Storage = localStorage,
): void {
  removeEntriesWithPrefixes(storage, [LEGACY_STORAGE_PREFIX]);
}

/** Remove every persisted file listing without touching unrelated application state. */
export function clearCachedFilesListings(
  storage: Storage = localStorage,
): void {
  removeEntriesWithPrefixes(storage, [LEGACY_STORAGE_PREFIX, STORAGE_PREFIX]);
}

function postServiceWorkerMessage(message: OfflineFileCacheMessage): void {
  if (
    typeof navigator === "undefined" ||
    !("serviceWorker" in navigator) ||
    !navigator.serviceWorker
  ) {
    return;
  }

  const generation = ++serviceWorkerMessageGeneration;
  const serviceWorker = navigator.serviceWorker;
  serviceWorker.controller?.postMessage(message);
  void serviceWorker.ready
    .then((registration) => {
      // A later logout or Vault switch must not be overtaken by a delayed ready
      // notification for the prior context.
      if (generation !== serviceWorkerMessageGeneration) return;
      registration.active?.postMessage(message);
    })
    .catch(() => undefined);
}

/** Tell the active worker which authenticated file-listing scope may be cached. */
export function setOfflineFileCacheContext(context: OfflineCacheContext): void {
  if (!isOfflineCacheContext(context)) return;
  invalidateLegacyCachedFilesListings();
  postServiceWorkerMessage({
    type: OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
    context,
  });
}

function notifyOfflineFileCacheInvalidated(): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(OFFLINE_FILE_CACHE_INVALIDATED_EVENT));
}

/** Subscribe to local authorization transitions before a fresh /api/me restores data. */
export function subscribeToOfflineFileCacheInvalidation(
  listener: () => void,
): () => void {
  if (typeof window === "undefined") return () => undefined;
  window.addEventListener(OFFLINE_FILE_CACHE_INVALIDATED_EVENT, listener);
  return () => window.removeEventListener(OFFLINE_FILE_CACHE_INVALIDATED_EVENT, listener);
}

/** Purge file listings locally and in the service worker on logout or scope changes. */
export function clearOfflineFileCache(storage: Storage = localStorage): void {
  clearCachedFilesListings(storage);
  notifyOfflineFileCacheInvalidated();
  postServiceWorkerMessage({ type: CLEAR_OFFLINE_FILE_CACHE_MESSAGE });
}

/**
 * Start an authorization transition before an API operation changes the active
 * Vault. The caller may restore a context only after a fresh /api/me response.
 */
export function runWithOfflineFileCacheBarrier<T>(
  operation: () => Promise<T>,
): Promise<T> {
  clearOfflineFileCache();
  return operation();
}

/** Persist the last successful file listing for this authorization scope. */
export function saveCachedFilesListing(
  context: OfflineCacheContext,
  query: FilesQuery,
  data: FilesResponse,
  storage: Storage = localStorage,
): void {
  if (!isOfflineCacheContext(context)) return;
  invalidateLegacyCachedFilesListings(storage);
  const payload: CachedFilesListing = {
    data,
    savedAt: new Date().toISOString(),
  };
  storage.setItem(
    STORAGE_PREFIX + cacheKey(context, query),
    JSON.stringify(payload),
  );
}

/** Load a listing only when it belongs to the current user and Vault. */
export function loadCachedFilesListing(
  context: OfflineCacheContext,
  query: FilesQuery,
  storage: Storage = localStorage,
): CachedFilesListing | null {
  if (!isOfflineCacheContext(context)) return null;
  invalidateLegacyCachedFilesListings(storage);
  const key = STORAGE_PREFIX + cacheKey(context, query);
  const raw = storage.getItem(key);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as CachedFilesListing;
    if (!parsed?.data || !Array.isArray(parsed.data.items)) return null;
    return parsed;
  } catch {
    storage.removeItem(key);
    return null;
  }
}

export function isBrowserOffline(): boolean {
  return typeof navigator !== "undefined" && navigator.onLine === false;
}
