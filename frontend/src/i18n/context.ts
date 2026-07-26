import { createContext } from "react";

import type { MessageParams } from "./translate";

export type I18nContextValue = {
  locale: string;
  locales: string[];
  ready: boolean;
  t: (key: string, params?: MessageParams) => string;
  setLocale: (locale: string) => Promise<void>;
};

export const I18nContext = createContext<I18nContextValue | null>(null);
