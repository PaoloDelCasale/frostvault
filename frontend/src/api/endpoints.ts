import {
  apiDownload,
  apiRequest,
  configureApiClient,
  setCsrfToken,
} from "./client";
import type { ApiDownload } from "./client";
import type {
  BillingExportStatus,
  FilesResponse,
  GeneralOverview,
  GeneralVaultsResponse,
  I18nCatalogResponse,
  LocaleUpdateResponse,
  LoginOptionsResponse,
  MeResponse,
  MetadataBackupRunAction,
  MetadataBackupVerification,
  NotificationItem,
  NotificationsResponse,
  RecoveryKitStatus,
  RetryFailedUploadsResponse,
  SourceDirectoryBrowseResponse,
  SourceVolumeInventoryResponse,
  UploadActivityResponse,
  VaultCreateResponse,
  VaultSelectResponse,
  VaultSelection,
  VaultSelfServiceCreate,
  VaultsResponse,
} from "./types";
import { translate } from "@/i18n/translate";

export type NotificationListStatus = "unread" | "read" | "all";

export type FetchNotificationsParams = {
  limit?: number;
  status?: NotificationListStatus;
  beforeId?: number;
};

export type MarkAllNotificationsReadResponse = {
  marked_count: number;
  unread_count: number;
};

/**
 * The metadata-backup, mark-read and sign-out routes below answer with an
 * untyped JSON object in the OpenAPI document, so their shapes are written
 * here; every other response type comes from the generated schema.
 *
 * Safe metadata-backup fields exposed to the SPA; filesystem paths are omitted.
 */
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
  /** Bucket of the off-host copy; null for local-only and pre-#734 runs. */
  s3_bucket?: string | null;
  size_bytes: number | null;
  error_message: string | null;
  verified_at: string | null;
};

/** The installation's System Bucket for metadata backups (#734). */
export type SystemBucket = {
  name: string;
  region: string;
  origin: "configured" | "created";
  ready: boolean;
};

export type MetadataBackupStatus = {
  last_status: string;
  last_run: MetadataBackupRun | null;
  succeeded_count: number;
  failed_count: number;
  /** Last verified backup and next automatic verification (#648). */
  verification?: MetadataBackupVerification;
  /** Where off-host copies go; null while there is none (#734). */
  system_bucket?: SystemBucket | null;
  /** Whether backups must also reach the System Bucket. */
  off_host?: boolean;
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
  payload: VaultSelfServiceCreate,
): Promise<VaultCreateResponse> {
  return apiRequest<VaultCreateResponse>("/api/vaults", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function selectVault(
  payload: VaultSelection,
): Promise<VaultSelectResponse> {
  return apiRequest<VaultSelectResponse>("/api/vaults/select", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** Public sign-in choices for the login page (no Session required). */
export function fetchLoginOptions(): Promise<LoginOptionsResponse> {
  return apiRequest<LoginOptionsResponse>("/api/login/options");
}

export function fetchI18nCatalog(locale?: string): Promise<I18nCatalogResponse> {
  const query = locale ? `?locale=${encodeURIComponent(locale)}` : "";
  return apiRequest<I18nCatalogResponse>(`/api/i18n/catalog${query}`).then(
    (catalog) => {
      configureApiClient({
        translate: (key, params) => translate(catalog.messages, key, params, catalog.locale),
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

export function fetchGeneralMetadataBackups(): Promise<MetadataBackupsResponse> {
  return apiRequest<MetadataBackupsResponseWire>(
    "/api/general/metadata-backups",
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

export function runGeneralMetadataBackup(
  payload: MetadataBackupRunAction = { reason: "operator requested backup" },
): Promise<MetadataBackupRunResult> {
  return apiRequest<MetadataBackupRunResultWire>(
    "/api/general/metadata-backups/run",
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  ).then(safeMetadataBackupResult);
}

export function downloadGeneralMetadataBackup(
  runId: number,
): Promise<MetadataBackupDownload> {
  if (!Number.isSafeInteger(runId) || runId < 1) {
    return Promise.reject(new TypeError("Metadata backup run ID must be positive"));
  }
  return apiDownload(
    `/api/general/metadata-backups/download/${encodeURIComponent(String(runId))}`,
    {},
    `metadata-backup-${runId}.bak.enc`,
  );
}

export function fetchRecoveryKitStatus(): Promise<RecoveryKitStatus> {
  return apiRequest<RecoveryKitStatus>("/api/recovery-kit");
}

export function downloadRecoveryKit(): Promise<ApiDownload> {
  return apiDownload("/api/recovery-kit/download", {}, "frostvault-recovery-kit.json");
}

export function acknowledgeRecoveryKit(fingerprint: string): Promise<RecoveryKitStatus> {
  return apiRequest<RecoveryKitStatus>("/api/recovery-kit/custody", {
    method: "POST", body: JSON.stringify({ fingerprint }),
  });
}

/** The installation at a glance: every Vault's estimate and open Problems. */
export function fetchOverview(): Promise<GeneralOverview> {
  return apiRequest<GeneralOverview>("/api/general");
}

/** One page of open Problems across every Vault. */
export function fetchProblems(page: number): Promise<NotificationsResponse> {
  return apiRequest<NotificationsResponse>(`/api/problems?page=${page}`);
}

/** Files by name across every Vault the Owner can see; a Vault's own list is `vaultApi(id).files`. */
export function searchFiles(params: URLSearchParams): Promise<FilesResponse> {
  return apiRequest<FilesResponse>(`/api/files/search?${params}`);
}

/** Installation-wide upload activity. */
export function fetchUploadActivity(): Promise<UploadActivityResponse> {
  return apiRequest<UploadActivityResponse>("/api/upload-activity");
}

/** Retries the failed Uploads of every Vault; one Vault's are `vaultApi(id).retryFailedUploads`. */
export function retryFailedUploads(): Promise<RetryFailedUploadsResponse> {
  return apiRequest<RetryFailedUploadsResponse>("/api/upload/retry-failed", { method: "POST" });
}

export function fetchGeneralVaults(): Promise<GeneralVaultsResponse> {
  return apiRequest<GeneralVaultsResponse>("/api/general/vaults");
}

/** The billing export's status and every known bucket's Billed Cost (#738). */
export function fetchBillingExport(): Promise<BillingExportStatus> {
  return apiRequest<BillingExportStatus>("/api/billing-export");
}

export function logout(): Promise<{ message: string; message_key: string }> {
  return apiRequest<{ message: string; message_key: string }>("/api/logout", {
    method: "POST",
    body: "{}",
  });
}

export function fetchGeneralSourceVolumes(): Promise<SourceVolumeInventoryResponse> {
  return apiRequest<SourceVolumeInventoryResponse>("/api/general/source-volumes");
}

export function browseSourceVolume(
  volumeAlias: string,
  path = "",
  reconnectVaultId?: number,
): Promise<SourceDirectoryBrowseResponse> {
  const query = new URLSearchParams();
  if (path) query.set("path", path);
  if (reconnectVaultId !== undefined) query.set("reconnect_vault_id", String(reconnectVaultId));
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiRequest<SourceDirectoryBrowseResponse>(
    `/api/source-volumes/${encodeURIComponent(volumeAlias)}/browse${suffix}`,
  );
}
