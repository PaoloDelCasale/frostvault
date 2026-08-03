/**
 * Screenshot/demo seams are available in Vite development and in an explicit
 * capture build only. Keep this gate shared so production URL parameters never
 * activate capture-only behavior by accident.
 */
export const DEMO_MODE_ENABLED =
  import.meta.env.DEV || import.meta.env.VITE_ALLOW_DEMO === "1";

/** Read a capture-only query parameter without exposing it in normal builds. */
export function getDemoSearchParam(name: string): string | null {
  if (!DEMO_MODE_ENABLED || typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get(name);
}
