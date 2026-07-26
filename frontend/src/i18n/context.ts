import { createContext } from "react";

import type { MessageParams } from "./translate";

export type SetLocaleOptions = {
  /**
   * Guest mode for the sign-in screen: set the locale cookie and load the
   * public catalog. Authenticated Session updates use PUT /api/locale (default).
   */
  mode?: "session" | "guest";
};

export type I18nContextValue = {
  locale: string;
  locales: string[];
  ready: boolean;
  t: (key: string, params?: MessageParams) => string;
  setLocale: (locale: string, options?: SetLocaleOptions) => Promise<void>;
};

export const I18nContext = createContext<I18nContextValue | null>(null);
