import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import {
  applyTheme,
  getSystemTheme,
  readActiveUserId,
  readThemePreference,
  removeStorageValue,
  resolveTheme,
  themeStorageKey,
  type ResolvedTheme,
  type ThemePreference,
  writeStorageValue,
  THEME_ACTIVE_USER_STORAGE_KEY,
} from "./theme";
import { ThemeContext, type ThemeContextValue } from "./context";

function normalizeUserId(userId: string | number | null | undefined): string | null {
  if (userId === null || userId === undefined || userId === "") return null;
  return String(userId);
}

function readInitialUserId(): string | null {
  if (typeof window !== "undefined" && window.location.pathname === "/login") {
    // A login screen has no trusted identity. Remove stale state before the
    // first React paint rather than waiting for LoginPage's effect.
    removeStorageValue(THEME_ACTIVE_USER_STORAGE_KEY);
    return null;
  }
  return readActiveUserId();
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [userId, setUserIdState] = useState<string | null>(readInitialUserId);
  const [preference, setPreference] = useState<ThemePreference>(() =>
    readThemePreference(userId),
  );
  const [systemTheme, setSystemTheme] = useState<ResolvedTheme>(getSystemTheme);
  const resolvedTheme = resolveTheme(preference, systemTheme);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    let mediaQuery: MediaQueryList;
    try {
      mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
    } catch {
      return;
    }
    const onChange = (event: MediaQueryListEvent) => {
      setSystemTheme(event.matches ? "dark" : "light");
    };
    setSystemTheme(mediaQuery.matches ? "dark" : "light");
    if (mediaQuery.addEventListener) {
      mediaQuery.addEventListener("change", onChange);
    } else {
      // Older Safari exposes addListener rather than addEventListener.
      mediaQuery.addListener?.(onChange);
    }
    return () => {
      if (mediaQuery.removeEventListener) {
        mediaQuery.removeEventListener("change", onChange);
      } else {
        mediaQuery.removeListener?.(onChange);
      }
    };
  }, []);

  useLayoutEffect(() => {
    applyTheme(resolvedTheme);
  }, [resolvedTheme]);

  const identifyUser = useCallback((nextUserId: string | number | null | undefined) => {
    const normalized = normalizeUserId(nextUserId);
    setUserIdState(normalized);
    setPreference(readThemePreference(normalized));
    if (normalized === null) {
      removeStorageValue(THEME_ACTIVE_USER_STORAGE_KEY);
    } else {
      writeStorageValue(THEME_ACTIVE_USER_STORAGE_KEY, normalized);
    }
  }, []);

  const setTheme = useCallback(
    (nextPreference: ThemePreference) => {
      setPreference(nextPreference);
      writeStorageValue(themeStorageKey(userId), nextPreference);
    },
    [userId],
  );

  const value = useMemo<ThemeContextValue>(
    () => ({
      userId,
      preference,
      resolvedTheme,
      setTheme,
      setUserId: identifyUser,
    }),
    [identifyUser, preference, resolvedTheme, setTheme, userId],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}
