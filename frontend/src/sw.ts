/// <reference lib="webworker" />
import { clientsClaim } from "workbox-core";
import { ExpirationPlugin } from "workbox-expiration";
import { cleanupOutdatedCaches, precacheAndRoute } from "workbox-precaching";
import { registerRoute } from "workbox-routing";
import { NetworkFirst, NetworkOnly } from "workbox-strategies";

import {
  CLEAR_OFFLINE_FILE_CACHE_MESSAGE,
  OFFLINE_FILE_CACHE_CONTEXT_MESSAGE,
  OFFLINE_FILE_SERVICE_WORKER_CACHE_PREFIX,
  isOfflineCacheContext,
  offlineFileServiceWorkerCacheName,
  type OfflineCacheContext,
} from "./pwa/offlineFiles";

declare let self: ServiceWorkerGlobalScope;

const LEGACY_FILE_LISTING_CACHE_NAME = "frostvault-file-listing";
const fileListingContexts = new Map<string, OfflineCacheContext>();
const fileListingStrategies = new Map<string, NetworkFirst>();
const uncachedFileListingStrategy = new NetworkOnly();

function clientIdFromMessage(event: ExtendableMessageEvent): string | null {
  const source = event.source;
  if (!source || typeof source !== "object" || !("id" in source)) return null;
  const { id } = source as { id?: unknown };
  return typeof id === "string" && id ? id : null;
}

function cacheContextFor(event: ExtendableEvent): OfflineCacheContext | null {
  const clientId = (event as FetchEvent).clientId;
  return clientId ? (fileListingContexts.get(clientId) ?? null) : null;
}

function isContextMessage(
  value: unknown,
): value is { type: typeof OFFLINE_FILE_CACHE_CONTEXT_MESSAGE; context: OfflineCacheContext } {
  if (!value || typeof value !== "object") return false;
  const message = value as { type?: unknown; context?: unknown };
  return (
    message.type === OFFLINE_FILE_CACHE_CONTEXT_MESSAGE &&
    isOfflineCacheContext(message.context)
  );
}

function isClearMessage(
  value: unknown,
): value is { type: typeof CLEAR_OFFLINE_FILE_CACHE_MESSAGE } {
  return (
    Boolean(value) &&
    typeof value === "object" &&
    (value as { type?: unknown }).type === CLEAR_OFFLINE_FILE_CACHE_MESSAGE
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

function fileListingStrategyFor(context: OfflineCacheContext): NetworkFirst {
  const cacheName = offlineFileServiceWorkerCacheName(context);
  const existing = fileListingStrategies.get(cacheName);
  if (existing) return existing;

  const strategy = new NetworkFirst({
    cacheName,
    networkTimeoutSeconds: 3,
    plugins: [
      new ExpirationPlugin({
        maxEntries: 32,
        maxAgeSeconds: 7 * 24 * 60 * 60,
      }),
    ],
  });
  fileListingStrategies.set(cacheName, strategy);
  return strategy;
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
  if (isContextMessage(event.data)) {
    const clientId = clientIdFromMessage(event);
    if (clientId) fileListingContexts.set(clientId, event.data.context);
    return;
  }
  if (!isClearMessage(event.data)) return;

  const clientId = clientIdFromMessage(event);
  if (clientId) fileListingContexts.delete(clientId);
  fileListingStrategies.clear();
  event.waitUntil(purgeFileListingCaches());
});

registerRoute(
  ({ request, url }) => request.method === "GET" && url.pathname === "/api/files",
  async ({ event, request }) => {
    const context = cacheContextFor(event);
    // A worker without an authenticated context must never fall back to a
    // shared URL-only cache for credentialed file listings.
    if (!context) return uncachedFileListingStrategy.handle({ event, request });
    return fileListingStrategyFor(context).handle({ event, request });
  },
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
