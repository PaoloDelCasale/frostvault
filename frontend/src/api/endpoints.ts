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
  ScanResponse,
  StatsResponse,
  StorageClassesResponse,
  StorageEstimateRequest,
  StorageEstimateResponse,
  VaultCreateRequest,
  VaultCreateResponse,
  VaultSelectRequest,
  VaultSelectResponse,
  VaultsResponse,
} from "./types";
import { translate } from "@/i18n/translate";

const DEFAULT_PAGE_SIZE = 100;

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
