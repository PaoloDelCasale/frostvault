/// <reference lib="webworker" />
import { clientsClaim } from "workbox-core";
import { ExpirationPlugin } from "workbox-expiration";
import { cleanupOutdatedCaches, precacheAndRoute } from "workbox-precaching";
import { registerRoute } from "workbox-routing";
import { NetworkFirst } from "workbox-strategies";

declare let self: ServiceWorkerGlobalScope;

cleanupOutdatedCaches();
precacheAndRoute(self.__WB_MANIFEST);
void self.skipWaiting();
clientsClaim();

registerRoute(
  ({ url }) => url.pathname === "/api/files",
  new NetworkFirst({
    cacheName: "frostvault-file-listing",
    networkTimeoutSeconds: 3,
    plugins: [
      new ExpirationPlugin({
        maxEntries: 32,
        maxAgeSeconds: 7 * 24 * 60 * 60,
      }),
    ],
  }),
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
