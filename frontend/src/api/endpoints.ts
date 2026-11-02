import {
  apiDownload,
  apiRequest,
  configureApiClient,
  setCsrfToken,
} from "./client";
import type { ApiDownload } from "./client";
import type {
  AdminVaultRelocatePayload,
  AdminVaultRelocationResponse,
  AdminVaultsResponse,
  VaultDecommissionPreview,
  VaultDecommissionSelection,
  VaultDecommissionStartPayload,
  VaultDecommissionStatus,
  SourceVolumeInventoryResponse,
  SourceDirectoryBrowseResponse,
  CloudDeletionPreview,
  CloudDeletionSettings,
  CloudPurgePayload,
  FileHistoryResponse,
  FileOperationPayload,
  FileOperationResponse,
  FilesQuery,
  FilesResponse,
  FileVersionsResponse,
  GlobPreviewResponse,
  I18nCatalogResponse,
  JobCancelPayload,
  JobCancelResponse,
  JobsResponse,
  LifecycleProfileSelection,
  LifecycleResponse,
  LocaleUpdateResponse,
  MetadataBackupRunAction,
  MeResponse,
  OperationPolicy,
  RecoverEstimateResponse,
  RecoveryConfirmRequest,
  RecoveryConfirmResponse,
  RecoveryExportRequest,
  RecoveryExportResponse,
  ScanResponse,
  SmtpEndpointAction,
  StatsResponse,
  StorageClassesResponse,
  StorageEstimateRequest,
  StorageEstimateResponse,
  SystemSettingsResponse,
  SystemSettingsUpdatePayload,
  VaultCreateRequest,
  VaultCreateResponse,
  VaultSelectRequest,
  VaultSelectResponse,
  VaultsResponse,
  WebhookEndpointAction,
} from "./types";
import { translate } from "@/i18n/translate";

const DEFAULT_PAGE_SIZE = 100;

/**
 * The rename-candidate route intentionally has a loose backend response model.
 * These are the fields currently emitted by ArchiveCatalog; size is optional so
 * the UI can show it if the contract grows without claiming evidence it lacks.
 */
export type RenameCandidate = {
  missing_vault_file_id: string;
  missing_path: string;
  new_vault_file_id: string;
  new_path: string;
  digest: string;
  decision: "auto" | "ambiguous" | "review" | "content_changed" | string;
  size?: number | null;
};

export type RenameCandidatesResponse = { items: RenameCandidate[] };
export type RenameConfirmationResponse = Record<string, unknown>;

/**
 * The notification routes intentionally use the generic JSON response model on
 * the server. Keep the inbox wire types here instead of claiming they are
 * generated OpenAPI schemas (the preferences GET is a Starlette route).
 */
export type NotificationItem = {
  id: number;
  user_id: number;
  vault_id: number | null;
  job_id: number | null;
  event: string;
  title: string;
  body: string;
  title_key: string | null;
  body_key: string | null;
  message_params: Record<string, unknown>;
  in_app_enabled: boolean;
  dedupe_key: string | null;
  created_at: string;
  read: boolean;
  read_at: string | null;
};

export type NotificationListStatus = "unread" | "read" | "all";

export type NotificationsResponse = {
  items: NotificationItem[];
  unread_count: number;
  /** True when another bounded page is available via ``before_id``. */
  has_more?: boolean;
};

export type FetchNotificationsParams = {
  limit?: number;
  status?: NotificationListStatus;
  beforeId?: number;
};

export type MarkAllNotificationsReadResponse = {
  marked_count: number;
  unread_count: number;
};

export type NotificationPreferenceChannel = "in_app" | "push";

export type VaultNotificationPreference = {
  id: number;
  user_id: number;
  vault_id: number;
  event: string;
  channel: NotificationPreferenceChannel;
  enabled: boolean;
};

export type VaultNotificationPreferencesResponse = {
  items: VaultNotificationPreference[];
};

export type VaultNotificationPreferencePayload = {
  event: string;
  channel: NotificationPreferenceChannel;
  enabled: boolean;
};

/** Persisted worker errors are currently exposed through a generic JSON response. */
export type AdminWorkerError = {
  id: number;
  created_at: string;
  component: string;
  classification: string;
  message: string;
  vault_id: number | null;
  detail: Record<string, unknown>;
};

export type AdminWorkerErrorsResponse = { items: AdminWorkerError[] };
/** Short aliases for consumers that do not need to repeat the admin scope. */
export type WorkerError = AdminWorkerError;
export type WorkerErrorsResponse = AdminWorkerErrorsResponse;

/**
 * Audit-event routes still use a generic JSON response. The server returns the
 * newest 100 rows and intentionally exposes no query parameters, so clients
 * must treat any filtering as local to this bounded loaded window.
 */
export type AuditEvent = {
  id: number;
  created_at: string;
  event: string;
  outcome: string | null;
  actor_user_id: number | null;
  vault_id: number | null;
  job_id: number | null;
  correlation_id: string | null;
  visibility: string;
  detail: Record<string, unknown>;
};

export type AuditEventsResponse = { events: AuditEvent[] };
export type VaultAuditEventsResponse = AuditEventsResponse;
export type AdminAuditEventsResponse = AuditEventsResponse;

/** Safe metadata-backup fields exposed to the SPA; filesystem paths are omitted. */
export type MetadataBackupRun = {
  id: number;
  created_at: string;
  finished_at: string | null;
  reason: string;
  backend: string;
  status: string;
  digest_sha256: string | null;
  database_sha256: string | null;
  s3_key: string | null;
  size_bytes: number | null;
  error_message: string | null;
  verified_at: string | null;
};

export type MetadataBackupStatus = {
  last_status: string;
  last_run: MetadataBackupRun | null;
  succeeded_count: number;
  failed_count: number;
};

export type MetadataBackupsResponse = {
  status: MetadataBackupStatus;
  runs: MetadataBackupRun[];
};

export type MetadataBackupRunResult = {
  ok: boolean;
  reason: string;
  digest_sha256: string;
  database_sha256: string;
  backend: string;
  s3_key: string | null;
  size_bytes: number;
  filename: string;
  created_at: string;
};

export type MetadataBackupDownload = ApiDownload;

type MetadataBackupRunWire = MetadataBackupRun & {
  local_path?: unknown;
  path?: unknown;
};

type MetadataBackupRunResultWire = MetadataBackupRunResult & {
  local_path?: unknown;
  path?: unknown;
};

type MetadataBackupsResponseWire = {
  status: Omit<MetadataBackupStatus, "last_run"> & {
    last_run: MetadataBackupRunWire | null;
  };
  runs: MetadataBackupRunWire[];
};

function withoutMetadataBackupPaths<T extends Record<string, unknown>>(
  value: T,
): Omit<T, "local_path" | "path"> {
  const safe = { ...value } as T & {
    local_path?: unknown;
    path?: unknown;
  };
  delete safe.local_path;
  delete safe.path;
  return safe as Omit<T, "local_path" | "path">;
}

function safeMetadataBackupRun(run: MetadataBackupRunWire): MetadataBackupRun {
  return withoutMetadataBackupPaths(run) as MetadataBackupRun;
}

function safeMetadataBackupResult(
  result: MetadataBackupRunResultWire,
): MetadataBackupRunResult {
  return withoutMetadataBackupPaths(result) as MetadataBackupRunResult;
}

export function fetchFiles(
  query: FilesQuery = {},
  options?: RequestInit,
): Promise<FilesResponse> {
  const params = new URLSearchParams();
  params.set("q", query.q ?? "");
  params.set("state", query.state ?? "");
  if (typeof query.storage === "string" && query.storage) {
    params.set("storage", query.storage);
  }
  params.set("directory", query.directory ?? "");
  params.set("page", String(query.page ?? 1));
  params.set("page_size", String(query.page_size ?? DEFAULT_PAGE_SIZE));
  if (typeof query.sort === "string" && query.sort) {
    params.set("sort", query.sort);
  }
  if (typeof query.order === "string" && query.order) {
    params.set("order", query.order);
  }
  return apiRequest<FilesResponse>(`/api/files?${params.toString()}`, options);
}

export function fetchFileHistory(path: string): Promise<FileHistoryResponse> {
  return apiRequest<FileHistoryResponse>(
    `/api/file-history?path=${encodeURIComponent(path)}`,
  );
}

export function fetchRenameCandidates(): Promise<RenameCandidatesResponse> {
  return apiRequest<RenameCandidatesResponse>("/api/rename-candidates");
}

export function confirmFileRename(payload: {
  vault_file_id: string;
  new_path: string;
  allow_content_change?: boolean;
}): Promise<RenameConfirmationResponse> {
  return apiRequest<RenameConfirmationResponse>("/api/confirm-rename", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function confirmFolderRename(payload: {
  old_prefix: string;
  new_prefix: string;
}): Promise<RenameConfirmationResponse> {
  return apiRequest<RenameConfirmationResponse>("/api/confirm-folder-rename", {
    method: "POST",
    body: JSON.stringify(payload),
  });
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

export function createVault(
  payload: VaultCreateRequest,
): Promise<VaultCreateResponse> {
  return apiRequest<VaultCreateResponse>("/api/vaults", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function selectVault(
  payload: VaultSelectRequest,
): Promise<VaultSelectResponse> {
  return apiRequest<VaultSelectResponse>("/api/vaults/select", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function confirmRecoveryCustody(
  payload: RecoveryConfirmRequest = { acknowledged: true },
): Promise<RecoveryConfirmResponse> {
  return apiRequest<RecoveryConfirmResponse>("/api/vault/recovery/confirm", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function exportRecoverySecret(
  payload: RecoveryExportRequest,
): Promise<RecoveryExportResponse> {
  return apiRequest<RecoveryExportResponse>("/api/vault/recovery/export", {
    method: "POST",
    body: JSON.stringify(payload),
  });
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

export function fetchOperationPolicy(): Promise<OperationPolicy> {
  return apiRequest<OperationPolicy>("/api/vault/operation-policy");
}

export function updateOperationPolicy(
  policy: OperationPolicy,
): Promise<OperationPolicy> {
  return apiRequest<OperationPolicy>("/api/vault/operation-policy", {
    method: "PUT",
    body: JSON.stringify(policy),
  });
}

export function previewOperationGlobs(payload: {
  paths: string[];
  include_globs: string[];
  exclude_globs: string[];
}): Promise<GlobPreviewResponse> {
  return apiRequest<GlobPreviewResponse>(
    "/api/vault/operation-policy/preview-globs",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function fetchLifecycle(): Promise<LifecycleResponse> {
  return apiRequest<LifecycleResponse>("/api/vault/lifecycle");
}

export function updateLifecycleDefault(
  selection: string | LifecycleProfileSelection,
): Promise<LifecycleResponse> {
  const payload =
    typeof selection === "string" ? { guided_profile: selection } : selection;
  return apiRequest<LifecycleResponse>("/api/vault/lifecycle/default", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function upsertLifecycleFolderOverride(
  folderPath: string,
  selection: string | LifecycleProfileSelection,
): Promise<LifecycleResponse> {
  const payload =
    typeof selection === "string" ? { guided_profile: selection } : selection;
  return apiRequest<LifecycleResponse>("/api/vault/lifecycle/folder-overrides", {
    method: "PUT",
    body: JSON.stringify({ folder_path: folderPath, ...payload }),
  });
}

export function deleteLifecycleFolderOverride(
  folderPath: string,
): Promise<LifecycleResponse> {
  return apiRequest<LifecycleResponse>("/api/vault/lifecycle/folder-overrides", {
    method: "DELETE",
    body: JSON.stringify({ folder_path: folderPath }),
  });
}

export function fetchCloudDeletion(): Promise<CloudDeletionSettings> {
  return apiRequest<CloudDeletionSettings>("/api/vault/cloud-deletion");
}

export function updateCloudDeletion(
  enabled: boolean,
): Promise<CloudDeletionSettings> {
  return apiRequest<CloudDeletionSettings>("/api/vault/cloud-deletion", {
    method: "PUT",
    body: JSON.stringify({ enabled }),
  });
}

export function fetchSystemSettings(): Promise<SystemSettingsResponse> {
  return apiRequest<SystemSettingsResponse>("/api/admin/settings");
}

export type NotificationEndpointResponse = {
  id: number;
  kind: "webhook" | "smtp";
  enabled: boolean;
  name?: string;
};

export function saveAdminWebhookEndpoint(
  payload: WebhookEndpointAction,
): Promise<NotificationEndpointResponse> {
  return apiRequest<NotificationEndpointResponse>(
    "/api/admin/notification-endpoints/webhook",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function saveAdminSmtpEndpoint(
  payload: SmtpEndpointAction,
): Promise<NotificationEndpointResponse> {
  return apiRequest<NotificationEndpointResponse>(
    "/api/admin/notification-endpoints/smtp",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

/** List the authenticated user's visible in-app notifications and unread count. */
export function fetchNotifications(
  limitOrParams?: number | FetchNotificationsParams,
): Promise<NotificationsResponse> {
  const params: FetchNotificationsParams =
    typeof limitOrParams === "number"
      ? { limit: limitOrParams }
      : (limitOrParams ?? {});
  const search = new URLSearchParams();
  if (params.limit !== undefined) {
    search.set("limit", String(params.limit));
  }
  if (params.status !== undefined) {
    search.set("status", params.status);
  }
  if (params.beforeId !== undefined) {
    search.set("before_id", String(params.beforeId));
  }
  const query = search.toString();
  return apiRequest<NotificationsResponse>(
    `/api/notifications${query ? `?${query}` : ""}`,
  );
}

/** Mark one visible notification read; the server operation is idempotent. */
export function markNotificationRead(
  notificationId: number,
): Promise<NotificationItem> {
  return apiRequest<NotificationItem>("/api/notifications/read", {
    method: "POST",
    body: JSON.stringify({ notification_id: notificationId }),
  });
}

/** Mark every currently visible unread notification read in one request. */
export function markAllNotificationsRead(): Promise<MarkAllNotificationsReadResponse> {
  return apiRequest<MarkAllNotificationsReadResponse>(
    "/api/notifications/read-all",
    { method: "POST" },
  );
}

/**
 * List personal preferences for the server-selected active Vault.
 * Owned by the dedicated notification-preferences surface (#226), not the inbox.
 */
export function fetchVaultNotificationPreferences(): Promise<VaultNotificationPreferencesResponse> {
  return apiRequest<VaultNotificationPreferencesResponse>(
    "/api/vault/notification-preferences",
  );
}

/**
 * Save one personal preference for the server-selected active Vault.
 * Owned by the dedicated notification-preferences surface (#226), not the inbox.
 */
export function setVaultNotificationPreference(
  payload: VaultNotificationPreferencePayload,
): Promise<VaultNotificationPreference> {
  return apiRequest<VaultNotificationPreference>(
    "/api/vault/notification-preferences",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

// Short aliases keep the helper names discoverable for callers that do not
// need to repeat the Vault scope; both use the same authenticated routes.
export const fetchNotificationPreferences = fetchVaultNotificationPreferences;
export const setNotificationPreference = setVaultNotificationPreference;

/** Fetch the newest bounded Vault-visible audit window; this endpoint has no filters. */
export function fetchVaultAuditEvents(): Promise<VaultAuditEventsResponse> {
  return apiRequest<VaultAuditEventsResponse>("/api/audit-events");
}

/** Fetch the newest bounded global audit window; this endpoint has no filters. */
export function fetchAdminAuditEvents(): Promise<AdminAuditEventsResponse> {
  return apiRequest<AdminAuditEventsResponse>("/api/admin/audit-events");
}

export function fetchAdminWorkerErrors(): Promise<AdminWorkerErrorsResponse> {
  return apiRequest<AdminWorkerErrorsResponse>("/api/admin/worker-errors");
}

export function fetchAdminMetadataBackups(): Promise<MetadataBackupsResponse> {
  return apiRequest<MetadataBackupsResponseWire>(
    "/api/admin/metadata-backups",
  ).then((response) => ({
    status: {
      ...response.status,
      last_run: response.status.last_run
        ? safeMetadataBackupRun(response.status.last_run)
        : null,
    },
    runs: (response.runs ?? []).map(safeMetadataBackupRun),
  }));
}

export function runAdminMetadataBackup(
  payload: MetadataBackupRunAction = { reason: "operator requested backup" },
): Promise<MetadataBackupRunResult> {
  return apiRequest<MetadataBackupRunResultWire>(
    "/api/admin/metadata-backups/run",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  ).then(safeMetadataBackupResult);
}

export function downloadAdminMetadataBackup(
  runId: number,
): Promise<MetadataBackupDownload> {
  if (!Number.isSafeInteger(runId) || runId < 1) {
    return Promise.reject(new TypeError("Metadata backup run ID must be positive"));
  }
  return apiDownload(
    `/api/admin/metadata-backups/download/${encodeURIComponent(String(runId))}`,
    {},
    `metadata-backup-${runId}.bak.enc`,
  );
}

export function estimateAdminStorageCost(
  payload: StorageEstimateRequest,
): Promise<StorageEstimateResponse> {
  return apiRequest<StorageEstimateResponse>("/api/admin/cost-estimates/storage", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateSystemSettings(
  payload: SystemSettingsUpdatePayload,
): Promise<SystemSettingsResponse> {
  return apiRequest<SystemSettingsResponse>("/api/admin/settings", {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function fetchAdminVaults(): Promise<AdminVaultsResponse> {
  return apiRequest<AdminVaultsResponse>("/api/admin/vaults");
}

export function relocateAdminVault(
  vaultId: number,
  payload: AdminVaultRelocatePayload,
): Promise<AdminVaultRelocationResponse> {
  return apiRequest<AdminVaultRelocationResponse>(
    `/api/admin/vaults/${vaultId}/relocate`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function previewOwnVaultDecommission(
  vaultId: number,
  payload: VaultDecommissionSelection,
): Promise<VaultDecommissionPreview> {
  return apiRequest<VaultDecommissionPreview>(
    `/api/vaults/${vaultId}/decommission/preview`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function startOwnVaultDecommission(
  vaultId: number,
  payload: VaultDecommissionStartPayload,
): Promise<VaultDecommissionStatus> {
  return apiRequest<VaultDecommissionStatus>(`/api/vaults/${vaultId}/decommission`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function fetchOwnVaultDecommissionStatus(
  vaultId: number,
): Promise<VaultDecommissionStatus> {
  return apiRequest<VaultDecommissionStatus>(
    `/api/vaults/${vaultId}/decommission/status`,
  );
}

export function cancelOwnVaultDecommissionCloudPurge(
  vaultId: number,
): Promise<VaultDecommissionStatus> {
  return apiRequest<VaultDecommissionStatus>(
    `/api/vaults/${vaultId}/decommission/cloud-purge/cancel`,
    { method: "POST", body: "{}" },
  );
}

export function requestScan(): Promise<ScanResponse> {
  return apiRequest<ScanResponse>("/api/scan", {
    method: "POST",
  });
}

export function fetchStats(): Promise<StatsResponse> {
  return apiRequest<StatsResponse>("/api/stats");
}

export function fetchJobs(): Promise<JobsResponse> {
  return apiRequest<JobsResponse>("/api/jobs");
}

export function fetchFileVersions(path: string): Promise<FileVersionsResponse> {
  return apiRequest<FileVersionsResponse>(
    `/api/files/versions?path=${encodeURIComponent(path)}`,
  );
}

export function estimateRecover(payload: {
  path: string;
  archive_version_id?: string;
  restore_tier?: string;
  restore_days?: number;
}): Promise<RecoverEstimateResponse> {
  return apiRequest<RecoverEstimateResponse>("/api/recover/estimate", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function startUpload(
  payload: FileOperationPayload,
): Promise<FileOperationResponse> {
  return apiRequest<FileOperationResponse>("/api/upload", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function startRecover(
  payload: FileOperationPayload,
): Promise<FileOperationResponse> {
  return apiRequest<FileOperationResponse>("/api/recover", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function approveRecover(
  groupId: string,
): Promise<FileOperationResponse> {
  return apiRequest<FileOperationResponse>("/api/recover/approve", {
    method: "POST",
    body: JSON.stringify({ group_id: groupId }),
  });
}

export function startFreeSpace(
  payload: FileOperationPayload,
): Promise<FileOperationResponse> {
  return apiRequest<FileOperationResponse>("/api/free-space", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function startCloudArchive(
  payload: FileOperationPayload,
): Promise<FileOperationResponse> {
  return apiRequest<FileOperationResponse>("/api/cloud-archive", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function startStorageClass(payload: {
  path: string;
  is_directory?: boolean;
  whole_vault?: boolean;
  target_storage_class: string;
  archive_version_id?: string | null;
  restore_tier?: string;
  restore_days?: number;
  pin_after?: boolean;
}): Promise<FileOperationResponse> {
  return apiRequest<FileOperationResponse>("/api/storage-class", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function fetchStorageClasses(): Promise<StorageClassesResponse> {
  return apiRequest<StorageClassesResponse>("/api/storage-classes");
}

export function updateLifecyclePin(payload: {
  path: string;
  is_directory?: boolean;
  pinned: boolean;
}): Promise<{ message?: string; pinned: boolean; path: string }> {
  return apiRequest("/api/lifecycle-pin", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function previewCloudDeletion(payload: {
  path: string;
  is_directory?: boolean;
}): Promise<CloudDeletionPreview> {
  return apiRequest<CloudDeletionPreview>("/api/cloud-deletion/preview", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function startCloudPurge(
  payload: CloudPurgePayload,
): Promise<FileOperationResponse> {
  return apiRequest<FileOperationResponse>("/api/cloud-purge", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function accelerateCloudPurge(
  groupId: string,
): Promise<FileOperationResponse> {
  return apiRequest<FileOperationResponse>("/api/cloud-purge/accelerate", {
    method: "POST",
    body: JSON.stringify({ group_id: groupId }),
  });
}

export function cancelJob(
  payload: JobCancelPayload,
): Promise<JobCancelResponse> {
  return apiRequest<JobCancelResponse>("/api/jobs/cancel", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** Legacy upload-cancel route — prefer cancelJob for all actions. */
export function cancelUpload(groupId: string): Promise<JobCancelResponse> {
  return apiRequest<JobCancelResponse>("/api/upload/cancel", {
    method: "POST",
    body: JSON.stringify({ group_id: groupId }),
  });
}

export { DEFAULT_PAGE_SIZE };

export function logout(): Promise<{ message: string; message_key: string }> {
  return apiRequest<{ message: string; message_key: string }>("/api/logout", {
    method: "POST",
    body: "{}",
  });
}

export function fetchAdminSourceVolumes(): Promise<SourceVolumeInventoryResponse> {
  return apiRequest<SourceVolumeInventoryResponse>("/api/admin/source-volumes");
}

export function browseSourceVolume(
  volumeAlias: string,
  path = "",
): Promise<SourceDirectoryBrowseResponse> {
  const query = new URLSearchParams();
  if (path) query.set("path", path);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiRequest<SourceDirectoryBrowseResponse>(
    `/api/source-volumes/${encodeURIComponent(volumeAlias)}/browse${suffix}`,
  );
}
