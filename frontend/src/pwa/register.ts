import { registerSW } from "virtual:pwa-register";

/**
 * Register the installable PWA service worker (auto-update).
 * Safe to call once at app bootstrap; no-ops when SW APIs are missing.
 *
 * A replacement controller (`isUpdate` / `isExternal`) must not reload the
 * document: App.tsx keeps `#main-content` mounted and reconciles authority in
 * place on `controllerchange`. vite-plugin-pwa would otherwise reload the
 * document, which tears down the landmark and, while `/api/me` is in flight,
 * looks like a logout.
 */
export function registerFrostVaultServiceWorker(): void {
  if (typeof window === "undefined" || !("serviceWorker" in navigator)) {
    return;
  }
  registerSW({
    immediate: true,
    // In-place reconciliation owns this transition. See App.tsx.
    onNeedReload: () => undefined,
  });
}
