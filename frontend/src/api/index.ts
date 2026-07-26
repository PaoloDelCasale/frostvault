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
  addVaultMember,
  deleteLifecycleFolderOverride,
  fetchCloudDeletion,
  fetchI18nCatalog,
  fetchLifecycle,
  fetchMe,
  fetchOperationPolicy,
  fetchStats,
  fetchVaultMembers,
  fetchVaultQuotas,
  fetchVaults,
  lookupVaultUser,
  logout,
  previewOperationGlobs,
  removeVaultMember,
  transferVaultOwner,
  updateAdminVaultQuotas,
  updateCloudDeletion,
  updateLifecycleDefault,
  updateLocale,
  updateOperationPolicy,
  upsertLifecycleFolderOverride,
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
  CloudDeletionSettings,
  FilesystemCheck,
  FilesystemFinding,
  FilesystemHealth,
  GlobPreviewResponse,
  I18nCatalogResponse,
  LifecycleGuidedProfile,
  LifecycleResponse,
  LocaleUpdateResponse,
  MeResponse,
  MeVault,
  OperationPolicy,
  QuotaEvaluation,
  StatsResponse,
  UserLookupResult,
  VaultListItem,
  VaultMember,
  VaultMembersResponse,
  VaultQuotaUpdatePayload,
  VaultQuotasResponse,
  VaultRole,
  VaultsResponse,
} from "./types";
