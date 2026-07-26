import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, type ReactNode } from "react";

import {
  apiQueryKeys,
  configureApiClient,
  fetchI18nCatalog,
  i18nCatalogQueryOptions,
  updateLocale,
} from "@/api";
import type { I18nCatalogResponse } from "@/api";
import {
  I18nContext,
  type I18nContextValue,
  type SetLocaleOptions,
} from "./context";
import { translate } from "./translate";

const LOCALE_COOKIE = "frostvault_locale";

function applyCatalogToApiClient(catalog: I18nCatalogResponse): void {
  configureApiClient({
    translate: (key, params) => translate(catalog.messages, key, params),
  });
}

function writeLocaleCookie(locale: string): void {
  if (typeof document === "undefined") return;
  const value = locale === "it" ? "it" : "en";
  document.cookie = `${LOCALE_COOKIE}=${encodeURIComponent(value)}; Path=/; Max-Age=31536000; SameSite=Lax`;
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

  const cacheCatalog = useCallback(
    (catalog: I18nCatalogResponse) => {
      applyCatalogToApiClient(catalog);
      queryClient.setQueryData(apiQueryKeys.i18nCatalog(initialLocale), catalog);
      queryClient.setQueryData(apiQueryKeys.i18nCatalog(catalog.locale), catalog);
      queryClient.setQueryData(apiQueryKeys.i18nCatalog(undefined), catalog);
      if (typeof document !== "undefined") {
        document.documentElement.lang = catalog.locale;
      }
    },
    [initialLocale, queryClient],
  );

  const setLocale = useCallback(
    async (locale: string, options?: SetLocaleOptions) => {
      if (options?.mode === "guest") {
        writeLocaleCookie(locale);
        const catalog = await fetchI18nCatalog(locale);
        cacheCatalog(catalog);
        return;
      }
      const updated = await updateLocale(locale);
      const catalog: I18nCatalogResponse = {
        locale: updated.locale,
        locales: catalogQuery.data?.locales ?? [updated.locale],
        messages: updated.messages,
      };
      cacheCatalog(catalog);
    },
    [cacheCatalog, catalogQuery.data?.locales],
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
