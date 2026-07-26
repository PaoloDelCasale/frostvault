export {
  ApiError,
  ReauthenticationRedirectError,
  apiRequest,
  configureApiClient,
  loginWithPassword,
  resetApiClientForTests,
  setCsrfToken,
} from "./client";
export type { ApiClientConfig, ApiFetch } from "./client";

export {
  fetchI18nCatalog,
  fetchMe,
  fetchStats,
  fetchVaults,
  logout,
  updateLocale,
} from "./endpoints";

export { createLatestRequestScope } from "./latest";
export type { LatestRequestHandle, LatestRequestScope } from "./latest";

export {
  ACTIVE_JOB_POLL_MS,
  IDLE_POLL_MS,
  jobAwareRefetchInterval,
  jobPollIntervalMs,
} from "./polling";

export {
  ApiQueryProvider,
  apiQueryKeys,
  createAppQueryClient,
  i18nCatalogQueryOptions,
  jobsRefetchInterval,
  meQueryOptions,
  statsQueryOptions,
  vaultsQueryOptions,
} from "./query";

export type {
  AuthMethod,
  FilesystemCheck,
  FilesystemFinding,
  FilesystemHealth,
  I18nCatalogResponse,
  LocaleUpdateResponse,
  MeResponse,
  MeVault,
  StatsResponse,
  VaultListItem,
  VaultRole,
  VaultsResponse,
} from "./types";
