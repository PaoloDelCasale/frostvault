import { useSyncExternalStore, type MouseEvent } from "react";

/** Client routes. Every HTML path is served by the SPA fallback. */
export type Route =
  | { name: "overview" }
  | { name: "files"; vaultId: number }
  | { name: "settings"; vaultId: number }
  | { name: "problems" }
  | { name: "system" }
  | { name: "new-vault" }
  | { name: "login" }
  | { name: "not-found" };

const NAVIGATE_EVENT = "frostvault:navigate";

export function parseRoute(pathname: string): Route {
  const path = pathname.replace(/\/+$/, "") || "/";
  if (path === "/") return { name: "overview" };
  if (path === "/problems") return { name: "problems" };
  if (path === "/system") return { name: "system" };
  if (path === "/vaults/new") return { name: "new-vault" };
  if (path === "/login") return { name: "login" };
  const vault = /^\/vaults\/(\d+)\/(files|settings)$/.exec(path);
  if (vault) {
    const vaultId = Number(vault[1]);
    if (Number.isSafeInteger(vaultId) && vaultId > 0) return { name: vault[2] as "files" | "settings", vaultId };
  }
  return { name: "not-found" };
}

export function vaultFilesUrl(vaultId: number, params?: Record<string, string>): string {
  const query = new URLSearchParams(Object.entries(params ?? {}).filter(([, value]) => value !== ""));
  const search = query.toString();
  return `/vaults/${vaultId}/files${search ? `?${search}` : ""}`;
}

/** `section` names a settings section to scroll to and highlight (`#protection`). */
export function vaultSettingsUrl(vaultId: number, section?: string): string {
  return `/vaults/${vaultId}/settings${section ? `#${section}` : ""}`;
}

/**
 * Translate a pre-#512 address (`/?vault_id=…`, `/general`) to its current
 * route. Email and stored links keep working; `null` means already current.
 */
export function legacyRedirect(pathname: string, search: string): string | null {
  if (pathname === "/general") return "/system";
  if (pathname !== "/" && pathname !== "") return null;
  const params = new URLSearchParams(search);
  const vaultId = Number(params.get("vault_id"));
  if (!params.has("vault_id")) return null;
  if (!Number.isSafeInteger(vaultId) || vaultId < 1) return "/";
  if (params.has("settings")) return vaultSettingsUrl(vaultId);
  params.delete("vault_id");
  const rest: Record<string, string> = {};
  for (const [key, value] of params) rest[key] = value;
  return vaultFilesUrl(vaultId, rest);
}

function currentLocation(): string {
  return `${window.location.pathname}${window.location.search}`;
}

function subscribe(listener: () => void): () => void {
  window.addEventListener("popstate", listener);
  window.addEventListener(NAVIGATE_EVENT, listener);
  return () => {
    window.removeEventListener("popstate", listener);
    window.removeEventListener(NAVIGATE_EVENT, listener);
  };
}

/** Re-renders on Back/Forward and on every `navigate`. */
export function useLocation(): { pathname: string; search: string } {
  const href = useSyncExternalStore(subscribe, currentLocation, currentLocation);
  const index = href.indexOf("?");
  return index < 0 ? { pathname: href, search: "" } : { pathname: href.slice(0, index), search: href.slice(index) };
}

export function navigate(url: string, { replace = false }: { replace?: boolean } = {}): void {
  const target = new URL(url, window.location.origin);
  const normalized = legacyRedirect(target.pathname, target.search) ?? `${target.pathname}${target.search}`;
  // A fragment (`#protection`) is kept so the page can scroll to that section.
  if (normalized === currentLocation() && !target.hash) return;
  window.history[replace ? "replaceState" : "pushState"]({}, "", `${normalized}${target.hash}`);
  // Views that keep their own query state listen to popstate as for Back.
  window.dispatchEvent(new PopStateEvent("popstate"));
  window.dispatchEvent(new Event(NAVIGATE_EVENT));
}

/** Paths that must stay full document navigations (server routes, sign-in). */
function isServerPath(pathname: string): boolean {
  return /^\/(api|auth|static|assets|login)(\/|$)/.test(pathname);
}

/**
 * Turn a plain same-origin link click into a client navigation; modified
 * clicks (new tab, download) keep the browser default.
 */
export function interceptLinkClick(event: MouseEvent | globalThis.MouseEvent): void {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const anchor = (event.target as Element | null)?.closest?.("a[href]");
  if (!(anchor instanceof HTMLAnchorElement)) return;
  if (anchor.target && anchor.target !== "_self") return;
  if (anchor.hasAttribute("download")) return;
  const url = new URL(anchor.href, window.location.href);
  if (url.origin !== window.location.origin || isServerPath(url.pathname)) return;
  event.preventDefault();
  navigate(`${url.pathname}${url.search}${url.hash}`);
}
