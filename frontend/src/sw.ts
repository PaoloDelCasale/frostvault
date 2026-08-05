/// <reference lib="webworker" />
import { clientsClaim } from "workbox-core";
import { ExpirationPlugin } from "workbox-expiration";
import { cleanupOutdatedCaches, precacheAndRoute } from "workbox-precaching";
import { registerRoute } from "workbox-routing";
import { NetworkFirst, NetworkOnly } from "workbox-strategies";

import {
  CLEAR_OFFLINE_FILE_CACHE_MESSAGE,
  OFFLINE_FILE_CACHE_CLEAR_ACK_MESSAGE,
  OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
  OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
  OFFLINE_FILE_CACHE_EPOCH_MESSAGE,
  OFFLINE_FILE_CACHE_EPOCH_REQUEST_MESSAGE,
  OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE,
  OFFLINE_FILE_SERVICE_WORKER_CACHE_PREFIX,
  isOfflineCacheContext,
  isOfflineFileCacheEpoch,
  offlineFileServiceWorkerCacheName,
  type OfflineCacheContext,
} from "./pwa/offlineFiles";

declare let self: ServiceWorkerGlobalScope;

const LEGACY_FILE_LISTING_CACHE_NAME = "frostvault-file-listing";

type ClientFileListingContext = Readonly<{
  context: OfflineCacheContext;
  epoch: number;
}>;

type ContextMessage = Readonly<{
  type: typeof OFFLINE_FILE_CACHE_CONTEXT_MESSAGE;
  requestId: string;
  epoch: number;
  context: OfflineCacheContext;
}>;

type ClearMessage = Readonly<{
  type: typeof CLEAR_OFFLINE_FILE_CACHE_MESSAGE;
  requestId?: string;
}>;

type EpochRequestMessage = Readonly<{
  type: typeof OFFLINE_FILE_CACHE_EPOCH_REQUEST_MESSAGE;
  requestId: string;
}>;

const fileListingContexts = new Map<string, ClientFileListingContext>();
const fileListingStrategies = new Map<string, NetworkFirst>();
const fileListingCompletions = new Map<number, Set<Promise<void>>>();
const uncachedFileListingStrategy = new NetworkOnly();

// This is authoritative while the Worker is alive. A context message must name
// the current epoch and wait for its clear barrier to finish before it can cache.
let fileListingEpoch = 0;
let readyFileListingEpoch = 0;
let clearFileListingBarrier: Promise<void> = Promise.resolve();

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

function cacheContextFor(event: ExtendableEvent): ClientFileListingContext | null {
  const clientId = (event as FetchEvent).clientId;
  if (!clientId) return null;
  const authorization = fileListingContexts.get(clientId) ?? null;
  if (!authorization || authorization.epoch !== fileListingEpoch) return null;
  return authorization;
}

function isContextMessage(value: unknown): value is ContextMessage {
  if (!value || typeof value !== "object") return false;
  const message = value as {
    type?: unknown;
    requestId?: unknown;
    epoch?: unknown;
    context?: unknown;
  };
  return (
    message.type === OFFLINE_FILE_CACHE_CONTEXT_MESSAGE &&
    isRequestId(message.requestId) &&
    isOfflineFileCacheEpoch(message.epoch) &&
    isOfflineCacheContext(message.context)
  );
}

function isClearMessage(value: unknown): value is ClearMessage {
  if (!value || typeof value !== "object") return false;
  const message = value as { type?: unknown; requestId?: unknown };
  return (
    message.type === CLEAR_OFFLINE_FILE_CACHE_MESSAGE &&
    (message.requestId === undefined || isRequestId(message.requestId))
  );
}

function isEpochRequestMessage(value: unknown): value is EpochRequestMessage {
  if (!value || typeof value !== "object") return false;
  const message = value as { type?: unknown; requestId?: unknown };
  return (
    message.type === OFFLINE_FILE_CACHE_EPOCH_REQUEST_MESSAGE &&
    isRequestId(message.requestId)
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

async function notifyWindowClients(epoch: number): Promise<void> {
  const windows = await self.clients.matchAll({
    type: "window",
    includeUncontrolled: true,
  });
  for (const client of windows) {
    client.postMessage({
      type: OFFLINE_FILE_CACHE_INVALIDATED_MESSAGE,
      epoch,
    });
  }
}

function strategyKey(context: OfflineCacheContext, epoch: number): string {
  return `${epoch}:${offlineFileServiceWorkerCacheName(context)}`;
}

function fileListingStrategyFor(
  context: OfflineCacheContext,
  epoch: number,
): NetworkFirst {
  const key = strategyKey(context, epoch);
  const existing = fileListingStrategies.get(key);
  if (existing) return existing;

  const strategy = new NetworkFirst({
    cacheName: offlineFileServiceWorkerCacheName(context),
    networkTimeoutSeconds: 3,
    plugins: [
      {
        cacheWillUpdate: async ({ response }: { response: Response }) => {
          // A response that started under an older epoch may still arrive after
          // a clear. Refuse its cache write before the clear barrier purges.
          if (epoch !== fileListingEpoch) return null;
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

function trackFileListingCompletion(
  epoch: number,
  completion: Promise<unknown>,
): void {
  const settled: Promise<void> = completion.then(
    () => undefined,
    () => undefined,
  );
  const pending = fileListingCompletions.get(epoch) ?? new Set<Promise<void>>();
  fileListingCompletions.set(epoch, pending);
  pending.add(settled);
  void settled.then(() => {
    pending.delete(settled);
    if (pending.size === 0) fileListingCompletions.delete(epoch);
  });
}

async function waitForOlderFileListingWrites(epoch: number): Promise<void> {
  while (true) {
    const pending = Array.from(fileListingCompletions.entries())
      .filter(([generation]) => generation < epoch)
      .flatMap(([, completions]) => Array.from(completions));
    if (pending.length === 0) return;
    await Promise.all(pending);
  }
}

async function handleFileListing({
  event,
  request,
}: {
  event: ExtendableEvent;
  request: Request;
}): Promise<Response> {
  const authorization = cacheContextFor(event);
  if (!authorization) {
    return uncachedFileListingStrategy.handle({ event, request });
  }

  const strategy = fileListingStrategyFor(
    authorization.context,
    authorization.epoch,
  );
  const [response, completion] = strategy.handleAll({ event, request });
  trackFileListingCompletion(authorization.epoch, completion);
  return response;
}

function clearFileListingCaches(
  event: ExtendableMessageEvent,
  message: ClearMessage,
): void {
  const epoch = ++fileListingEpoch;
  fileListingContexts.clear();
  fileListingStrategies.clear();

  const work = clearFileListingBarrier
    .catch(() => undefined)
    .then(async () => {
      await waitForOlderFileListingWrites(epoch);
      await purgeFileListingCaches();
      if (fileListingEpoch === epoch) readyFileListingEpoch = epoch;
      await notifyWindowClients(epoch);
    });
  clearFileListingBarrier = work;

  event.waitUntil(
    work.then(() => {
      if (!message.requestId) return;
      postToMessageSource(event, {
        type: OFFLINE_FILE_CACHE_CLEAR_ACK_MESSAGE,
        requestId: message.requestId,
        epoch,
      });
    }),
  );
}

cleanupOutdatedCaches();
precacheAndRoute(self.__WB_MANIFEST);
void self.skipWaiting();
clientsClaim();

// v1 used one shared URL-keyed runtime cache. It is unsafe for credentialed
// APIs, so delete it even when Workbox's precache cleanup has nothing to do.
self.addEventListener("activate", (event) => {
  event.waitUntil(purgeFileListingCaches());
});

self.addEventListener("message", (event) => {
  if (isEpochRequestMessage(event.data)) {
    postToMessageSource(event, {
      type: OFFLINE_FILE_CACHE_EPOCH_MESSAGE,
      requestId: event.data.requestId,
      epoch: fileListingEpoch,
    });
    return;
  }
  if (isContextMessage(event.data)) {
    const clientId = clientIdFromMessage(event);
    const accepted = Boolean(
      clientId &&
        event.data.epoch === fileListingEpoch &&
        event.data.epoch === readyFileListingEpoch,
    );
    if (accepted && clientId) {
      fileListingContexts.set(clientId, {
        context: event.data.context,
        epoch: event.data.epoch,
      });
    }
    postToMessageSource(event, {
      type: OFFLINE_FILE_CACHE_CONTEXT_ACK_MESSAGE,
      requestId: event.data.requestId,
      epoch: fileListingEpoch,
      accepted,
    });
    return;
  }
  if (isClearMessage(event.data)) {
    clearFileListingCaches(event, event.data);
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
