import {
  apiDownload,
  apiRequest,
  configureApiClient,
  setCsrfToken,
} from "./client";
import type { ApiDownload } from "./client";
import type {
  AdminIdentitiesResponse,
  AdminInvitesResponse,
  AdminMembershipCreatePayload,
  AdminOwnerTransferPayload,
  AdminUser,
  AdminUserCreatePayload,
  AdminUsersResponse,
  AdminUserUpdatePayload,
  AdminVault,
  AdminVaultCreatePayload,
  AdminVaultMembersResponse,
  AdminVaultRelocatePayload,
  AdminVaultRelocationResponse,
  AdminVaultsResponse,
  CostPriceBookActivate,
  CostPriceBookCreate,
  VaultDecommissionPreview,
  VaultDecommissionSelection,
  VaultDecommissionStartPayload,
  VaultDecommissionStatus,
  SourceVolumeInventoryResponse,
  SourceAreaAssignPayload,
  SourceAreaGrant,
  SourceAreaListResponse,
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
  OidcConfigurationResponse,
  OidcDraftPayload,
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
  UserLookupResult,
  VaultCreateRequest,
  VaultCreateResponse,
  VaultMembersResponse,
  VaultQuotaUpdatePayload,
  VaultQuotasResponse,
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
  decision: "auto" | "ambiguous" | string;
  size?: number | null;
};

export type RenameCandidatesResponse = { items: RenameCandidate[] };
export type RenameConfirmationResponse = Record<string, unknown>;

/** Response shape emitted by the admin price-book endpoints. */
export type CostPriceBook = {
  id: number | null;
  name: string;
  currency: string;
  effective_at: string;
  updated_at: string | null;
  assumptions: Record<string, unknown>;
  storage_rates: Record<string, number>;
  restore_rates: Record<string, Record<string, number>>;
  is_active: boolean;
};

export type CostPriceBooksResponse = { items: CostPriceBook[] };
export type CostPriceBookCreatePayload = CostPriceBookCreate;
export type CostPriceBookActivatePayload = CostPriceBookActivate;

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

export function fetchFiles(query: FilesQuery = {}): Promise<FilesResponse> {
  const params = new URLSearchParams();
  params.set("q", query.q ?? "");
  params.set("state", query.state ?? "");
  params.set("directory", query.directory ?? "");
  params.set("page", String(query.page ?? 1));
  params.set("page_size", String(query.page_size ?? DEFAULT_PAGE_SIZE));
  return apiRequest<FilesResponse>(`/api/files?${params.toString()}`);
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

export function lookupVaultUser(username: string): Promise<UserLookupResult> {
  return apiRequest<UserLookupResult>("/api/vault/user-lookup", {
    method: "POST",
    body: JSON.stringify({ username }),
  });
}

export function fetchVaultMembers(): Promise<VaultMembersResponse> {
  return apiRequest<VaultMembersResponse>("/api/vault/members");
}

export function addVaultMember(
  userId: number,
  role: string,
): Promise<{ message: string }> {
  return apiRequest<{ message: string }>("/api/vault/members", {
    method: "POST",
    body: JSON.stringify({ user_id: userId, role }),
  });
}

export function removeVaultMember(userId: number): Promise<{ message: string }> {
  return apiRequest<{ message: string }>(`/api/vault/members/${userId}`, {
    method: "DELETE",
  });
}

export function transferVaultOwner(
  newOwnerUserId: number,
): Promise<{ message: string }> {
  return apiRequest<{ message: string }>("/api/vault/transfer-owner", {
    method: "POST",
    body: JSON.stringify({ new_owner_user_id: newOwnerUserId }),
  });
}

export function fetchVaultQuotas(): Promise<VaultQuotasResponse> {
  return apiRequest<VaultQuotasResponse>("/api/vault/quotas");
}

export function updateAdminVaultQuotas(
  vaultId: number,
  payload: VaultQuotaUpdatePayload,
): Promise<VaultQuotasResponse> {
  return apiRequest<VaultQuotasResponse>(`/api/admin/vaults/${vaultId}/quotas`, {
    method: "PUT",
    body: JSON.stringify(payload),
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

export function fetchOidcConfiguration(): Promise<OidcConfigurationResponse> {
  return apiRequest<OidcConfigurationResponse>("/api/admin/oidc-configuration");
}

export function saveOidcDraft(payload: OidcDraftPayload): Promise<OidcConfigurationResponse> {
  return apiRequest<OidcConfigurationResponse>("/api/admin/oidc-configuration/draft", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

export function validateOidcDraft(): Promise<OidcConfigurationResponse> {
  return apiRequest<OidcConfigurationResponse>("/api/admin/oidc-configuration/draft/validate", { method: "POST" });
}

export function activateOidcConfiguration(): Promise<OidcConfigurationResponse> {
  return apiRequest<OidcConfigurationResponse>("/api/admin/oidc-configuration/activate", { method: "POST" });
}

export function disableOidcConfiguration(): Promise<OidcConfigurationResponse> {
  return apiRequest<OidcConfigurationResponse>("/api/admin/oidc-configuration/disable", { method: "POST" });
}

export function rotateOidcSecret(clientSecret: string): Promise<OidcConfigurationResponse> {
  return apiRequest<OidcConfigurationResponse>("/api/admin/oidc-configuration/rotate-secret", {
    method: "POST",
    body: JSON.stringify({ client_secret: clientSecret }),
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

export function fetchAdminCostPriceBooks(): Promise<CostPriceBooksResponse> {
  return apiRequest<CostPriceBooksResponse>("/api/admin/cost-price-books");
}

export function estimateAdminStorageCost(
  payload: StorageEstimateRequest,
): Promise<StorageEstimateResponse> {
  return apiRequest<StorageEstimateResponse>("/api/admin/cost-estimates/storage", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function fetchActiveAdminCostPriceBook(): Promise<CostPriceBook> {
  return apiRequest<CostPriceBook>("/api/admin/cost-price-books/active");
}

export function createAdminCostPriceBook(
  payload: CostPriceBookCreatePayload,
): Promise<CostPriceBook> {
  return apiRequest<CostPriceBook>("/api/admin/cost-price-books", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function activateAdminCostPriceBook(
  priceBookId: number,
  payload: CostPriceBookActivatePayload,
): Promise<CostPriceBook> {
  return apiRequest<CostPriceBook>(
    `/api/admin/cost-price-books/${priceBookId}/activate`,
    {
      method: "POST",
      body: JSON.stringify(payload),
    },
  );
}

export function updateSystemSettings(
  payload: SystemSettingsUpdatePayload,
): Promise<SystemSettingsResponse> {
  return apiRequest<SystemSettingsResponse>("/api/admin/settings", {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function fetchAdminUsers(): Promise<AdminUsersResponse> {
  return apiRequest<AdminUsersResponse>("/api/admin/users");
}

export function createAdminUser(
  payload: AdminUserCreatePayload,
): Promise<AdminUser> {
  return apiRequest<AdminUser>("/api/admin/users", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function updateAdminUser(
  userId: number,
  payload: AdminUserUpdatePayload,
): Promise<AdminUser> {
  return apiRequest<AdminUser>(`/api/admin/users/${userId}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export function fetchAdminInvites(): Promise<AdminInvitesResponse> {
  return apiRequest<AdminInvitesResponse>("/api/admin/invites");
}

export function createAdminInvite(targetUserId: number): Promise<{ token: string }> {
  return apiRequest<{ token: string }>("/api/admin/invites", {
    method: "POST",
    body: JSON.stringify({ target_user_id: targetUserId }),
  });
}

export function revokeAdminInvite(inviteId: number): Promise<unknown> {
  return apiRequest(`/api/admin/invites/${inviteId}/revoke`, { method: "POST" });
}

export function fetchAdminIdentities(
  userId: number,
): Promise<AdminIdentitiesResponse> {
  return apiRequest<AdminIdentitiesResponse>(
    `/api/admin/users/${userId}/identities`,
  );
}

export function unlinkAdminIdentity(
  userId: number,
  identityId: number,
): Promise<AdminIdentitiesResponse> {
  return apiRequest<AdminIdentitiesResponse>(
    `/api/admin/users/${userId}/identities/${identityId}?confirm=true`,
    { method: "DELETE" },
  );
}

export function fetchAdminVaults(): Promise<AdminVaultsResponse> {
  return apiRequest<AdminVaultsResponse>("/api/admin/vaults");
}

export function createAdminVault(
  payload: AdminVaultCreatePayload,
): Promise<AdminVault> {
  return apiRequest<AdminVault>("/api/admin/vaults", {
    method: "POST",
    body: JSON.stringify(payload),
  });
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

export function previewAdminVaultDecommission(
  vaultId: number,
  payload: VaultDecommissionSelection,
): Promise<VaultDecommissionPreview> {
  return apiRequest<VaultDecommissionPreview>(
    `/api/admin/vaults/${vaultId}/decommission/preview`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function startAdminVaultDecommission(
  vaultId: number,
  payload: VaultDecommissionStartPayload,
): Promise<VaultDecommissionStatus> {
  return apiRequest<VaultDecommissionStatus>(
    `/api/admin/vaults/${vaultId}/decommission`,
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function fetchAdminVaultDecommissionStatus(
  vaultId: number,
): Promise<VaultDecommissionStatus> {
  return apiRequest<VaultDecommissionStatus>(
    `/api/admin/vaults/${vaultId}/decommission/status`,
  );
}

export function cancelAdminVaultDecommissionCloudPurge(
  vaultId: number,
): Promise<VaultDecommissionStatus> {
  return apiRequest<VaultDecommissionStatus>(
    `/api/admin/vaults/${vaultId}/decommission/cloud-purge/cancel`,
    { method: "POST", body: "{}" },
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

export function fetchAdminVaultMembers(
  vaultId: number,
): Promise<AdminVaultMembersResponse> {
  return apiRequest<AdminVaultMembersResponse>(
    `/api/admin/vaults/${vaultId}/members`,
  );
}

export function addAdminVaultMember(
  vaultId: number,
  payload: AdminMembershipCreatePayload,
): Promise<unknown> {
  return apiRequest(`/api/admin/vaults/${vaultId}/members`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function removeAdminVaultMember(
  vaultId: number,
  userId: number,
  reason: string,
): Promise<unknown> {
  const query = new URLSearchParams({ reason });
  return apiRequest(
    `/api/admin/vaults/${vaultId}/members/${userId}?${query}`,
    { method: "DELETE" },
  );
}

export function fetchAdminVaultQuotas(
  vaultId: number,
): Promise<VaultQuotasResponse> {
  return apiRequest<VaultQuotasResponse>(
    `/api/admin/vaults/${vaultId}/quotas`,
  );
}

export function transferAdminVaultOwner(
  vaultId: number,
  payload: AdminOwnerTransferPayload,
): Promise<unknown> {
  return apiRequest(`/api/admin/vaults/${vaultId}/transfer-owner`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function exportAdminVaultRecovery(
  vaultId: number,
  reason: string,
): Promise<RecoveryExportResponse> {
  return apiRequest<RecoveryExportResponse>(
    `/api/admin/vaults/${vaultId}/recovery/export`,
    {
      method: "POST",
      body: JSON.stringify({ reason }),
    },
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

export function fetchAdminSourceAreas(params?: {
  userId?: number;
  volumeAlias?: string;
}): Promise<SourceAreaListResponse> {
  const query = new URLSearchParams();
  if (params?.userId != null) query.set("user_id", String(params.userId));
  if (params?.volumeAlias) query.set("volume_alias", params.volumeAlias);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiRequest<SourceAreaListResponse>(`/api/admin/source-areas${suffix}`);
}

export function assignAdminSourceArea(
  payload: SourceAreaAssignPayload,
): Promise<SourceAreaGrant> {
  return apiRequest<SourceAreaGrant>("/api/admin/source-areas", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function revokeAdminSourceArea(
  sourceAreaId: number,
  reason: string,
): Promise<SourceAreaGrant> {
  const query = new URLSearchParams({ reason });
  return apiRequest<SourceAreaGrant>(
    `/api/admin/source-areas/${sourceAreaId}?${query.toString()}`,
    { method: "DELETE" },
  );
}

export function browseAdminSourceVolume(
  volumeAlias: string,
  path = "",
  purpose: "grant" | "adopt" = "grant",
): Promise<SourceDirectoryBrowseResponse> {
  const query = new URLSearchParams();
  if (path) query.set("path", path);
  if (purpose !== "grant") query.set("purpose", purpose);
  const suffix = query.toString() ? `?${query.toString()}` : "";
  return apiRequest<SourceDirectoryBrowseResponse>(
    `/api/admin/source-volumes/${encodeURIComponent(volumeAlias)}/browse${suffix}`,
  );
}

export function fetchMySourceAreas(): Promise<SourceAreaListResponse> {
  return apiRequest<SourceAreaListResponse>("/api/source-areas");
}

export function browseMySourceVolume(
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
