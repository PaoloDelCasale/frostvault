import { createContext } from "react";

import type { ResolvedTheme, ThemePreference } from "./theme";

export type ThemeContextValue = {
  userId: string | null;
  preference: ThemePreference;
  resolvedTheme: ResolvedTheme;
  setTheme: (preference: ThemePreference) => void;
  setUserId: (userId: string | number | null | undefined) => void;
};

export const ThemeContext = createContext<ThemeContextValue>({
  userId: null,
  preference: "system",
  resolvedTheme: "light",
  setTheme: () => undefined,
  setUserId: () => undefined,
});
