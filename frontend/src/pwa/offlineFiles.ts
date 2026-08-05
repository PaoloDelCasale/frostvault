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
export const OFFLINE_FILE_CACHE_EPOCH_REQUEST_MESSAGE =
  "frostvault.offline-file-cache-epoch-request";
export const OFFLINE_FILE_CACHE_EPOCH_MESSAGE =
  "frostvault.offline-file-cache-epoch";
export const OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE =
  "frostvault.offline-file-cache-context-ack";
export const OFFLINE_FILE_CACHE_CLEAR_ACK_MESSAGE =
  "frostvault.offline-file-cache-clear-ack";
export const OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE =
  "frostvault.offline-file-cache-invalidated";
export const OFFLINE_FILE_CACHE_INVALIDATED_EVENT =
  "frostvault.offline-file-cache-invalidated";

/** The complete authorization scope required to read an offline file listing. */
export type OfflineCacheContext = Readonly<{
  userId: number;
  vaultId: number;
}>;

/** A fresh /api/me request may use its captured epoch only once it still matches. */
export type OfflineFileCacheFreshness = Readonly<{
  epoch: number;
  generation: number;
}>;

/** A current authorization lease prevents late UI effects from restoring old data. */
export type OfflineFileCacheLease = Readonly<{
  context: OfflineCacheContext;
  epoch: number;
  generation: number;
}>;

export type OfflineFileCacheInvalidation = Readonly<{
  epoch: number;
  generation: number;
}>;

export type CachedFilesListing = {
  data: FilesResponse;
  savedAt: string;
};

type ContextMessage = {
  type: typeof OFFLINE_FILE_CACHE_CONTEXT_MESSAGE;
  requestId: string;
  epoch: number;
  context: OfflineCacheContext;
};

type ClearMessage = {
  type: typeof CLEAR_OFFLINE_FILE_CACHE_MESSAGE;
  requestId: string;
};

type EpochRequestMessage = {
  type: typeof OFFLINE_FILE_CACHE_EPOCH_REQUEST_MESSAGE;
  requestId: string;
};

type OutboundServiceWorkerMessage =
  | ContextMessage
  | ClearMessage
  | EpochRequestMessage;

type EpochMessage = {
  type: typeof OFFLINE_FILE_CACHE_EPOCH_MESSAGE;
  requestId: string;
  epoch: number;
};

type ContextAcknowledgement = {
  type: typeof OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE;
  requestId: string;
  epoch: number;
  accepted: boolean;
};

type ClearAcknowledgement = {
  type: typeof OFFLINE_FILE_CACHE_CLEAR_ACK_MESSAGE;
  requestId: string;
  epoch: number;
};

type ServiceWorkerReply =
  | EpochMessage
  | ContextAcknowledgement
  | ClearAcknowledgement;

type PendingServiceWorkerReply = {
  expectedType: ServiceWorkerReply["type"];
  resolve: (reply: ServiceWorkerReply) => void;
};

let knownServiceWorkerEpoch = 0;
let offlineFileCacheGeneration = 0;
let serviceWorkerMessageSequence = 0;
let serviceWorkerMessageContainer: ServiceWorkerContainer | null = null;
const pendingServiceWorkerReplies = new Map<string, PendingServiceWorkerReply>();

function isPositiveSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

/** Epoch zero is the initial Service Worker generation. */
export function isOfflineFileCacheEpoch(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= 0
  );
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

function serviceWorkerContainer(): ServiceWorkerContainer | null {
  if (
    typeof navigator === "undefined" ||
    !("serviceWorker" in navigator) ||
    !navigator.serviceWorker
  ) {
    return null;
  }
  return navigator.serviceWorker;
}

function nextServiceWorkerRequestId(): string {
  serviceWorkerMessageSequence += 1;
  return `offline-file-cache-${serviceWorkerMessageSequence}`;
}

function isRequestId(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isServiceWorkerReply(value: unknown): value is ServiceWorkerReply {
  if (!value || typeof value !== "object") return false;
  const message = value as {
    type?: unknown;
    requestId?: unknown;
    epoch?: unknown;
    accepted?: unknown;
  };
  if (
    !isRequestId(message.requestId) ||
    !isOfflineFileCacheEpoch(message.epoch)
  ) {
    return false;
  }
  if (message.type === OFFLINE_FILE_CACHE_EPOCH_MESSAGE) return true;
  if (message.type === OFFLINE_FILE_CACHE_CLEAR_ACK_MESSAGE) return true;
  return (
    message.type === OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE &&
    typeof message.accepted === "boolean"
  );
}

function isInvalidationMessage(
  value: unknown,
): value is { type: typeof OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE; epoch: number } {
  if (!value || typeof value !== "object") return false;
  const message = value as { type?: unknown; epoch?: unknown };
  return (
    message.type === OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE &&
    isOfflineFileCacheEpoch(message.epoch)
  );
}

function dispatchOfflineFileCacheInvalidated(
  invalidation: OfflineFileCacheInvalidation,
): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent<OfflineFileCacheInvalidation>(
      OFFLINE_FILE_CACHE_INVALIDATED_EVENT,
      { detail: invalidation },
    ),
  );
}

function invalidateOfflineFileCacheClient(
  epoch: number,
  storage: Storage = localStorage,
): OfflineFileCacheInvalidation {
  knownServiceWorkerEpoch = epoch;
  offlineFileCacheGeneration += 1;
  clearCachedFilesListings(storage);
  const invalidation = {
    epoch: knownServiceWorkerEpoch,
    generation: offlineFileCacheGeneration,
  };
  dispatchOfflineFileCacheInvalidated(invalidation);
  return invalidation;
}

function observeServiceWorkerEpoch(epoch: number): void {
  if (epoch === knownServiceWorkerEpoch) return;
  // A changed epoch means this page missed an invalidation or a Worker restart.
  // In either case, discard every local listing before a new /api/me can grant a
  // fresh lease for the Worker generation.
  invalidateOfflineFileCacheClient(epoch);
}

function handleServiceWorkerMessage(event: MessageEvent<unknown>): void {
  const { data } = event;
  if (isInvalidationMessage(data)) {
    // Always clear on a clear notification, even when the epoch is duplicated:
    // the payload is the authoritative cross-tab invalidation signal.
    invalidateOfflineFileCacheClient(data.epoch);
    return;
  }
  if (!isServiceWorkerReply(data)) return;

  const pending = pendingServiceWorkerReplies.get(data.requestId);
  if (!pending || pending.expectedType !== data.type) return;
  pendingServiceWorkerReplies.delete(data.requestId);
  pending.resolve(data);
}

function ensureServiceWorkerMessageListener(): void {
  const container = serviceWorkerContainer();
  if (!container || serviceWorkerMessageContainer === container) return;
  if (serviceWorkerMessageContainer) {
    serviceWorkerMessageContainer.removeEventListener(
      "message",
      handleServiceWorkerMessage,
    );
  }
  container.addEventListener("message", handleServiceWorkerMessage);
  serviceWorkerMessageContainer = container;
}

async function activeServiceWorker(): Promise<ServiceWorker | null> {
  const container = serviceWorkerContainer();
  if (!container) return null;
  if (container.controller) return container.controller;
  try {
    const registration = await container.getRegistration();
    return registration?.active ?? null;
  } catch {
    return null;
  }
}

async function requestServiceWorkerReply(
  message: OutboundServiceWorkerMessage,
  expectedType: ServiceWorkerReply["type"],
): Promise<ServiceWorkerReply | null> {
  ensureServiceWorkerMessageListener();
  const worker = await activeServiceWorker();
  if (!worker) return null;

  return new Promise<ServiceWorkerReply | null>((resolve) => {
    pendingServiceWorkerReplies.set(message.requestId, {
      expectedType,
      resolve,
    });
    try {
      worker.postMessage(message);
    } catch {
      pendingServiceWorkerReplies.delete(message.requestId);
      resolve(null);
    }
  });
}

/**
 * Capture the current Worker epoch immediately before fetching /api/me.
 * A context can be activated only with this freshness record, and only if no
 * invalidation changes its client generation before the response is applied.
 */
export async function prepareOfflineFileCacheContext(): Promise<OfflineFileCacheFreshness> {
  const requestId = nextServiceWorkerRequestId();
  const reply = await requestServiceWorkerReply(
    { type: OFFLINE_FILE_CACHE_EPOCH_REQUEST_MESSAGE, requestId },
    OFFLINE_FILE_CACHE_EPOCH_MESSAGE,
  );
  if (reply?.type === OFFLINE_FILE_CACHE_EPOCH_MESSAGE) {
    observeServiceWorkerEpoch(reply.epoch);
  }
  return {
    epoch: knownServiceWorkerEpoch,
    generation: offlineFileCacheGeneration,
  };
}

function freshnessIsCurrent(freshness: OfflineFileCacheFreshness): boolean {
  return (
    freshness.epoch === knownServiceWorkerEpoch &&
    freshness.generation === offlineFileCacheGeneration
  );
}

/**
 * Authorize the Worker cache only after a /api/me response collected with the
 * supplied current freshness record. A stale response cannot revive a context.
 */
export async function setOfflineFileCacheContext(
  context: OfflineCacheContext,
  freshness: OfflineFileCacheFreshness,
): Promise<OfflineFileCacheLease | null> {
  if (!isOfflineCacheContext(context) || !freshnessIsCurrent(freshness)) {
    return null;
  }
  invalidateLegacyCachedFilesListings();

  const requestId = nextServiceWorkerRequestId();
  const reply = await requestServiceWorkerReply(
    {
      type: OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
      requestId,
      epoch: freshness.epoch,
      context,
    },
    OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
  );
  if (reply?.type === OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE) {
    if (reply.epoch !== knownServiceWorkerEpoch) {
      observeServiceWorkerEpoch(reply.epoch);
      return null;
    }
    if (!reply.accepted || !freshnessIsCurrent(freshness)) return null;
  } else if (!freshnessIsCurrent(freshness)) {
    return null;
  }

  return {
    context,
    epoch: freshness.epoch,
    generation: freshness.generation,
  };
}

/** True only while this lease still represents the current local generation. */
export function isOfflineFileCacheLeaseCurrent(
  lease: OfflineFileCacheLease,
  context: OfflineCacheContext,
): boolean {
  return (
    lease.context.userId === context.userId &&
    lease.context.vaultId === context.vaultId &&
    lease.epoch === knownServiceWorkerEpoch &&
    lease.generation === offlineFileCacheGeneration
  );
}

/** Subscribe every mounted App to Worker-originated authorization transitions. */
export function subscribeToOfflineFileCacheInvalidation(
  listener: (invalidation: OfflineFileCacheInvalidation) => void,
): () => void {
  ensureServiceWorkerMessageListener();
  if (typeof window === "undefined") return () => undefined;
  const wrapped = (event: Event) => {
    const detail = (event as CustomEvent<OfflineFileCacheInvalidation>).detail;
    listener(
      detail ?? {
        epoch: knownServiceWorkerEpoch,
        generation: offlineFileCacheGeneration,
      },
    );
  };
  window.addEventListener(OFFLINE_FILE_CACHE_INVALIDATED_EVENT, wrapped);
  return () =>
    window.removeEventListener(OFFLINE_FILE_CACHE_INVALIDATED_EVENT, wrapped);
}

/**
 * Purge local listings and wait for the Service Worker's epoch acknowledgement.
 * The Worker acknowledges only after older-generation cache writes are unable
 * to recreate a listing cache.
 */
export async function clearOfflineFileCache(
  storage: Storage = localStorage,
): Promise<OfflineFileCacheInvalidation> {
  const localInvalidation = invalidateOfflineFileCacheClient(
    knownServiceWorkerEpoch,
    storage,
  );
  const requestId = nextServiceWorkerRequestId();
  const reply = await requestServiceWorkerReply(
    { type: CLEAR_OFFLINE_FILE_CACHE_MESSAGE, requestId },
    OFFLINE_FILE_CACHE_CLEAR_ACK_MESSAGE,
  );
  if (reply?.type !== OFFLINE_FILE_CACHE_CLEAR_ACK_MESSAGE) {
    return localInvalidation;
  }

  if (reply.epoch > knownServiceWorkerEpoch) {
    return invalidateOfflineFileCacheClient(reply.epoch, storage);
  }
  return {
    epoch: knownServiceWorkerEpoch,
    generation: offlineFileCacheGeneration,
  };
}

/**
 * Start an authorization transition before an API operation changes the active
 * Vault. The operation runs only after the cache-clear acknowledgement barrier.
 */
export async function runWithOfflineFileCacheBarrier<T>(
  operation: () => Promise<T>,
): Promise<T> {
  await clearOfflineFileCache();
  return operation();
}

/** Persist the last successful file listing for this authorization scope. */
export function saveCachedFilesListing(
  context: OfflineCacheContext,
  query: FilesQuery,
  data: FilesResponse,
  storage: Storage = localStorage,
  lease?: OfflineFileCacheLease,
): void {
  if (!isOfflineCacheContext(context)) return;
  if (lease && !isOfflineFileCacheLeaseCurrent(lease, context)) return;
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

/** Load a listing only when it belongs to the current User, Vault, and lease. */
export function loadCachedFilesListing(
  context: OfflineCacheContext,
  query: FilesQuery,
  storage: Storage = localStorage,
  lease?: OfflineFileCacheLease,
): CachedFilesListing | null {
  if (!isOfflineCacheContext(context)) return null;
  if (lease && !isOfflineFileCacheLeaseCurrent(lease, context)) return null;
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
