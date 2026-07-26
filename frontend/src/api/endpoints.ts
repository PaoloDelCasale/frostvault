import { apiRequest, configureApiClient, setCsrfToken } from "./client";
import type {
  AdminMembershipCreatePayload,
  AdminOwnerTransferPayload,
  AdminUser,
  AdminUserCreatePayload,
  AdminUsersResponse,
  AdminUserUpdatePayload,
  AdminVault,
  AdminVaultCreatePayload,
  AdminVaultMembersResponse,
  AdminVaultsResponse,
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
  LifecycleResponse,
  LocaleUpdateResponse,
  MeResponse,
  OperationPolicy,
  RecoverEstimateResponse,
  RecoveryConfirmRequest,
  RecoveryConfirmResponse,
  RecoveryExportRequest,
  RecoveryExportResponse,
  StatsResponse,
  StorageClassesResponse,
  UserLookupResult,
  VaultCreateRequest,
  VaultCreateResponse,
  VaultMembersResponse,
  VaultQuotaUpdatePayload,
  VaultQuotasResponse,
  VaultSelectRequest,
  VaultSelectResponse,
  VaultsResponse,
} from "./types";
import { translate } from "@/i18n/translate";

const DEFAULT_PAGE_SIZE = 100;

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
  guidedProfile: string,
): Promise<LifecycleResponse> {
  return apiRequest<LifecycleResponse>("/api/vault/lifecycle/default", {
    method: "PUT",
    body: JSON.stringify({ guided_profile: guidedProfile }),
  });
}

export function upsertLifecycleFolderOverride(
  folderPath: string,
  guidedProfile: string,
): Promise<LifecycleResponse> {
  return apiRequest<LifecycleResponse>("/api/vault/lifecycle/folder-overrides", {
    method: "PUT",
    body: JSON.stringify({
      folder_path: folderPath,
      guided_profile: guidedProfile,
    }),
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
