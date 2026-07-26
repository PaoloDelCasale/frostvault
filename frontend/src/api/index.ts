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
  DEFAULT_PAGE_SIZE,
  fetchFileHistory,
  fetchFiles,
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
  fileHistoryQueryOptions,
  filesQueryOptions,
  i18nCatalogQueryOptions,
  jobsRefetchInterval,
  meQueryOptions,
  statsQueryOptions,
  vaultsQueryOptions,
} from "./query";

export type {
  ArchiveListItem,
  ArchiveVersionSummary,
  AuthMethod,
  DirectoryListItem,
  FileHistoryResponse,
  FilesQuery,
  FilesResponse,
  FilesystemCheck,
  FilesystemFinding,
  FilesystemHealth,
  I18nCatalogResponse,
  LocaleUpdateResponse,
  MeResponse,
  MeVault,
  PathHistoryEntry,
  StatsResponse,
  VaultFileListItem,
  VaultListItem,
  VaultRole,
  VaultsResponse,
} from "./types";
