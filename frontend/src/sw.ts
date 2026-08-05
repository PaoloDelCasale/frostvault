/// <reference lib="webworker" />
import { clientsClaim } from "workbox-core";
import { ExpirationPlugin } from "workbox-expiration";
import { cleanupOutdatedCaches, precacheAndRoute } from "workbox-precaching";
import { registerRoute } from "workbox-routing";
import { NetworkFirst, NetworkOnly } from "workbox-strategies";

import {
  CLEAR_OFFLINE_FILE_CACHE_MESSAGE,
  LEGACY_OFFLINE_FILE_SERVICE_WORKER_CACHE_PREFIX,
  OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER,
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

type ActiveTransition = Readonly<{
  id: string;
  ownerClientId: string | null;
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
const uncachedFileListingStrategy = new NetworkOnly();

const workerBootId = createWorkerBootId();
let fileListingGeneration: OfflineFileCacheGeneration = {
  bootId: workerBootId,
  counter: 0,
};
// A process restart does not preserve contexts or transition capabilities. It
// therefore starts closed and only a fresh page reconciliation can reopen it.
let barrierClosed = true;
let activeTransition: ActiveTransition | null = null;
let hasHandledFirstInteraction = false;

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

function isFileListingCache(cacheName: string): boolean {
  return (
    cacheName === LEGACY_FILE_LISTING_CACHE_NAME ||
    cacheName.startsWith(LEGACY_OFFLINE_FILE_SERVICE_WORKER_CACHE_PREFIX) ||
    cacheName.startsWith(OFFLINE_FILE_SERVICE_WORKER_CACHE_PREFIX)
  );
}

/** Delete only obsolete generations so a delayed old purge cannot erase new bytes. */
async function purgeObsoleteFileListingCaches(): Promise<void> {
  const currentSuffix = `:generation-${offlineFileCacheGenerationKey(
    fileListingGeneration,
  )}`;
  const cacheNames = await caches.keys();
  await Promise.all(
    cacheNames
      .filter(
        (cacheName) =>
          isFileListingCache(cacheName) && !cacheName.endsWith(currentSuffix),
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
  const boundedPurge = settleWithin(purgeObsoleteFileListingCaches());
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
  barrierClosed = closed;
  fileListingContexts.clear();
  fileListingStrategies.clear();
  schedulePurge(event);
  notifyBounded(event, generation, closed, excludedClientId);
  return generation;
}

/**
 * An ordinary process restart has no activate/controllerchange signal. On its
 * first message it announces a closed, new boot generation to every page.
 */
function ensureFirstInteraction(event: ExtendableMessageEvent): void {
  if (hasHandledFirstInteraction) return;
  hasHandledFirstInteraction = true;
  activeTransition = null;
  globallyInvalidate(event, true);
}

function cacheContextFor(
  event: ExtendableEvent,
  request: Request,
): ClientFileListingContext | null {
  if (barrierClosed) return null;
  const clientId = (event as FetchEvent).clientId;
  if (!clientId) return null;
  const authorization = fileListingContexts.get(clientId) ?? null;
  if (!authorization) return null;
  if (
    !sameOfflineFileCacheGeneration(
      authorization.generation,
      fileListingGeneration,
    )
  ) {
    return null;
  }
  if (
    request.headers.get(OFFLINE_FILE_CACHE_GENERATION_HEADER) !==
      offlineFileCacheGenerationKey(authorization.generation) ||
    request.headers.get(OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER) !==
      authorization.context.authorizationGeneration
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
  return !barrierClosed && sameOfflineFileCacheGeneration(generation, fileListingGeneration);
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
          // that began before a transition can never recreate an old cache; a
          // server response must also prove it still belongs to this Session.
          if (
            !cacheWritesAreAllowed(generation) ||
            response.headers.get(OFFLINE_FILE_CACHE_AUTHORIZATION_HEADER) !==
              context.authorizationGeneration
          ) {
            return null;
          }
          return response.status === 200 ? response : null;
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
    closed: barrierClosed,
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
    closed: barrierClosed,
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
    closed: barrierClosed,
    transitionComplete,
  });
}

function beginTransition(
  event: ExtendableMessageEvent,
  message: BeginTransitionMessage,
): void {
  ensureFirstInteraction(event);
  const ownerClientId = clientIdFromMessage(event);
  if (
    !activeTransition ||
    activeTransition.id !== message.transitionId ||
    activeTransition.ownerClientId !== ownerClientId
  ) {
    // A newer explicit close supersedes an interrupted/lost transition. It is
    // the only way to recover caching after a tab dies while the barrier is
    // closed; no timer or restart can silently reopen it.
    activeTransition = { id: message.transitionId, ownerClientId };
    globallyInvalidate(event, true, ownerClientId);
  }
  // ACK after synchronous state closure but before asynchronous cleanup.
  postTransitionAcknowledgement(event, message.requestId, true, false);
}

function transitionBelongsTo(
  event: ExtendableMessageEvent,
  transitionId: string,
): boolean {
  const ownerClientId = clientIdFromMessage(event);
  return Boolean(
    activeTransition &&
      activeTransition.id === transitionId &&
      activeTransition.ownerClientId === ownerClientId,
  );
}

function completeTransitionWithContext(
  event: ExtendableMessageEvent,
  message: ContextMessage,
): void {
  ensureFirstInteraction(event);
  const clientId = clientIdFromMessage(event);
  let accepted = false;
  let transitionComplete = false;

  if (
    clientId &&
    sameOfflineFileCacheGeneration(message.generation, fileListingGeneration)
  ) {
    if (!barrierClosed && !message.transitionId) {
      fileListingContexts.set(clientId, {
        context: message.context,
        generation: fileListingGeneration,
      });
      accepted = true;
    } else if (
      barrierClosed &&
      message.transitionId &&
      transitionBelongsTo(event, message.transitionId)
    ) {
      activeTransition = null;
      const generation = globallyInvalidate(event, false, clientId);
      fileListingContexts.set(clientId, {
        context: message.context,
        generation,
      });
      accepted = true;
      transitionComplete = true;
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
  ensureFirstInteraction(event);
  let accepted = false;
  let transitionComplete = false;
  if (
    barrierClosed &&
    sameOfflineFileCacheGeneration(message.generation, fileListingGeneration) &&
    transitionBelongsTo(event, message.transitionId)
  ) {
    activeTransition = null;
    globallyInvalidate(event, false, clientIdFromMessage(event));
    accepted = true;
    transitionComplete = true;
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

self.addEventListener("activate", (event) => {
  // Keep the new process closed. Its first page handshake broadcasts the boot
  // nonce and closure; activate/controllerchange are only one restart path.
  schedulePurge(event);
});

self.addEventListener("message", (event) => {
  if (isGenerationRequestMessage(event.data)) {
    ensureFirstInteraction(event);
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
