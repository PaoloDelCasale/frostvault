import type { FilesQuery, FilesResponse } from "@/api/types";

const LEGACY_STORAGE_PREFIXES = [
  "frostvault.files.cache.v1:",
  "frostvault.files.cache.v2:",
];
const STORAGE_PREFIX = "frostvault.files.cache.v3:";
const OFFLINE_FILE_CACHE_BARRIER_STORAGE_KEY =
  "frostvault.offline-file-cache-barrier.v1";

/** Runtime cache names are scoped separately from the URL keys within them. */
export const OFFLINE_FILE_SERVICE_WORKER_CACHE_PREFIX =
  "frostvault-file-listing-v3:";
export const LEGACY_OFFLINE_FILE_SERVICE_WORKER_CACHE_PREFIX =
  "frostvault-file-listing-v2:";
export const OFFLINE_FILE_CACHE_CONTEXT_MESSAGE =
  "frostvault.offline-file-cache-context";
export const CLEAR_OFFLINE_FILE_CACHE_MESSAGE =
  "frostvault.clear-offline-file-cache";
export const OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE =
  "frostvault.offline-file-cache-begin-transition";
export const OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE =
  "frostvault.offline-file-cache-transition-ack";
export const OFFLINE_FILE_CACHE_FINISH_TRANSITION_MESSAGE =
  "frostvault.offline-file-cache-finish-transition";
export const OFFLINE_FILE_CACHE_GENERATION_REQUEST_MESSAGE =
  "frostvault.offline-file-cache-generation-request";
export const OFFLINE_FILE_CACHE_GENERATION_MESSAGE =
  "frostvault.offline-file-cache-generation";
export const OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE =
  "frostvault.offline-file-cache-context-ack";
export const OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE =
  "frostvault.offline-file-cache-invalidated";
export const OFFLINE_FILE_CACHE_INVALIDATED_EVENT =
  "frostvault.offline-file-cache-invalidated";

/** Identifies the Service Worker process/generation, not the authenticated Session. */
export const OFFLINE_FILE_CACHE_GENERATION_HEADER =
  "X-FrostVault-Offline-Cache-Generation";
/** Opaque server-validated Session/Vault generation attached to /api/files. */
export const OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER =
  "X-FrostVault-Offline-Cache-Authorization";

/** Compatibility names for messages emitted by the first #192 protocol. */
export const OFFLINE_FILE_CACHE_EPOCH_REQUEST_MESSAGE =
  OFFLINE_FILE_CACHE_GENERATION_REQUEST_MESSAGE;
export const OFFLINE_FILE_CACHE_EPOCH_MESSAGE =
  OFFLINE_FILE_CACHE_GENERATION_MESSAGE;
export const OFFLINE_FILE_CACHE_CLEAR_ACK_MESSAGE =
  OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE;

/** Every Worker process gets a new boot id, even when its counter restarts at zero. */
export type OfflineFileCacheGeneration = Readonly<{
  bootId: string;
  counter: number;
}>;

/**
 * The full authorization scope for an offline listing. authorizationGeneration
 * is produced by /api/me from the current server-side Session and Vault.
 */
export type OfflineCacheContext = Readonly<{
  userId: number;
  vaultId: number;
  authorizationGeneration: string;
}>;

/** A /api/me response can only authorize the exact Worker generation it observed. */
export type OfflineFileCacheFreshness = Readonly<{
  generation: OfflineFileCacheGeneration | null;
  clientGeneration: number;
  barrierRevision: string;
  barrierState: OfflineFileCacheBarrierState;
  workerClosed: boolean;
}>;

/** A current authorization lease prevents late UI effects from restoring old data. */
export type OfflineFileCacheLease = Readonly<{
  context: OfflineCacheContext;
  generation: OfflineFileCacheGeneration;
  clientGeneration: number;
}>;

/**
 * A capability held only by the page that deliberately closed the Worker before
 * an authentication or Vault mutation. A missing acknowledgement is represented
 * explicitly; it never means that every client was closed.
 */
export type OfflineFileCacheTransition = Readonly<{
  id: string;
  generation: OfflineFileCacheGeneration | null;
  workerAcknowledged: boolean;
}>;

export type OfflineFileCacheBarrierState = "closed" | "reconcile" | "open";

export type OfflineFileCacheInvalidation = Readonly<{
  generation: OfflineFileCacheGeneration | null;
  clientGeneration: number;
  state: "closed" | "reconcile" | "unknown";
}>;

export type CachedFilesListing = {
  data: FilesResponse;
  savedAt: string;
};

type CacheBarrier = Readonly<{
  version: 1;
  revision: string;
  state: OfflineFileCacheBarrierState;
  context: OfflineCacheContext | null;
  generation: OfflineFileCacheGeneration | null;
  transitionId: string | null;
}>;

type ContextMessage = {
  type: typeof OFFLINE_FILE_CACHE_CONTEXT_MESSAGE;
  requestId: string;
  generation: OfflineFileCacheGeneration;
  context: OfflineCacheContext;
  transitionId?: string;
};

type BeginTransitionMessage = {
  type: typeof OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE;
  requestId: string;
  transitionId: string;
};

type FinishTransitionMessage = {
  type: typeof OFFLINE_FILE_CACHE_FINISH_TRANSITION_MESSAGE;
  requestId: string;
  generation: OfflineFileCacheGeneration;
  transitionId: string;
};

type GenerationRequestMessage = {
  type: typeof OFFLINE_FILE_CACHE_GENERATION_REQUEST_MESSAGE;
  requestId: string;
};

type OutboundServiceWorkerMessage =
  | ContextMessage
  | BeginTransitionMessage
  | FinishTransitionMessage
  | GenerationRequestMessage;

type GenerationMessage = {
  type: typeof OFFLINE_FILE_CACHE_GENERATION_MESSAGE;
  requestId: string;
  generation: OfflineFileCacheGeneration;
  closed: boolean;
};

type ContextAcknowledgement = {
  type: typeof OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE;
  requestId: string;
  generation: OfflineFileCacheGeneration;
  accepted: boolean;
  closed: boolean;
  transitionComplete: boolean;
};

type TransitionAcknowledgement = {
  type: typeof OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE;
  requestId: string;
  generation: OfflineFileCacheGeneration;
  accepted: boolean;
  closed: boolean;
  transitionComplete: boolean;
};

type ServiceWorkerReply =
  | GenerationMessage
  | ContextAcknowledgement
  | TransitionAcknowledgement;

type PendingServiceWorkerReply = {
  expectedType: ServiceWorkerReply["type"];
  resolve: (reply: ServiceWorkerReply | null) => void;
  timeout: ReturnType<typeof setTimeout>;
};

/** A Worker reply must never stall a Session/Vault mutation indefinitely. */
export const OFFLINE_FILE_CACHE_REPLY_TIMEOUT_MS = 1_000;

const DEFAULT_BARRIER: CacheBarrier = {
  version: 1,
  revision: "initial",
  state: "closed",
  context: null,
  generation: null,
  transitionId: null,
};

let knownServiceWorkerGeneration: OfflineFileCacheGeneration | null = null;
let offlineFileCacheClientGeneration = 0;
let serviceWorkerMessageSequence = 0;
let serviceWorkerMessageContainer: ServiceWorkerContainer | null = null;
let serviceWorkerControllerChangeContainer: ServiceWorkerContainer | null = null;
let storageListenerInstalled = false;
let memoryBarrier: CacheBarrier | null = null;
const pendingServiceWorkerReplies = new Map<string, PendingServiceWorkerReply>();

function isPositiveSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

/** Epoch zero was valid in the pre-nonce protocol and remains accepted for migration parsing. */
export function isOfflineFileCacheEpoch(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isSafeInteger(value) &&
    value >= 0
  );
}

function isNonce(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 256;
}

function isAuthorizationGeneration(value: unknown): value is string {
  if (typeof value !== "string" || value.length === 0 || value.length > 256) {
    return false;
  }
  return Array.from(value).every((character) => {
    const code = character.charCodeAt(0);
    return code >= 32 && code !== 127;
  });
}

export function isOfflineFileCacheGeneration(
  value: unknown,
): value is OfflineFileCacheGeneration {
  if (!value || typeof value !== "object") return false;
  const generation = value as { bootId?: unknown; counter?: unknown };
  return isNonce(generation.bootId) && isOfflineFileCacheEpoch(generation.counter);
}

export function sameOfflineFileCacheGeneration(
  left: OfflineFileCacheGeneration | null | undefined,
  right: OfflineFileCacheGeneration | null | undefined,
): boolean {
  if (!left || !right) return false;
  return left.bootId === right.bootId && left.counter === right.counter;
}

/** Stable header/cache suffix representation; boot id prevents restart collisions. */
export function offlineFileCacheGenerationKey(
  generation: OfflineFileCacheGeneration,
): string {
  return `${encodeURIComponent(generation.bootId)}.${generation.counter}`;
}

export function isOfflineCacheContext(
  value: unknown,
): value is OfflineCacheContext {
  if (!value || typeof value !== "object") return false;
  const context = value as {
    userId?: unknown;
    vaultId?: unknown;
    authorizationGeneration?: unknown;
  };
  return (
    isPositiveSafeInteger(context.userId) &&
    isPositiveSafeInteger(context.vaultId) &&
    isAuthorizationGeneration(context.authorizationGeneration)
  );
}

function sameOfflineCacheContext(
  left: OfflineCacheContext | null | undefined,
  right: OfflineCacheContext | null | undefined,
): boolean {
  if (!left || !right) return false;
  return (
    left.userId === right.userId &&
    left.vaultId === right.vaultId &&
    left.authorizationGeneration === right.authorizationGeneration
  );
}

function cacheScope(context: OfflineCacheContext): string {
  return `user-${context.userId}:vault-${context.vaultId}:authorization-${encodeURIComponent(
    context.authorizationGeneration,
  )}`;
}

/** CacheStorage names include User, Vault, server authorization, and Worker generation. */
export function offlineFileServiceWorkerCacheName(
  context: OfflineCacheContext,
  generation?: OfflineFileCacheGeneration,
): string {
  const base = `${OFFLINE_FILE_SERVICE_WORKER_CACHE_PREFIX}${cacheScope(context)}`;
  return generation ? `${base}:generation-${offlineFileCacheGenerationKey(generation)}` : base;
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
  try {
    for (let index = storage.length - 1; index >= 0; index -= 1) {
      const key = storage.key(index);
      if (key && prefixes.some((prefix) => key.startsWith(prefix))) {
        storage.removeItem(key);
      }
    }
  } catch {
    // Storage can be disabled by browser privacy settings. No cache is safer.
  }
}

/** Remove v1/v2 URL/query-only entries before they can be considered for offline UI. */
export function invalidateLegacyCachedFilesListings(
  storage: Storage = localStorage,
): void {
  removeEntriesWithPrefixes(storage, LEGACY_STORAGE_PREFIXES);
}

/** Remove every persisted file listing without touching unrelated application state. */
export function clearCachedFilesListings(
  storage: Storage = localStorage,
): void {
  removeEntriesWithPrefixes(storage, [...LEGACY_STORAGE_PREFIXES, STORAGE_PREFIX]);
}

function isCacheBarrier(value: unknown): value is CacheBarrier {
  if (!value || typeof value !== "object") return false;
  const barrier = value as Partial<CacheBarrier>;
  return (
    barrier.version === 1 &&
    isNonce(barrier.revision) &&
    (barrier.state === "closed" ||
      barrier.state === "reconcile" ||
      barrier.state === "open") &&
    (barrier.context === null || isOfflineCacheContext(barrier.context)) &&
    (barrier.generation === null ||
      isOfflineFileCacheGeneration(barrier.generation)) &&
    (barrier.transitionId === null || isNonce(barrier.transitionId))
  );
}

function readCacheBarrier(storage: Storage = localStorage): CacheBarrier {
  try {
    const raw = storage.getItem(OFFLINE_FILE_CACHE_BARRIER_STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as unknown;
      if (isCacheBarrier(parsed)) {
        memoryBarrier = parsed;
        return parsed;
      }
    }
    // A successful read of an absent/invalid marker is a fresh closed state;
    // do not resurrect this module's memory after the browser cleared storage.
    return DEFAULT_BARRIER;
  } catch {
    // With disabled storage, in-memory state still prevents this page from
    // restoring a delayed listing. Other pages cannot persist listings either.
    return memoryBarrier ?? DEFAULT_BARRIER;
  }
}

function writeCacheBarrier(
  barrier: CacheBarrier,
  storage: Storage = localStorage,
): void {
  memoryBarrier = barrier;
  try {
    storage.setItem(OFFLINE_FILE_CACHE_BARRIER_STORAGE_KEY, JSON.stringify(barrier));
  } catch {
    // The matching local listing write will also fail, so remaining network-only
    // is the conservative outcome.
  }
}

function createOpaqueId(): string {
  const values = new Uint32Array(4);
  try {
    const crypto = globalThis.crypto;
    if (!crypto?.getRandomValues) throw new Error("Web Crypto is unavailable");
    crypto.getRandomValues(values);
    return Array.from(values, (value) => value.toString(36)).join("-");
  } catch {
    // Browsers supporting Service Workers have Web Crypto. This fallback still
    // prevents accidental cross-tab capability collisions in degraded hosts.
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }
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

function nextInvalidation(
  generation: OfflineFileCacheGeneration | null,
  state: OfflineFileCacheInvalidation["state"],
): OfflineFileCacheInvalidation {
  offlineFileCacheClientGeneration += 1;
  return {
    generation,
    clientGeneration: offlineFileCacheClientGeneration,
    state,
  };
}

function closeCacheBarrier(
  options: {
    transitionId?: string | null;
    generation?: OfflineFileCacheGeneration | null;
    preserveTransition?: boolean;
    dispatch?: boolean;
    invalidationState?: OfflineFileCacheInvalidation["state"];
    storage?: Storage;
  } = {},
): OfflineFileCacheInvalidation {
  const storage = options.storage ?? localStorage;
  const previous = readCacheBarrier(storage);
  const transitionId = options.preserveTransition
    ? previous.transitionId
    : (options.transitionId ?? null);
  const hasGeneration = Object.prototype.hasOwnProperty.call(options, "generation");
  const generation = hasGeneration
    ? (options.generation ?? null)
    : (previous.generation ?? knownServiceWorkerGeneration);
  clearCachedFilesListings(storage);
  const barrier: CacheBarrier = {
    version: 1,
    revision: createOpaqueId(),
    state: "closed",
    context: null,
    generation,
    transitionId,
  };
  writeCacheBarrier(barrier, storage);
  const invalidation = nextInvalidation(
    generation,
    options.invalidationState ?? "closed",
  );
  if (options.dispatch !== false) dispatchOfflineFileCacheInvalidated(invalidation);
  return invalidation;
}

function reconcileCacheBarrier(
  generation: OfflineFileCacheGeneration,
  options: { dispatch?: boolean; storage?: Storage } = {},
): OfflineFileCacheInvalidation {
  const storage = options.storage ?? localStorage;
  clearCachedFilesListings(storage);
  writeCacheBarrier(
    {
      version: 1,
      revision: createOpaqueId(),
      state: "reconcile",
      context: null,
      generation,
      transitionId: null,
    },
    storage,
  );
  const invalidation = nextInvalidation(generation, "reconcile");
  if (options.dispatch !== false) dispatchOfflineFileCacheInvalidated(invalidation);
  return invalidation;
}

function openCacheBarrier(
  context: OfflineCacheContext,
  generation: OfflineFileCacheGeneration,
  preserveCachedListings = false,
): void {
  if (!preserveCachedListings) clearCachedFilesListings();
  writeCacheBarrier({
    version: 1,
    revision: createOpaqueId(),
    state: "open",
    context,
    generation,
    transitionId: null,
  });
}

/**
 * A Worker boot/counter change invalidates every in-memory lease. A boot change
 * also loses its transition capability, so the page must explicitly begin a
 * new transition rather than allowing an old completion to reopen caching.
 */
function observeServiceWorkerGeneration(
  generation: OfflineFileCacheGeneration,
  workerClosed: boolean,
): void {
  const barrier = readCacheBarrier();
  const previous = barrier.generation;
  const bootChanged = Boolean(previous && previous.bootId !== generation.bootId);
  const generationChanged = Boolean(
    previous && !sameOfflineFileCacheGeneration(previous, generation),
  );
  knownServiceWorkerGeneration = generation;

  if (workerClosed) {
    closeCacheBarrier({
      generation,
      preserveTransition: !bootChanged,
      dispatch: generationChanged || barrier.state !== "closed",
    });
    return;
  }

  if (bootChanged) {
    // A restarted process has no contexts and must never inherit a lost token.
    closeCacheBarrier({ generation, dispatch: true });
    return;
  }

  if (generationChanged) {
    if (barrier.state === "closed" && barrier.transitionId) {
      closeCacheBarrier({
        generation,
        preserveTransition: true,
        dispatch: false,
      });
    } else {
      reconcileCacheBarrier(generation);
    }
    return;
  }

  if (barrier.state === "closed" && barrier.transitionId) {
    // A timed-out begin can leave the Worker unknown/open. The initiating page
    // owns a locally durable closed barrier until a new explicit reconciliation.
    return;
  }
  if (barrier.state !== "open") {
    reconcileCacheBarrier(generation, { dispatch: false });
  }
}

function settlePendingServiceWorkerReplies(): void {
  for (const [requestId, pending] of pendingServiceWorkerReplies) {
    clearTimeout(pending.timeout);
    pendingServiceWorkerReplies.delete(requestId);
    pending.resolve(null);
  }
}

function handleServiceWorkerControllerChange(): void {
  settlePendingServiceWorkerReplies();
  knownServiceWorkerGeneration = null;
  // controllerchange is globally observable, but an ordinary Worker process
  // restart is not. Both start from a durable local closed barrier.
  closeCacheBarrier({ generation: null, invalidationState: "unknown" });
}

function isInvalidationMessage(
  value: unknown,
): value is {
  type: typeof OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE;
  generation: OfflineFileCacheGeneration;
  closed: boolean;
} {
  if (!value || typeof value !== "object") return false;
  const message = value as {
    type?: unknown;
    generation?: unknown;
    closed?: unknown;
  };
  return (
    message.type === OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE &&
    isOfflineFileCacheGeneration(message.generation) &&
    typeof message.closed === "boolean"
  );
}

function isRequestId(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isServiceWorkerReply(value: unknown): value is ServiceWorkerReply {
  if (!value || typeof value !== "object") return false;
  const message = value as {
    type?: unknown;
    requestId?: unknown;
    generation?: unknown;
    accepted?: unknown;
    closed?: unknown;
    transitionComplete?: unknown;
  };
  if (
    !isRequestId(message.requestId) ||
    !isOfflineFileCacheGeneration(message.generation) ||
    typeof message.closed !== "boolean"
  ) {
    return false;
  }
  if (message.type === OFFLINE_FILE_CACHE_GENERATION_MESSAGE) return true;
  return (
    (message.type === OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE ||
      message.type === OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE) &&
    typeof message.accepted === "boolean" &&
    typeof message.transitionComplete === "boolean"
  );
}

function isOlderThanKnownGeneration(
  generation: OfflineFileCacheGeneration,
): boolean {
  const known = knownServiceWorkerGeneration ?? readCacheBarrier().generation;
  return Boolean(
    known && generation.bootId === known.bootId && generation.counter < known.counter,
  );
}

function handleServiceWorkerMessage(event: MessageEvent<unknown>): void {
  const { data } = event;
  if (isInvalidationMessage(data)) {
    // Worker broadcasts are asynchronous. A delayed closed notification from
    // the first boot handshake must not tear down a lease that this same page
    // already completed in a later counter of the same Worker process.
    if (isOlderThanKnownGeneration(data.generation)) return;
    knownServiceWorkerGeneration = data.generation;
    // Do not overwrite the initiating page's shared barrier on an open
    // broadcast. Other tabs must refresh /api/me before they receive a lease;
    // their process-local generation below makes old leases unusable now.
    clearCachedFilesListings();
    dispatchOfflineFileCacheInvalidated(
      nextInvalidation(data.generation, data.closed ? "closed" : "reconcile"),
    );
    return;
  }
  if (!isServiceWorkerReply(data)) return;

  const pending = pendingServiceWorkerReplies.get(data.requestId);
  if (!pending || pending.expectedType !== data.type) return;
  clearTimeout(pending.timeout);
  pendingServiceWorkerReplies.delete(data.requestId);
  pending.resolve(data);
}

function handleCacheBarrierStorageEvent(event: StorageEvent): void {
  if (
    event.storageArea !== localStorage ||
    event.key !== OFFLINE_FILE_CACHE_BARRIER_STORAGE_KEY
  ) {
    return;
  }
  let next: CacheBarrier | null = null;
  try {
    const parsed = event.newValue ? (JSON.parse(event.newValue) as unknown) : null;
    if (isCacheBarrier(parsed)) next = parsed;
  } catch {
    // A malformed cross-tab marker is treated as a closure below.
  }
  knownServiceWorkerGeneration = null;
  clearCachedFilesListings();
  dispatchOfflineFileCacheInvalidated(
    nextInvalidation(
      next?.generation ?? null,
      next?.state === "open" || next?.state === "reconcile"
        ? "reconcile"
        : "closed",
    ),
  );
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

function ensureServiceWorkerMessageListener(): void {
  const container = serviceWorkerContainer();
  if (container && serviceWorkerMessageContainer !== container) {
    if (serviceWorkerMessageContainer) {
      serviceWorkerMessageContainer.removeEventListener(
        "message",
        handleServiceWorkerMessage,
      );
    }
    container.addEventListener("message", handleServiceWorkerMessage);
    serviceWorkerMessageContainer = container;
  }
  if (container && serviceWorkerControllerChangeContainer !== container) {
    if (serviceWorkerControllerChangeContainer) {
      serviceWorkerControllerChangeContainer.removeEventListener(
        "controllerchange",
        handleServiceWorkerControllerChange,
      );
    }
    container.addEventListener("controllerchange", handleServiceWorkerControllerChange);
    serviceWorkerControllerChangeContainer = container;
  }
  if (!storageListenerInstalled && typeof window !== "undefined") {
    window.addEventListener("storage", handleCacheBarrierStorageEvent);
    storageListenerInstalled = true;
  }
}

function nextServiceWorkerRequestId(): string {
  serviceWorkerMessageSequence += 1;
  return `offline-file-cache-${serviceWorkerMessageSequence}`;
}

function settleWithin<T>(
  promise: Promise<T>,
  timeoutMs = OFFLINE_FILE_CACHE_REPLY_TIMEOUT_MS,
): Promise<T | null> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value: T | null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      resolve(value);
    };
    const timeout = setTimeout(() => finish(null), timeoutMs);
    void promise.then(
      (value) => finish(value),
      () => finish(null),
    );
  });
}

async function activeServiceWorker(): Promise<ServiceWorker | null> {
  const container = serviceWorkerContainer();
  if (!container) return null;
  if (container.controller) return container.controller;
  const registration = await settleWithin(container.getRegistration());
  return registration?.active ?? null;
}

async function requestServiceWorkerReply(
  message: OutboundServiceWorkerMessage,
  expectedType: ServiceWorkerReply["type"],
): Promise<ServiceWorkerReply | null> {
  ensureServiceWorkerMessageListener();
  const worker = await activeServiceWorker();
  if (!worker) return null;

  return new Promise<ServiceWorkerReply | null>((resolve) => {
    const timeout = setTimeout(() => {
      const pending = pendingServiceWorkerReplies.get(message.requestId);
      if (!pending) return;
      pendingServiceWorkerReplies.delete(message.requestId);
      resolve(null);
    }, OFFLINE_FILE_CACHE_REPLY_TIMEOUT_MS);
    pendingServiceWorkerReplies.set(message.requestId, {
      expectedType,
      resolve,
      timeout,
    });
    try {
      worker.postMessage(message);
    } catch {
      clearTimeout(timeout);
      pendingServiceWorkerReplies.delete(message.requestId);
      resolve(null);
    }
  });
}

/**
 * Probe the Worker before a fresh /api/me response can grant a lease. The
 * persisted boot nonce catches ordinary process restarts which do not emit
 * activate/controllerchange events.
 */
export async function prepareOfflineFileCacheContext(): Promise<OfflineFileCacheFreshness> {
  const requestId = nextServiceWorkerRequestId();
  const reply = await requestServiceWorkerReply(
    { type: OFFLINE_FILE_CACHE_GENERATION_REQUEST_MESSAGE, requestId },
    OFFLINE_FILE_CACHE_GENERATION_MESSAGE,
  );
  if (reply?.type === OFFLINE_FILE_CACHE_GENERATION_MESSAGE) {
    observeServiceWorkerGeneration(reply.generation, reply.closed);
    const barrier = readCacheBarrier();
    return {
      generation: reply.generation,
      clientGeneration: offlineFileCacheClientGeneration,
      barrierRevision: barrier.revision,
      barrierState: barrier.state,
      workerClosed: reply.closed,
    };
  }

  // An absent, old, or terminated Worker cannot be trusted to retain a
  // context. This page purges immediately; the auth mutation still proceeds.
  knownServiceWorkerGeneration = null;
  const invalidation = closeCacheBarrier({ generation: null });
  const barrier = readCacheBarrier();
  return {
    generation: null,
    clientGeneration: invalidation.clientGeneration,
    barrierRevision: barrier.revision,
    barrierState: barrier.state,
    workerClosed: true,
  };
}

function freshnessIsCurrent(freshness: OfflineFileCacheFreshness): boolean {
  const barrier = readCacheBarrier();
  return (
    sameOfflineFileCacheGeneration(
      freshness.generation,
      knownServiceWorkerGeneration,
    ) &&
    freshness.clientGeneration === offlineFileCacheClientGeneration &&
    freshness.barrierRevision === barrier.revision
  );
}

/** Whether a fresh /api/me needs a deliberate close/reconcile before registration. */
export function offlineFileCacheFreshnessNeedsTransition(
  freshness: OfflineFileCacheFreshness,
): boolean {
  return Boolean(
    freshness.generation &&
      (freshness.workerClosed || freshness.barrierState === "closed"),
  );
}

/** A changed server Session/Vault generation must close before it can be trusted. */
export function offlineFileCacheContextNeedsTransition(
  context: OfflineCacheContext,
  freshness: OfflineFileCacheFreshness,
): boolean {
  if (!freshness.generation || !freshnessIsCurrent(freshness)) return false;
  const barrier = readCacheBarrier();
  return (
    barrier.state === "closed" ||
    (barrier.state === "open" && !sameOfflineCacheContext(barrier.context, context))
  );
}

/**
 * Close this browser's durable barrier before an auth/Vault mutation. The
 * returned acknowledgement says only whether the Worker heard us; a timeout
 * never claims global closure and never blocks the mutation itself.
 */
export async function beginOfflineFileCacheTransition(
  storage: Storage = localStorage,
): Promise<OfflineFileCacheTransition> {
  const id = createOpaqueId();
  closeCacheBarrier({ transitionId: id, generation: knownServiceWorkerGeneration, storage });
  const requestId = nextServiceWorkerRequestId();
  const reply = await requestServiceWorkerReply(
    {
      type: OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE,
      requestId,
      transitionId: id,
    },
    OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE,
  );
  if (reply?.type === OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE) {
    observeServiceWorkerGeneration(reply.generation, reply.closed);
    if (reply.accepted && reply.closed) {
      return {
        id,
        generation: reply.generation,
        workerAcknowledged: true,
      };
    }
  }
  return { id, generation: null, workerAcknowledged: false };
}

function transitionCanComplete(
  transition: OfflineFileCacheTransition,
  freshness: OfflineFileCacheFreshness,
): boolean {
  return (
    !transition.generation ||
    sameOfflineFileCacheGeneration(transition.generation, freshness.generation)
  );
}

/** A restart/lost capability must be replaced by a new explicit transition. */
export function offlineFileCacheTransitionWasLost(
  transition: OfflineFileCacheTransition,
  freshness: OfflineFileCacheFreshness,
): boolean {
  return !transitionCanComplete(transition, freshness);
}

/**
 * Authorize the Worker only after a fresh /api/me response. The Worker will
 * reopen only when the owning transition capability is presented; normal
 * registrations require an already-open, matching durable barrier.
 */
export async function setOfflineFileCacheContext(
  context: OfflineCacheContext,
  freshness: OfflineFileCacheFreshness,
  transition?: OfflineFileCacheTransition,
): Promise<OfflineFileCacheLease | null> {
  if (
    !isOfflineCacheContext(context) ||
    !freshness.generation ||
    !freshnessIsCurrent(freshness) ||
    (transition && !transitionCanComplete(transition, freshness))
  ) {
    return null;
  }

  const barrier = readCacheBarrier();
  const allowed = transition
    ? barrier.state === "closed" && barrier.transitionId === transition.id
    : barrier.state === "reconcile" ||
      (barrier.state === "open" && sameOfflineCacheContext(barrier.context, context));
  if (!allowed) return null;

  invalidateLegacyCachedFilesListings();
  const requestId = nextServiceWorkerRequestId();
  const reply = await requestServiceWorkerReply(
    {
      type: OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
      requestId,
      generation: freshness.generation,
      context,
      ...(transition ? { transitionId: transition.id } : {}),
    },
    OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
  );
  if (reply?.type !== OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE) {
    closeCacheBarrier({ generation: null });
    knownServiceWorkerGeneration = null;
    return null;
  }

  observeServiceWorkerGeneration(reply.generation, reply.closed);
  const currentBarrier = readCacheBarrier();
  const accepted =
    reply.accepted &&
    !reply.closed &&
    (transition
      ? reply.transitionComplete && currentBarrier.transitionId === transition.id
      : !reply.transitionComplete &&
        sameOfflineFileCacheGeneration(reply.generation, freshness.generation) &&
        (currentBarrier.state === "reconcile" ||
          sameOfflineCacheContext(currentBarrier.context, context)));
  if (!accepted) {
    closeCacheBarrier({ generation: reply.generation });
    return null;
  }

  knownServiceWorkerGeneration = reply.generation;
  openCacheBarrier(
    context,
    reply.generation,
    !transition &&
      currentBarrier.state === "open" &&
      sameOfflineCacheContext(currentBarrier.context, context) &&
      sameOfflineFileCacheGeneration(currentBarrier.generation, reply.generation),
  );
  return {
    context,
    generation: reply.generation,
    clientGeneration: offlineFileCacheClientGeneration,
  };
}

/**
 * Complete an authenticated transition without a Vault. This reopens the
 * Worker network-only and rotates its generation, leaving no cache context.
 */
export async function finishOfflineFileCacheTransition(
  transition: OfflineFileCacheTransition,
  freshness: OfflineFileCacheFreshness,
): Promise<boolean> {
  if (
    !freshness.generation ||
    !freshnessIsCurrent(freshness) ||
    !transitionCanComplete(transition, freshness)
  ) {
    return false;
  }
  const barrier = readCacheBarrier();
  if (barrier.state !== "closed" || barrier.transitionId !== transition.id) {
    return false;
  }

  const requestId = nextServiceWorkerRequestId();
  const reply = await requestServiceWorkerReply(
    {
      type: OFFLINE_FILE_CACHE_FINISH_TRANSITION_MESSAGE,
      requestId,
      transitionId: transition.id,
      generation: freshness.generation,
    },
    OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE,
  );
  if (reply?.type !== OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE) {
    closeCacheBarrier({ generation: null });
    knownServiceWorkerGeneration = null;
    return false;
  }

  observeServiceWorkerGeneration(reply.generation, reply.closed);
  if (!reply.accepted || !reply.transitionComplete || reply.closed) {
    closeCacheBarrier({ generation: reply.generation });
    return false;
  }
  knownServiceWorkerGeneration = reply.generation;
  reconcileCacheBarrier(reply.generation, { dispatch: false });
  return true;
}

/** True only while this lease still represents the current local durable state. */
export function isOfflineFileCacheLeaseCurrent(
  lease: OfflineFileCacheLease,
  context: OfflineCacheContext,
): boolean {
  const barrier = readCacheBarrier();
  return (
    sameOfflineCacheContext(lease.context, context) &&
    barrier.state === "open" &&
    sameOfflineCacheContext(barrier.context, context) &&
    sameOfflineFileCacheGeneration(lease.generation, knownServiceWorkerGeneration) &&
    sameOfflineFileCacheGeneration(lease.generation, barrier.generation) &&
    lease.clientGeneration === offlineFileCacheClientGeneration
  );
}

/**
 * Re-probe a mounted page's lease before it resumes/focuses. This bounded
 * handshake catches a Worker process restart that emits neither activate nor
 * controllerchange, before the page is allowed to keep trusting local bytes.
 */
export async function verifyOfflineFileCacheLease(
  lease: OfflineFileCacheLease,
  context: OfflineCacheContext,
): Promise<boolean> {
  if (!isOfflineFileCacheLeaseCurrent(lease, context)) return false;
  const freshness = await prepareOfflineFileCacheContext();
  return Boolean(
    freshness.generation &&
      !freshness.workerClosed &&
      !offlineFileCacheFreshnessNeedsTransition(freshness) &&
      sameOfflineFileCacheGeneration(freshness.generation, lease.generation) &&
      isOfflineFileCacheLeaseCurrent(lease, context),
  );
}

/** A missing/stale lease sends /api/files through the Worker NetworkOnly path. */
export function offlineFileCacheRequestHeader(
  lease: OfflineFileCacheLease | null | undefined,
  context: OfflineCacheContext | null,
): string | null {
  if (!lease || !context || !isOfflineFileCacheLeaseCurrent(lease, context)) {
    return null;
  }
  return offlineFileCacheGenerationKey(lease.generation);
}

/** Both Worker and server must recognize the same current authorization lease. */
export function offlineFileCacheRequestHeaders(
  lease: OfflineFileCacheLease | null | undefined,
  context: OfflineCacheContext | null,
): Record<string, string> | null {
  const workerGeneration = offlineFileCacheRequestHeader(lease, context);
  if (!workerGeneration || !context) return null;
  return {
    [OFFLINE_FILE_CACHE_GENERATION_HEADER]: workerGeneration,
    [OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER]: context.authorizationGeneration,
  };
}

/** Subscribe every mounted App to Worker and cross-tab authorization transitions. */
export function subscribeToOfflineFileCacheInvalidation(
  listener: (invalidation: OfflineFileCacheInvalidation) => void,
): () => void {
  ensureServiceWorkerMessageListener();
  if (typeof window === "undefined") return () => undefined;
  const wrapped = (event: Event) => {
    const detail = (event as CustomEvent<OfflineFileCacheInvalidation>).detail;
    listener(
      detail ?? {
        generation: knownServiceWorkerGeneration,
        clientGeneration: offlineFileCacheClientGeneration,
        state: "unknown",
      },
    );
  };
  window.addEventListener(OFFLINE_FILE_CACHE_INVALIDATED_EVENT, wrapped);
  return () =>
    window.removeEventListener(OFFLINE_FILE_CACHE_INVALIDATED_EVENT, wrapped);
}

/** Legacy convenience API: local data is purged and a bounded close begins. */
export async function clearOfflineFileCache(
  storage: Storage = localStorage,
): Promise<OfflineFileCacheInvalidation> {
  await beginOfflineFileCacheTransition(storage);
  return {
    generation: knownServiceWorkerGeneration,
    clientGeneration: offlineFileCacheClientGeneration,
    state: "closed",
  };
}

function leaseAllowsCacheAccess(
  lease: OfflineFileCacheLease | undefined,
  context: OfflineCacheContext,
): boolean {
  // Undefined is retained only for isolated serialization/component test seams.
  // Production FileBrowser always passes null or a verified lease, and therefore
  // cannot use this path. Even this seam keys entries by the server generation.
  return lease === undefined || isOfflineFileCacheLeaseCurrent(lease, context);
}

/** Persist the last successful file listing for this exact authorization scope. */
export function saveCachedFilesListing(
  context: OfflineCacheContext,
  query: FilesQuery,
  data: FilesResponse,
  storage: Storage = localStorage,
  lease?: OfflineFileCacheLease,
): void {
  if (!isOfflineCacheContext(context) || !leaseAllowsCacheAccess(lease, context)) {
    return;
  }
  invalidateLegacyCachedFilesListings(storage);
  const payload: CachedFilesListing = {
    data,
    savedAt: new Date().toISOString(),
  };
  try {
    storage.setItem(
      STORAGE_PREFIX + cacheKey(context, query),
      JSON.stringify(payload),
    );
  } catch {
    // Quota/privacy failures simply leave this response network-only.
  }
}

/** Load a listing only when it belongs to the current User, Vault, and Session. */
export function loadCachedFilesListing(
  context: OfflineCacheContext,
  query: FilesQuery,
  storage: Storage = localStorage,
  lease?: OfflineFileCacheLease,
): CachedFilesListing | null {
  if (!isOfflineCacheContext(context) || !leaseAllowsCacheAccess(lease, context)) {
    return null;
  }
  invalidateLegacyCachedFilesListings(storage);
  const key = STORAGE_PREFIX + cacheKey(context, query);
  let raw: string | null;
  try {
    raw = storage.getItem(key);
  } catch {
    return null;
  }
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as CachedFilesListing;
    if (!parsed?.data || !Array.isArray(parsed.data.items)) return null;
    return parsed;
  } catch {
    try {
      storage.removeItem(key);
    } catch {
      // The malformed entry is already unusable.
    }
    return null;
  }
}

export function isBrowserOffline(): boolean {
  return typeof navigator !== "undefined" && navigator.onLine === false;
}
