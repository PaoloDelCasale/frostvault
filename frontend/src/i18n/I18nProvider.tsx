import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  type ReactNode,
} from "react";

import {
  apiQueryKeys,
  configureApiClient,
  i18nCatalogQueryOptions,
  updateLocale,
} from "@/api";
import type { I18nCatalogResponse } from "@/api";
import { translate, type MessageParams } from "./translate";

export type I18nContextValue = {
  locale: string;
  locales: string[];
  ready: boolean;
  t: (key: string, params?: MessageParams) => string;
  setLocale: (locale: string) => Promise<void>;
};

const I18nContext = createContext<I18nContextValue | null>(null);

function applyCatalogToApiClient(catalog: I18nCatalogResponse): void {
  configureApiClient({
    translate: (key, params) => translate(catalog.messages, key, params),
  });
}

export function I18nProvider({
  children,
  initialLocale,
}: {
  children: ReactNode;
  initialLocale?: string;
}) {
  const queryClient = useQueryClient();
  const catalogQuery = useQuery(i18nCatalogQueryOptions(initialLocale));

  useEffect(() => {
    if (catalogQuery.data) {
      applyCatalogToApiClient(catalogQuery.data);
    }
  }, [catalogQuery.data]);

  useEffect(() => {
    const locale = catalogQuery.data?.locale;
    if (locale && typeof document !== "undefined") {
      document.documentElement.lang = locale;
    }
  }, [catalogQuery.data?.locale]);

  const setLocale = useCallback(
    async (locale: string) => {
      const updated = await updateLocale(locale);
      const catalog: I18nCatalogResponse = {
        locale: updated.locale,
        locales: catalogQuery.data?.locales ?? [updated.locale],
        messages: updated.messages,
      };
      applyCatalogToApiClient(catalog);
      queryClient.setQueryData(apiQueryKeys.i18nCatalog(initialLocale), catalog);
      queryClient.setQueryData(apiQueryKeys.i18nCatalog(updated.locale), catalog);
      queryClient.setQueryData(apiQueryKeys.i18nCatalog(undefined), catalog);
    },
    [catalogQuery.data?.locales, initialLocale, queryClient],
  );

  const value = useMemo<I18nContextValue>(() => {
    const catalog = catalogQuery.data;
    const messages = catalog?.messages ?? {};
    return {
      locale: catalog?.locale ?? initialLocale ?? "en",
      locales: catalog?.locales ?? ["en", "it"],
      ready: Boolean(catalog),
      t: (key, params) => translate(messages, key, params),
      setLocale,
    };
  }, [catalogQuery.data, initialLocale, setLocale]);

  return (
    <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
  );
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext);
  if (!ctx) {
    throw new Error("useI18n must be used within I18nProvider");
  }
  return ctx;
}
