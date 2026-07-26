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
  addAdminVaultMember,
  createAdminUser,
  createAdminVault,
  exportAdminVaultRecovery,
  fetchAdminUsers,
  fetchAdminVaultMembers,
  fetchAdminVaultQuotas,
  fetchAdminVaults,
  fetchI18nCatalog,
  fetchMe,
  fetchVaults,
  removeAdminVaultMember,
  transferAdminVaultOwner,
  updateAdminUser,
  updateAdminVaultQuotas,
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
  vaultsQueryOptions,
} from "./query";

export type {
  AdminMembershipCreatePayload,
  AdminOwnerTransferPayload,
  AdminUser,
  AdminUserCreatePayload,
  AdminUsersResponse,
  AdminUserUpdatePayload,
  AdminVault,
  AdminVaultCreatePayload,
  AdminVaultMember,
  AdminVaultMembersResponse,
  AdminVaultsResponse,
  AuthMethod,
  I18nCatalogResponse,
  LocaleUpdateResponse,
  MeResponse,
  MeVault,
  QuotaDecision,
  QuotaEvaluation,
  QuotaLimits,
  QuotaUsage,
  RecoveryExportResponse,
  VaultListItem,
  VaultQuotaUpdatePayload,
  VaultQuotasResponse,
  VaultRole,
  VaultsResponse,
} from "./types";
