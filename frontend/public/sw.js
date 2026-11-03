// Retirement script at the old URL: existing installations update to this,
// discard the old shell, unregister, and load the online site. Never registered
// by the current frontend. No fetch handler, offline cache or Workbox runtime.
self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});
self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    try {
      for (const name of await caches.keys()) {
        if (
          name === "frostvault-file-listing" ||
          /^frostvault-file-listing-v[23]:/.test(name) ||
          (name.startsWith("workbox-precache-v2-") && name.endsWith(self.registration.scope))
        ) {
          await caches.delete(name);
        }
      }
    } catch {
      // Even if storage is unavailable, the old Worker must stop controlling.
    }
    await self.registration.unregister();
    const clients = await self.clients.matchAll({ type: "window" });
    await Promise.all(clients.map((client) => client.navigate(client.url)));
  })());
});
