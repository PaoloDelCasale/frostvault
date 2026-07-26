export {
  ApiError,
  ReauthenticationRedirectError,
  apiRequest,
  configureApiClient,
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
  fetchVaultMembers,
  fetchVaultQuotas,
  fetchVaults,
  lookupVaultUser,
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
  vaultsQueryOptions,
} from "./query";

export type {
  AuthMethod,
  CloudDeletionSettings,
  GlobPreviewResponse,
  I18nCatalogResponse,
  LifecycleGuidedProfile,
  LifecycleResponse,
  LocaleUpdateResponse,
  MeResponse,
  MeVault,
  OperationPolicy,
  QuotaEvaluation,
  UserLookupResult,
  VaultListItem,
  VaultMember,
  VaultMembersResponse,
  VaultQuotaUpdatePayload,
  VaultQuotasResponse,
  VaultRole,
  VaultsResponse,
} from "./types";
