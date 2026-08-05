/// <reference lib="webworker" />
import { clientsClaim } from "workbox-core";
import { ExpirationPlugin } from "workbox-expiration";
import { cleanupOutdatedCaches, precacheAndRoute } from "workbox-precaching";
import { registerRoute } from "workbox-routing";
import { NetworkFirst, NetworkOnly } from "workbox-strategies";

import {
  CLEAR_OFFLINE_FILE_CACHE_MESSAGE,
  OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE,
  OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
  OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
  OFFLINE_FILE_CACHE_FINISH_TRANSITION_MESSAGE,
  OFFLINE_FILE_CACHE_GENERATION_HEADER,
  OFFLINE_FILE_CACHE_GENERATION_MESSAGE,
  OFFLINE_FILE_CACHE_GENERATION_REQUEST_MESSAGE,
  OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE,
  OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE,
  OFFLINE_FILE_SERVICE_WORKER_CACHE_PREFIX,
  isOfflineCacheContext,
  isOfflineFileCacheGeneration,
  offlineFileCacheGenerationKey,
  offlineFileServiceWorkerCacheName,
  sameOfflineFileCacheGeneration,
  type OfflineCacheContext,
  type OfflineFileCacheGeneration,
} from "./pwa/offlineFiles";

declare let self: ServiceWorkerGlobalScope;

const LEGACY_FILE_LISTING_CACHE_NAME = "frostvault-file-listing";
const WORKER_WAIT_TIMEOUT_MS = 1_000;

type ClientFileListingContext = Readonly<{
  context: OfflineCacheContext;
  generation: OfflineFileCacheGeneration;
}>;

type ContextMessage = Readonly<{
  type: typeof OFFLINE_FILE_CACHE_CONTEXT_MESSAGE;
  requestId: string;
  generation: OfflineFileCacheGeneration;
  context: OfflineCacheContext;
  transitionId?: string;
}>;

type BeginTransitionMessage = Readonly<{
  type: typeof OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE;
  requestId: string;
  transitionId: string;
}>;

type FinishTransitionMessage = Readonly<{
  type: typeof OFFLINE_FILE_CACHE_FINISH_TRANSITION_MESSAGE;
  requestId: string;
  generation: OfflineFileCacheGeneration;
  transitionId: string;
}>;

type GenerationRequestMessage = Readonly<{
  type: typeof OFFLINE_FILE_CACHE_GENERATION_REQUEST_MESSAGE;
  requestId: string;
}>;

const fileListingContexts = new Map<string, ClientFileListingContext>();
const fileListingStrategies = new Map<string, NetworkFirst>();
const activeTransitionIds = new Set<string>();
const uncachedFileListingStrategy = new NetworkOnly();

const workerBootId = createWorkerBootId();
let fileListingGeneration: OfflineFileCacheGeneration = {
  bootId: workerBootId,
  counter: 0,
};

function createWorkerBootId(): string {
  const values = new Uint32Array(4);
  try {
    self.crypto.getRandomValues(values);
    return Array.from(values, (value) => value.toString(36)).join("-");
  } catch {
    // Web Crypto is required by supported Service Worker hosts. The fallback
    // protects test/degraded hosts from an accidental counter-only collision.
    return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  }
}

function nextGeneration(): OfflineFileCacheGeneration {
  fileListingGeneration = {
    bootId: workerBootId,
    counter: fileListingGeneration.counter + 1,
  };
  return fileListingGeneration;
}

function isRequestId(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function clientIdFromMessage(event: ExtendableMessageEvent): string | null {
  const source = event.source;
  if (!source || typeof source !== "object" || !("id" in source)) return null;
  const { id } = source as { id?: unknown };
  return typeof id === "string" && id ? id : null;
}

function postToMessageSource(
  event: ExtendableMessageEvent,
  message: Record<string, unknown>,
): void {
  const source = event.source;
  if (!source || typeof source !== "object" || !("postMessage" in source)) {
    return;
  }
  const postMessage = (source as { postMessage?: unknown }).postMessage;
  if (typeof postMessage === "function") {
    postMessage.call(source, message);
  }
}

function isContextMessage(value: unknown): value is ContextMessage {
  if (!value || typeof value !== "object") return false;
  const message = value as {
    type?: unknown;
    requestId?: unknown;
    generation?: unknown;
    context?: unknown;
    transitionId?: unknown;
  };
  return (
    message.type === OFFLINE_FILE_CACHE_CONTEXT_MESSAGE &&
    isRequestId(message.requestId) &&
    isOfflineFileCacheGeneration(message.generation) &&
    isOfflineCacheContext(message.context) &&
    (message.transitionId === undefined || isRequestId(message.transitionId))
  );
}

function isBeginTransitionMessage(value: unknown): value is BeginTransitionMessage {
  if (!value || typeof value !== "object") return false;
  const message = value as {
    type?: unknown;
    requestId?: unknown;
    transitionId?: unknown;
  };
  return (
    message.type === OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE &&
    isRequestId(message.requestId) &&
    isRequestId(message.transitionId)
  );
}

function isFinishTransitionMessage(value: unknown): value is FinishTransitionMessage {
  if (!value || typeof value !== "object") return false;
  const message = value as {
    type?: unknown;
    requestId?: unknown;
    generation?: unknown;
    transitionId?: unknown;
  };
  return (
    message.type === OFFLINE_FILE_CACHE_FINISH_TRANSITION_MESSAGE &&
    isRequestId(message.requestId) &&
    isOfflineFileCacheGeneration(message.generation) &&
    isRequestId(message.transitionId)
  );
}

function isGenerationRequestMessage(value: unknown): value is GenerationRequestMessage {
  if (!value || typeof value !== "object") return false;
  const message = value as { type?: unknown; requestId?: unknown };
  return (
    message.type === OFFLINE_FILE_CACHE_GENERATION_REQUEST_MESSAGE &&
    isRequestId(message.requestId)
  );
}

function isLegacyClearMessage(value: unknown): boolean {
  return Boolean(
    value &&
      typeof value === "object" &&
      (value as { type?: unknown }).type === CLEAR_OFFLINE_FILE_CACHE_MESSAGE,
  );
}

async function purgeFileListingCaches(): Promise<void> {
  const cacheNames = await caches.keys();
  await Promise.all(
    cacheNames
      .filter(
        (cacheName) =>
          cacheName === LEGACY_FILE_LISTING_CACHE_NAME ||
          cacheName.startsWith(OFFLINE_FILE_SERVICE_WORKER_CACHE_PREFIX),
      )
      .map((cacheName) => caches.delete(cacheName)),
  );
}

/** Never hold an event, ACK, or later transition on a hung CacheStorage task. */
function settleWithin<T>(
  promise: Promise<T>,
  timeoutMs = WORKER_WAIT_TIMEOUT_MS,
): Promise<void> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      resolve();
    };
    const timeout = setTimeout(finish, timeoutMs);
    void promise.then(finish, finish);
  });
}

function schedulePurge(event?: ExtendableEvent): void {
  const boundedPurge = settleWithin(purgeFileListingCaches());
  if (event) event.waitUntil(boundedPurge);
}

async function notifyWindowClients(
  generation: OfflineFileCacheGeneration,
  closed: boolean,
  excludedClientId?: string | null,
): Promise<void> {
  const windows = await self.clients.matchAll({
    type: "window",
    includeUncontrolled: true,
  });
  for (const client of windows) {
    if (client.id === excludedClientId) continue;
    client.postMessage({
      type: OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE,
      generation,
      closed,
    });
  }
}

function notifyBounded(
  event: ExtendableEvent | undefined,
  generation: OfflineFileCacheGeneration,
  closed: boolean,
  excludedClientId?: string | null,
): void {
  const notification = settleWithin(
    notifyWindowClients(generation, closed, excludedClientId),
  );
  if (event) event.waitUntil(notification);
}

function globallyInvalidate(
  event: ExtendableEvent | undefined,
  closed: boolean,
  excludedClientId?: string | null,
): OfflineFileCacheGeneration {
  const generation = nextGeneration();
  fileListingContexts.clear();
  fileListingStrategies.clear();
  schedulePurge(event);
  notifyBounded(event, generation, closed, excludedClientId);
  return generation;
}

function cacheContextFor(
  event: ExtendableEvent,
  request: Request,
): ClientFileListingContext | null {
  const clientId = (event as FetchEvent).clientId;
  if (!clientId) return null;
  const authorization = fileListingContexts.get(clientId) ?? null;
  if (!authorization) return null;
  if (!sameOfflineFileCacheGeneration(authorization.generation, fileListingGeneration)) {
    return null;
  }
  // This lease header lets a page fall back to NetworkOnly even if an old
  // Worker was too unhealthy to acknowledge the client's transition message.
  if (
    request.headers.get(OFFLINE_FILE_CACHE_GENERATION_HEADER) !==
    offlineFileCacheGenerationKey(authorization.generation)
  ) {
    return null;
  }
  return authorization;
}

function strategyKey(
  context: OfflineCacheContext,
  generation: OfflineFileCacheGeneration,
): string {
  return `${offlineFileCacheGenerationKey(generation)}:${offlineFileServiceWorkerCacheName(
    context,
    generation,
  )}`;
}

function cacheWritesAreAllowed(generation: OfflineFileCacheGeneration): boolean {
  return (
    activeTransitionIds.size === 0 &&
    sameOfflineFileCacheGeneration(generation, fileListingGeneration)
  );
}

function fileListingStrategyFor(
  context: OfflineCacheContext,
  generation: OfflineFileCacheGeneration,
): NetworkFirst {
  const key = strategyKey(context, generation);
  const existing = fileListingStrategies.get(key);
  if (existing) return existing;

  const strategy = new NetworkFirst({
    cacheName: offlineFileServiceWorkerCacheName(context, generation),
    networkTimeoutSeconds: 3,
    plugins: [
      {
        cacheWillUpdate: async ({ response }: { response: Response }) => {
          // Workbox invokes this immediately before CacheStorage.put. A response
          // that started before a transition can therefore never recreate an
          // old cache after its generation was closed/replaced.
          if (!cacheWritesAreAllowed(generation)) return null;
          return response.status === 200 || response.status === 0
            ? response
            : null;
        },
      },
      new ExpirationPlugin({
        maxEntries: 32,
        maxAgeSeconds: 7 * 24 * 60 * 60,
      }),
    ],
  });
  fileListingStrategies.set(key, strategy);
  return strategy;
}

function observeCacheCompletion(completion: Promise<unknown>): void {
  // Do not await an in-flight fetch while closing a transition. Its cache write
  // is independently generation-checked, and this bounded observer consumes a
  // late rejection without retaining a transition barrier forever.
  void settleWithin(completion);
}

async function handleFileListing({
  event,
  request,
}: {
  event: ExtendableEvent;
  request: Request;
}): Promise<Response> {
  const authorization = cacheContextFor(event, request);
  if (!authorization) {
    return uncachedFileListingStrategy.handle({ event, request });
  }

  const strategy = fileListingStrategyFor(
    authorization.context,
    authorization.generation,
  );
  const [response, completion] = strategy.handleAll({ event, request });
  observeCacheCompletion(completion);
  return response;
}

function postGeneration(
  event: ExtendableMessageEvent,
  requestId: string,
): void {
  postToMessageSource(event, {
    type: OFFLINE_FILE_CACHE_GENERATION_MESSAGE,
    requestId,
    generation: fileListingGeneration,
    closed: activeTransitionIds.size > 0,
  });
}

function postTransitionAcknowledgement(
  event: ExtendableMessageEvent,
  requestId: string,
  accepted: boolean,
  transitionComplete: boolean,
): void {
  postToMessageSource(event, {
    type: OFFLINE_FILE_CACHE_TRANSITION_ACK_MESSAGE,
    requestId,
    generation: fileListingGeneration,
    accepted,
    closed: activeTransitionIds.size > 0,
    transitionComplete,
  });
}

function postContextAcknowledgement(
  event: ExtendableMessageEvent,
  requestId: string,
  accepted: boolean,
  transitionComplete: boolean,
): void {
  postToMessageSource(event, {
    type: OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
    requestId,
    generation: fileListingGeneration,
    accepted,
    closed: activeTransitionIds.size > 0,
    transitionComplete,
  });
}

function beginTransition(
  event: ExtendableMessageEvent,
  message: BeginTransitionMessage,
): void {
  if (!activeTransitionIds.has(message.transitionId)) {
    activeTransitionIds.add(message.transitionId);
    globallyInvalidate(event, true);
  }
  // This ACK is deliberately sent after state closure but before asynchronous
  // cleanup. The Worker remains closed through the server mutation itself.
  postTransitionAcknowledgement(event, message.requestId, true, false);
}

function completeTransitionWithContext(
  event: ExtendableMessageEvent,
  message: ContextMessage,
): void {
  const clientId = clientIdFromMessage(event);
  let accepted = false;
  let transitionComplete = false;

  if (
    clientId &&
    sameOfflineFileCacheGeneration(message.generation, fileListingGeneration)
  ) {
    if (activeTransitionIds.size === 0 && !message.transitionId) {
      fileListingContexts.set(clientId, {
        context: message.context,
        generation: fileListingGeneration,
      });
      accepted = true;
    } else if (
      message.transitionId &&
      activeTransitionIds.has(message.transitionId)
    ) {
      activeTransitionIds.delete(message.transitionId);
      if (activeTransitionIds.size === 0) {
        // Rotate once more before reopening. Any /api/me response captured
        // while closed now has an unusable generation, including other tabs'.
        const generation = globallyInvalidate(event, false, clientId);
        fileListingContexts.set(clientId, {
          context: message.context,
          generation,
        });
        accepted = true;
        transitionComplete = true;
      }
    }
  }

  postContextAcknowledgement(
    event,
    message.requestId,
    accepted,
    transitionComplete,
  );
}

function finishTransition(
  event: ExtendableMessageEvent,
  message: FinishTransitionMessage,
): void {
  let accepted = false;
  let transitionComplete = false;
  if (
    sameOfflineFileCacheGeneration(message.generation, fileListingGeneration) &&
    activeTransitionIds.has(message.transitionId)
  ) {
    activeTransitionIds.delete(message.transitionId);
    if (activeTransitionIds.size === 0) {
      globallyInvalidate(event, false);
      accepted = true;
      transitionComplete = true;
    }
  }
  postTransitionAcknowledgement(
    event,
    message.requestId,
    accepted,
    transitionComplete,
  );
}

cleanupOutdatedCaches();
precacheAndRoute(self.__WB_MANIFEST);
void self.skipWaiting();
clientsClaim();

// An activate/update uses a fresh boot id and broadcasts a new generation.
// Existing pages clear their leases on both this message and controllerchange.
self.addEventListener("activate", (event) => {
  globallyInvalidate(event, false);
});

self.addEventListener("message", (event) => {
  if (isGenerationRequestMessage(event.data)) {
    postGeneration(event, event.data.requestId);
    return;
  }
  if (isBeginTransitionMessage(event.data)) {
    beginTransition(event, event.data);
    return;
  }
  if (isContextMessage(event.data)) {
    completeTransitionWithContext(event, event.data);
    return;
  }
  if (isFinishTransitionMessage(event.data)) {
    finishTransition(event, event.data);
    return;
  }
  if (isLegacyClearMessage(event.data)) {
    // A page running the prior protocol can only make the Worker safer: it
    // closes globally, but lacks a capability to reopen it.
    const requestId = `legacy-${Date.now()}-${Math.random()}`;
    beginTransition(event, {
      type: OFFLINE_FILE_CACHE_BEGIN_TRANSITION_MESSAGE,
      requestId,
      transitionId: requestId,
    });
  }
});

registerRoute(
  ({ request, url }) => request.method === "GET" && url.pathname === "/api/files",
  handleFileListing,
);

self.addEventListener("push", (event) => {
  let title = "FrostVault";
  let body = "";
  let data: Record<string, unknown> = {};
  try {
    const payload = event.data?.json() as {
      title?: string;
      body?: string;
      data?: Record<string, unknown>;
    };
    title = payload.title || title;
    body = payload.body || "";
    data = payload.data || {};
  } catch {
    body = event.data?.text() || "";
  }
  event.waitUntil(
    self.registration.showNotification(title, {
      body,
      data,
      icon: "/pwa-192.png",
      badge: "/pwa-192.png",
    }),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl =
    typeof event.notification.data?.url === "string"
      ? event.notification.data.url
      : "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ("focus" in client) {
          void client.navigate(targetUrl);
          return client.focus();
        }
      }
      return self.clients.openWindow(targetUrl);
    }),
  );
});
