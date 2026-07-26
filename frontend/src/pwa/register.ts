import { registerSW } from "virtual:pwa-register";

/**
 * Register the installable PWA service worker (auto-update).
 * Safe to call once at app bootstrap; no-ops when SW APIs are missing.
 */
export function registerFrostVaultServiceWorker(): void {
  if (typeof window === "undefined" || !("serviceWorker" in navigator)) {
    return;
  }
  registerSW({ immediate: true });
}
