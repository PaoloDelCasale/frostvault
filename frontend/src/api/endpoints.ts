import { apiRequest, configureApiClient, setCsrfToken } from "./client";
import type {
  FileHistoryResponse,
  FilesQuery,
  FilesResponse,
  I18nCatalogResponse,
  LocaleUpdateResponse,
  MeResponse,
  StatsResponse,
  VaultsResponse,
} from "./types";
import { translate } from "@/i18n/translate";

const DEFAULT_PAGE_SIZE = 100;

export function fetchFiles(query: FilesQuery = {}): Promise<FilesResponse> {
  const params = new URLSearchParams();
  params.set("q", query.q ?? "");
  params.set("state", query.state ?? "");
  params.set("directory", query.directory ?? "");
  params.set("page", String(query.page ?? 1));
  params.set("page_size", String(query.page_size ?? DEFAULT_PAGE_SIZE));
  return apiRequest<FilesResponse>(`/api/files?${params.toString()}`);
}

export function fetchFileHistory(path: string): Promise<FileHistoryResponse> {
  return apiRequest<FileHistoryResponse>(
    `/api/file-history?path=${encodeURIComponent(path)}`,
  );
}

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
        translate: (key, params) => translate(catalog.messages, key, params),
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

export function fetchStats(): Promise<StatsResponse> {
  return apiRequest<StatsResponse>("/api/stats");
}

export { DEFAULT_PAGE_SIZE };

export function logout(): Promise<{ message: string; message_key: string }> {
  return apiRequest<{ message: string; message_key: string }>("/api/logout", {
    method: "POST",
    body: "{}",
  });
}
