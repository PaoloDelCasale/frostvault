export {
  ApiError,
  ReauthenticationRedirectError,
  apiDownload,
  apiRequest,
  configureApiClient,
  filenameFromContentDisposition,
  loginWithPassword,
  resetApiClientForTests,
  setCsrfToken,
} from "./client";
export type { ApiClientConfig, ApiDownload, ApiFetch } from "./client";

export {
  createVault,
  fetchGeneralVaults,
  markNotificationRead,
  markAllNotificationsRead,
  fetchGeneralMetadataBackups,
  runGeneralMetadataBackup,
  downloadGeneralMetadataBackup,
  fetchGeneralSourceVolumes,
  browseSourceVolume,
  fetchBillingExport,
  fetchI18nCatalog,
  fetchLoginOptions,
  fetchMe,
  fetchNotifications,
  fetchOverview,
  fetchProblems,
  fetchUploadActivity,
  retryFailedUploads,
  searchFiles,
  fetchVaults,
  logout,
  fetchRecoveryKitStatus,
  downloadRecoveryKit,
  acknowledgeRecoveryKit,
  selectVault,
  updateLocale,
} from "./endpoints";
export type {
  NotificationListStatus,
  FetchNotificationsParams,
  MarkAllNotificationsReadResponse,
  MetadataBackupRun,
  MetadataBackupRunResult,
  MetadataBackupStatus,
  MetadataBackupsResponse,
  MetadataBackupDownload,
  SystemBucket,
} from "./endpoints";

export { vaultApi } from "./vault";
export type { VaultApi } from "./vault";
export { useVaultStats } from "./useVaultStats";
export { uploadBacklog, useUploadActivity } from "./uploadActivity";


export {
  catalogEventsUrl,
  catalogRevisionUrl,
  fetchCatalogRevision,
  getDefaultEventSourceFactory,
  normalizeDomains,
  openCatalogEventSource,
  parseCatalogEventData,
} from "./catalogEvents";
export type {
  CatalogEventHandlers,
  CatalogEventSource,
  CatalogEventSourceFactory,
  CatalogHelloSignal,
  CatalogInvalidationDomains,
  CatalogRevisionSignal,
} from "./catalogEvents";
export { CATALOG_REFRESH_MS, STATS_REFRESH_MS, useCatalogEvents } from "./useCatalogEvents";
export type { UseCatalogEventsOptions } from "./useCatalogEvents";

export {
  ApiQueryProvider,
  apiQueryKeys,
  createAppQueryClient,
  i18nCatalogQueryOptions,
  notificationsQueryOptions,
} from "./query";

export type * from "./types";
