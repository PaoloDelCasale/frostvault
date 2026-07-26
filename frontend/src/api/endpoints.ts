import { apiRequest, configureApiClient, setCsrfToken } from "./client";
import type {
  I18nCatalogResponse,
  LocaleUpdateResponse,
  MeResponse,
  VaultsResponse,
} from "./types";

export function fetchMe(): Promise<MeResponse> {
  return apiRequest<MeResponse>("/api/me").then((me) => {
    setCsrfToken(me.csrf_token);
    configureApiClient({ getAuthMethod: () => me.auth_method });
    return me;
  });
}

export function fetchVaults(): Promise<VaultsResponse> {
  return apiRequest<VaultsResponse>("/api/vaults");
}

export function fetchI18nCatalog(locale?: string): Promise<I18nCatalogResponse> {
  const query = locale ? `?locale=${encodeURIComponent(locale)}` : "";
  return apiRequest<I18nCatalogResponse>(`/api/i18n/catalog${query}`).then(
    (catalog) => {
      configureApiClient({
        translate: (key, params) => {
          let message = catalog.messages[key] ?? key;
          if (params) {
            for (const [name, value] of Object.entries(params)) {
              message = message.replaceAll(`{${name}}`, String(value));
            }
          }
          return message;
        },
      });
      return catalog;
    },
  );
}

export function updateLocale(locale: string): Promise<LocaleUpdateResponse> {
  return apiRequest<LocaleUpdateResponse>("/api/locale", {
    method: "PUT",
    body: JSON.stringify({ locale }),
  });
}
