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
  confirmRecoveryCustody,
  createVault,
  exportRecoverySecret,
  fetchI18nCatalog,
  fetchMe,
  fetchStats,
  fetchVaults,
  logout,
  selectVault,
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
  EncryptionMode,
  FilesystemCheck,
  FilesystemFinding,
  FilesystemHealth,
  I18nCatalogResponse,
  LocaleUpdateResponse,
  MeResponse,
  MeVault,
  RecoveryConfirmRequest,
  RecoveryConfirmResponse,
  RecoveryExportRequest,
  RecoveryExportResponse,
  StatsResponse,
  VaultCreateRequest,
  VaultCreateResponse,
  VaultListItem,
  VaultRole,
  VaultSelectRequest,
  VaultSelectResponse,
  VaultsResponse,
} from "./types";
