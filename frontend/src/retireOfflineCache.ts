/** One-way upgrade cleanup, not a cache or a Service Worker registration. */
export async function retireOfflineCache(): Promise<void> {
  try {
    for (const key of Object.keys(localStorage)) {
      if (
        /^frostvault\.files\.cache\.v[123]:/.test(key) ||
        key === "frostvault.offline-file-cache-barrier.v1"
      ) {
        localStorage.removeItem(key);
      }
    }
  } catch {
    // Storage can be disabled. Never prevent the online site from starting.
  }

  try {
    const registrations = await navigator.serviceWorker?.getRegistrations();
    await Promise.all((registrations ?? []).map(async (registration) => {
      const worker = registration.active ?? registration.waiting ?? registration.installing;
      if (!worker) return;
      const script = new URL(worker.scriptURL);
      if (script.origin === location.origin && script.pathname === "/sw.js") {
        await registration.unregister();
      }
    }));
  } catch {
    // Old browsers and restricted contexts may not expose Service Workers.
  }

  try {
    for (const name of await caches.keys()) {
      if (
        name === "frostvault-file-listing" ||
        /^frostvault-file-listing-v[23]:/.test(name) ||
        (name.startsWith("workbox-precache-v2-") && name.endsWith(`${location.origin}/`))
      ) {
        await caches.delete(name);
      }
    }
  } catch {
    // CacheStorage is optional; the application no longer reads or writes it.
  }
}
