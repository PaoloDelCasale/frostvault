export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_PREFIX = "frostvault_theme";
export const THEME_ACTIVE_USER_STORAGE_KEY = `${THEME_STORAGE_PREFIX}_active_user`;
export const THEME_GUEST_STORAGE_KEY = `${THEME_STORAGE_PREFIX}_guest`;

export function normalizeThemePreference(value: unknown): ThemePreference {
  return value === "light" || value === "dark" || value === "system" ? value : "system";
}

export function themeStorageKey(userId: string | number | null): string {
  if (userId === null || userId === undefined || userId === "") {
    return THEME_GUEST_STORAGE_KEY;
  }
  return `${THEME_STORAGE_PREFIX}_user_${encodeURIComponent(String(userId))}`;
}

export function readStorageValue(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    // Private browsing and denied storage must not prevent the app from loading.
    return null;
  }
}

export function writeStorageValue(key: string, value: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    window.localStorage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

export function removeStorageValue(key: string): boolean {
  if (typeof window === "undefined") return false;
  try {
    window.localStorage.removeItem(key);
    return true;
  } catch {
    return false;
  }
}

export function readThemePreference(userId: string | number | null): ThemePreference {
  return normalizeThemePreference(readStorageValue(themeStorageKey(userId)));
}

export function readActiveUserId(): string | null {
  const value = readStorageValue(THEME_ACTIVE_USER_STORAGE_KEY);
  return value && value.length > 0 ? value : null;
}

export function getSystemTheme(): ResolvedTheme {
  if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
    try {
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    } catch {
      // Fall back to the accessible light palette when media queries are unavailable.
    }
  }
  return "light";
}

export function resolveTheme(
  preference: ThemePreference,
  systemTheme: ResolvedTheme,
): ResolvedTheme {
  return preference === "system" ? systemTheme : preference;
}

export function applyTheme(theme: ResolvedTheme): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.classList.toggle("dark", theme === "dark");
  root.style.colorScheme = theme;
}

export function activeUserStorageKeyScript(): string {
  return `
(function () {
  var prefix = "${THEME_STORAGE_PREFIX}";
  var activeUserKey = "${THEME_ACTIVE_USER_STORAGE_KEY}";
  var guestKey = "${THEME_GUEST_STORAGE_KEY}";
  var preference = "system";
  try {
    var activeUser = window.localStorage.getItem(activeUserKey);
    var key = activeUser ? prefix + "_user_" + encodeURIComponent(activeUser) : guestKey;
    var stored = window.localStorage.getItem(key);
    if (stored === "light" || stored === "dark" || stored === "system") preference = stored;
  } catch (_) {}
  var dark = preference === "dark";
  if (preference === "system") {
    try { dark = window.matchMedia("(prefers-color-scheme: dark)").matches; } catch (_) {}
  }
  var theme = dark ? "dark" : "light";
  var root = document.documentElement;
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
  if (dark) root.classList.add("dark");
})();`.trim();
}
