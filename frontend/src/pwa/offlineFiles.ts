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
export const OFFLINE_FILE_CACHE_GENERATION_HEADER =
  "X-FrostVault-Offline-Cache-Generation";

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

/** The complete authorization scope required to read an offline file listing. */
export type OfflineCacheContext = Readonly<{
  userId: number;
  vaultId: number;
}>;

/** A /api/me response can only authorize the exact Worker generation it observed. */
export type OfflineFileCacheFreshness = Readonly<{
  generation: OfflineFileCacheGeneration | null;
  clientGeneration: number;
}>;

/** A current authorization lease prevents late UI effects from restoring old data. */
export type OfflineFileCacheLease = Readonly<{
  context: OfflineCacheContext;
  generation: OfflineFileCacheGeneration;
  clientGeneration: number;
}>;

/**
 * A capability held only by the tab which closed the Worker during a session or
 * Vault mutation. It is intentionally not included in invalidation broadcasts.
 */
export type OfflineFileCacheTransition = Readonly<{
  id: string;
}>;

export type OfflineFileCacheInvalidation = Readonly<{
  generation: OfflineFileCacheGeneration | null;
  clientGeneration: number;
  state: "closed" | "open" | "unknown";
}>;

export type CachedFilesListing = {
  data: FilesResponse;
  savedAt: string;
};

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

/** A Worker reply must never stall a session transition indefinitely. */
export const OFFLINE_FILE_CACHE_REPLY_TIMEOUT_MS = 1_000;

let knownServiceWorkerGeneration: OfflineFileCacheGeneration | null = null;
let offlineFileCacheClientGeneration = 0;
let serviceWorkerMessageSequence = 0;
let serviceWorkerMessageContainer: ServiceWorkerContainer | null = null;
let serviceWorkerControllerChangeContainer: ServiceWorkerContainer | null = null;
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
  };
  return (
    isPositiveSafeInteger(context.userId) &&
    isPositiveSafeInteger(context.vaultId)
  );
}

function cacheScope(context: OfflineCacheContext): string {
  return `user-${context.userId}:vault-${context.vaultId}`;
}

/** CacheStorage names include auth scope and the non-reusable Worker generation. */
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

function createTransitionId(): string {
  const values = new Uint32Array(4);
  try {
    const crypto = globalThis.crypto;
    if (!crypto?.getRandomValues) throw new Error("Web Crypto is unavailable");
    crypto.getRandomValues(values);
    return Array.from(values, (value) => value.toString(36)).join("-");
  } catch {
    // Browsers supporting Service Workers have Web Crypto. This fallback still
    // prevents accidental cross-tab token collisions in degraded test hosts.
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }
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
  generation: OfflineFileCacheGeneration | null,
  state: OfflineFileCacheInvalidation["state"],
  storage: Storage = localStorage,
  dispatch = true,
): OfflineFileCacheInvalidation {
  knownServiceWorkerGeneration = generation;
  offlineFileCacheClientGeneration += 1;
  clearCachedFilesListings(storage);
  const invalidation = {
    generation: knownServiceWorkerGeneration,
    clientGeneration: offlineFileCacheClientGeneration,
    state,
  };
  if (dispatch) dispatchOfflineFileCacheInvalidated(invalidation);
  return invalidation;
}

function observeServiceWorkerGeneration(
  generation: OfflineFileCacheGeneration,
  state: OfflineFileCacheInvalidation["state"],
): void {
  if (sameOfflineFileCacheGeneration(generation, knownServiceWorkerGeneration)) {
    return;
  }
  // A boot id change is a restart/update even if its numeric counter repeats.
  // This response was solicited by the current tab, so it need not trigger a
  // second concurrent App refresh; worker broadcasts/controllerchange do.
  invalidateOfflineFileCacheClient(generation, state, localStorage, false);
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
  // Controller changes are an invalidation even when the new Worker happens to
  // start at the same numeric counter. The following generation probe obtains
  // its fresh boot id before any context can be registered again.
  invalidateOfflineFileCacheClient(null, "open");
}

function handleServiceWorkerMessage(event: MessageEvent<unknown>): void {
  const { data } = event;
  if (isInvalidationMessage(data)) {
    // A transition notification is authoritative even if this tab already saw
    // the same generation through an acknowledgement.
    invalidateOfflineFileCacheClient(
      data.generation,
      data.closed ? "closed" : "open",
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

function ensureServiceWorkerMessageListener(): void {
  const container = serviceWorkerContainer();
  if (!container) return;
  if (serviceWorkerMessageContainer !== container) {
    if (serviceWorkerMessageContainer) {
      serviceWorkerMessageContainer.removeEventListener(
        "message",
        handleServiceWorkerMessage,
      );
    }
    container.addEventListener("message", handleServiceWorkerMessage);
    serviceWorkerMessageContainer = container;
  }
  if (serviceWorkerControllerChangeContainer !== container) {
    if (serviceWorkerControllerChangeContainer) {
      serviceWorkerControllerChangeContainer.removeEventListener(
        "controllerchange",
        handleServiceWorkerControllerChange,
      );
    }
    container.addEventListener("controllerchange", handleServiceWorkerControllerChange);
    serviceWorkerControllerChangeContainer = container;
  }
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
 * Capture the current Worker generation immediately before fetching /api/me.
 * A context may only be registered if this record remains current afterwards.
 */
export async function prepareOfflineFileCacheContext(): Promise<OfflineFileCacheFreshness> {
  const requestId = nextServiceWorkerRequestId();
  const reply = await requestServiceWorkerReply(
    { type: OFFLINE_FILE_CACHE_GENERATION_REQUEST_MESSAGE, requestId },
    OFFLINE_FILE_CACHE_GENERATION_MESSAGE,
  );
  if (reply?.type === OFFLINE_FILE_CACHE_GENERATION_MESSAGE) {
    observeServiceWorkerGeneration(reply.generation, reply.closed ? "closed" : "open");
  } else {
    // An absent, old, or terminated Worker cannot be trusted to retain a
    // context. Continue network-only after purging local data.
    invalidateOfflineFileCacheClient(null, "unknown");
  }
  return {
    generation: knownServiceWorkerGeneration,
    clientGeneration: offlineFileCacheClientGeneration,
  };
}

function freshnessIsCurrent(freshness: OfflineFileCacheFreshness): boolean {
  return (
    sameOfflineFileCacheGeneration(
      freshness.generation,
      knownServiceWorkerGeneration,
    ) && freshness.clientGeneration === offlineFileCacheClientGeneration
  );
}

/**
 * Close the global authorization barrier before an operation changes a Session
 * or selected Vault. The Worker acknowledges state closure, not cache cleanup;
 * generation-scoped cache names and write guards make a delayed cleanup safe.
 */
export async function beginOfflineFileCacheTransition(
  storage: Storage = localStorage,
): Promise<OfflineFileCacheTransition> {
  const transition = { id: createTransitionId() };
  invalidateOfflineFileCacheClient(
    knownServiceWorkerGeneration,
    "closed",
    storage,
  );
  const requestId = nextServiceWorkerRequestId();
  const reply = await requestServiceWorkerReply(
    {
      type: OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE,
      requestId,
      transitionId: transition.id,
    },
    OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE,
  );
  if (reply?.type === OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE) {
    observeServiceWorkerGeneration(reply.generation, "closed");
  }
  // A timed-out acknowledgement intentionally does not prevent the server
  // mutation. The random transition id lets a Worker that did receive begin
  // still accept the post-mutation completion; otherwise the client stays
  // network-only.
  return transition;
}

/**
 * Authorize the Worker only after a fresh /api/me response. During an active
 * transition only its initiating tab's opaque transition id may reopen it.
 */
export async function setOfflineFileCacheContext(
  context: OfflineCacheContext,
  freshness: OfflineFileCacheFreshness,
  transition?: OfflineFileCacheTransition,
): Promise<OfflineFileCacheLease | null> {
  if (
    !isOfflineCacheContext(context) ||
    !freshness.generation ||
    !freshnessIsCurrent(freshness)
  ) {
    return null;
  }
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
    invalidateOfflineFileCacheClient(null, "unknown");
    return null;
  }

  observeServiceWorkerGeneration(reply.generation, reply.closed ? "closed" : "open");
  if (!reply.accepted) return null;

  if (transition) {
    if (!reply.transitionComplete || reply.closed) return null;
  } else if (
    reply.transitionComplete ||
    !sameOfflineFileCacheGeneration(reply.generation, freshness.generation) ||
    !freshnessIsCurrent(freshness)
  ) {
    // A normal registration must not turn a response collected before a
    // transition/restart into authority for a later generation.
    return null;
  }

  const generation = knownServiceWorkerGeneration;
  if (!generation) return null;
  return {
    context,
    generation,
    clientGeneration: offlineFileCacheClientGeneration,
  };
}

/**
 * Complete a transition which has a fresh authoritative /api/me response but
 * no selected Vault. The Worker rotates again before reopening network-only,
 * making every closed-generation response unusable.
 */
export async function finishOfflineFileCacheTransition(
  transition: OfflineFileCacheTransition,
  freshness: OfflineFileCacheFreshness,
): Promise<boolean> {
  if (!freshness.generation || !freshnessIsCurrent(freshness)) return false;
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
    invalidateOfflineFileCacheClient(null, "unknown");
    return false;
  }
  observeServiceWorkerGeneration(reply.generation, reply.closed ? "closed" : "open");
  return reply.accepted && reply.transitionComplete && !reply.closed;
}

/** True only while this lease still represents the current local generation. */
export function isOfflineFileCacheLeaseCurrent(
  lease: OfflineFileCacheLease,
  context: OfflineCacheContext,
): boolean {
  return (
    lease.context.userId === context.userId &&
    lease.context.vaultId === context.vaultId &&
    sameOfflineFileCacheGeneration(
      lease.generation,
      knownServiceWorkerGeneration,
    ) &&
    lease.clientGeneration === offlineFileCacheClientGeneration
  );
}

/** A missing lease sends /api/files through the Worker NetworkOnly path. */
export function offlineFileCacheRequestHeader(
  lease: OfflineFileCacheLease | null | undefined,
  context: OfflineCacheContext | null,
): string | null {
  if (!lease || !context || !isOfflineFileCacheLeaseCurrent(lease, context)) {
    return null;
  }
  return offlineFileCacheGenerationKey(lease.generation);
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

/**
 * Legacy convenience API: local data is purged and a closed transition starts.
 * Callers that mutate session/Vault state must complete it with a post-mutation
 * /api/me response instead of treating this acknowledgement as an open barrier.
 */
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
  try {
    storage.setItem(
      STORAGE_PREFIX + cacheKey(context, query),
      JSON.stringify(payload),
    );
  } catch {
    // Quota/privacy failures simply leave this response network-only.
  }
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
