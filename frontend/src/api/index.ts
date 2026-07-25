export {
  ApiError,
  ReauthenticationRedirectError,
  apiRequest,
  configureApiClient,
  resetApiClientForTests,
  setCsrfToken,
} from "./client";
export type { ApiClientConfig, ApiFetch } from "./client";

export { fetchI18nCatalog, fetchMe, fetchVaults } from "./endpoints";

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
  vaultsQueryOptions,
} from "./query";

export type {
  AuthMethod,
  I18nCatalogResponse,
  MeResponse,
  MeVault,
  VaultListItem,
  VaultRole,
  VaultsResponse,
} from "./types";
