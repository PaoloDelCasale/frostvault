import { apiRequest } from "./client";
import type {
  ArchiveListItem,
  BulkUploadAdmissionResponse,
  CloudDeletionPreview,
  CloudDeletionSettings,
  DuplicateCleanupStatus,
  DuplicateCleanupUpdate,
  ExcludedFolderListing,
  FileJobHistoryResponse,
  FileOperationPayload,
  FileOperationResponse,
  FileVersionsResponse,
  FilesResponse,
  GeneralVaultReconnectPayload,
  GeneralVaultReconnectResponse,
  GlobPreviewRequest,
  GlobPreviewResponse,
  JobCancelResponse,
  JobRecord,
  JsonObjectResponse,
  JobsResponse,
  LifecycleProfileSelection,
  LifecycleResponse,
  MinimumStorageSummary,
  MirrorCleanupStatus,
  MirrorCleanupUpdate,
  OperationPolicy,
  OperationPolicyGlobEdit,
  RecoverEstimateResponse,
  RetentionLockStatus,
  RetryFailedUploadsResponse,
  StatsResponse,
  StopProtectionPayload,
  StopProtectionStatus,
  UploadReconciliationResponse,
  VaultBilledCost,
  VaultBucketStatus,
  VaultCloudUsage,
  VaultCreationCancelled,
  VaultDeletionPreview,
  VaultDeletionRequest,
  VaultDeletionStatus,
  VaultInclusionPreview,
  VaultMovePreview,
  VaultMoveRequest,
  VaultMoveResponse,
  VaultPauseResponse,
} from "./types";

/**
 * Every request bound to one Vault (ADR-0022). The identity is mandatory and
 * immutable: it never reads or changes the Session's selection, and the URL,
 * including its scope, survives CSRF/reauth retries unchanged.
 *
 * The server names the Vault in two ways, both built only here: a `vault_id`
 * query parameter (`scoped`) or a `/api/vaults/{id}/…` path (`owned`).
 */
export function vaultApi(vaultId: number) {
  if (!Number.isSafeInteger(vaultId) || vaultId < 1) throw new Error("Invalid Vault identity");
  const scoped = (path: string) => `${path}${path.includes("?") ? "&" : "?"}vault_id=${vaultId}`;
  const owned = (suffix: string) => `/api/vaults/${vaultId}/${suffix}`;
  const get = <T>(url: string) => apiRequest<T>(url);
  const send = <T>(method: "POST" | "PUT", url: string, body?: unknown) =>
    apiRequest<T>(url, body === undefined ? { method } : { method, body: JSON.stringify(body) });
  /** These routes read a JSON body even when it carries nothing. */
  const sendEmpty = <T>(url: string) => apiRequest<T>(url, { method: "POST", body: "{}" });
  return {
    stats: () => get<StatsResponse>(owned("stats")),

    files: (params: URLSearchParams) => get<FilesResponse>(owned(`files?${params}`)),
    details: (path: string) => get<ArchiveListItem>(owned(`files/detail?path=${encodeURIComponent(path)}`)),
    versions: (path: string) => get<FileVersionsResponse>(scoped(`/api/files/versions?path=${encodeURIComponent(path)}`)),
    fileJobs: (path: string, beforeId?: number) => get<FileJobHistoryResponse>(scoped(`/api/files/jobs?path=${encodeURIComponent(path)}${beforeId ? `&before_id=${beforeId}` : ""}`)),
    replaceCloudOnlyConflict: (path: string) => send<JsonObjectResponse>("POST", scoped("/api/files/cloud-only-conflict/replace"), { path }),

    policy: () => get<OperationPolicy>(owned("operation-policy")),
    updatePolicy: (policy: Partial<OperationPolicy>) => send<OperationPolicy>("PUT", owned("operation-policy"), policy),
    /** One level read from disk: Excluded Folders, or the entries inside one (#754). */
    excludedFolders: (directory: string) => get<ExcludedFolderListing>(owned(`excluded-folders?directory=${encodeURIComponent(directory)}`)),
    /** Adds or removes one glob against the stored policy; never replaces a list. */
    editGlob: (edit: OperationPolicyGlobEdit) => send<OperationPolicy>("POST", owned("operation-policy/globs"), edit),
    previewGlobs: (payload: GlobPreviewRequest) => send<GlobPreviewResponse>("POST", owned("operation-policy/preview-globs"), payload),
    inclusionPreview: (path: string, isDirectory: boolean) => get<VaultInclusionPreview>(owned(`exclusions/inclusion-preview?path=${encodeURIComponent(path)}&is_directory=${isDirectory}`)),
    exclude: (path: string, isDirectory: boolean) => send<OperationPolicy>("POST", owned("exclusions/exclude"), { path, is_directory: isDirectory }),
    include: (path: string, isDirectory: boolean) => send<OperationPolicy>("POST", owned("exclusions/include"), { path, is_directory: isDirectory }),

    lifecycle: () => get<LifecycleResponse>(owned("lifecycle")),
    /** A guided profile by name, or a custom selection. */
    updateLifecycle: (selection: string | LifecycleProfileSelection) => send<LifecycleResponse>("PUT", owned("lifecycle"),
      typeof selection === "string" ? { guided_profile: selection } : selection),
    move: () => get<VaultMovePreview>(owned("move")),
    startMove: (payload: VaultMoveRequest) => send<VaultMoveResponse>("POST", owned("move"), payload),

    jobs: (options: { actions?: string[]; activeOnly?: boolean; path?: string; groupsBeforeId?: number } = {}) => {
      const params = new URLSearchParams();
      options.actions?.forEach((action) => params.append("action", action));
      if (options.activeOnly) params.set("active_only", "true");
      if (options.path !== undefined) params.set("path", options.path);
      if (options.groupsBeforeId) params.set("groups_before_id", String(options.groupsBeforeId));
      return get<JobsResponse>(scoped(`/api/jobs?${params}`));
    },
    groupJobs: (groupId: string, beforeId?: number) => get<FileJobHistoryResponse>(scoped(`/api/job-groups/${encodeURIComponent(groupId)}/jobs${beforeId ? `?before_id=${beforeId}` : ""}`)),
    job: (id: number) => get<JobRecord>(scoped(`/api/jobs/${id}`)),
    /** Cancel one group of Jobs of `action`, e.g. a pending Delayed Cloud Deletion before it starts. */
    cancelJobGroup: (groupId: string | number, action: string) => send<JobCancelResponse>("POST", scoped("/api/jobs/cancel"), { group_id: groupId, action }),
    /** Restore Approval of one held recover request (#533). */
    approveRecover: (groupId: string) => send<JsonObjectResponse>("POST", scoped("/api/recover/approve"), { group_id: groupId }),

    estimate: (payload: unknown) => send<RecoverEstimateResponse>("POST", scoped("/api/recover/estimate"), payload),
    action: (action: "recover" | "free-space", payload: FileOperationPayload) => send<FileOperationResponse>("POST", scoped(`/api/${action}`), payload),

    /** Upload one item, or the whole Vault with `path: ""`; `request_id` makes a retry idempotent. */
    upload: (payload: { path: string; is_directory?: boolean; request_id?: string }) =>
      send<{ admission?: BulkUploadAdmissionResponse }>("POST", scoped("/api/upload"), payload),
    uploadAdmission: (groupId: string) => get<BulkUploadAdmissionResponse>(scoped(`/api/upload/admissions/${groupId}`)),
    reconcileUpload: (payload: { path: string; remediation_id?: string }) => send<UploadReconciliationResponse>("POST", scoped("/api/upload/reconcile"), payload),
    /** Cancels every Upload of the Vault still waiting to start. */
    cancelPendingUploads: () => send<JobCancelResponse>("POST", scoped("/api/upload/cancel-pending"), { path: "" }),
    retryFailedUploads: () => send<RetryFailedUploadsResponse>("POST", scoped("/api/upload/retry-failed")),

    cloudDeletionSettings: () => get<CloudDeletionSettings>(scoped("/api/cloud-deletion")),
    previewCloudDeletion: (payload: Record<string, unknown>) => send<CloudDeletionPreview>("POST", scoped("/api/cloud-deletion/preview"), payload),
    cloudPurge: (payload: Record<string, unknown>) => send<JsonObjectResponse>("POST", scoped("/api/cloud-purge"), payload),

    bucket: () => get<VaultBucketStatus>(owned("bucket")),
    retryBucket: () => send<VaultBucketStatus>("POST", owned("bucket/retry")),
    /** Cancel a Vault still being created (#735); a destructive action behind reauthentication. */
    cancelCreation: () => send<VaultCreationCancelled>("POST", owned("creation/cancel")),
    reconnect: (payload: GeneralVaultReconnectPayload) => send<GeneralVaultReconnectResponse>("POST", owned("reconnect"), payload),
    /** A hold on the Vault's work, independent of its protection state. */
    setPaused: (paused: boolean) => send<VaultPauseResponse>("POST", owned(paused ? "pause" : "resume")),

    stopProtection: () => get<StopProtectionStatus>(owned("stop-protection")),
    stop: (payload: StopProtectionPayload) => send<StopProtectionStatus>("POST", owned("stop-protection"), payload),
    cancelStoppedDeletion: () => sendEmpty<StopProtectionStatus>(owned("stop-protection/cancel-deletion")),

    /** Automatic Duplicate Cleanup (#726, ADR-0048). */
    duplicateCleanup: () => get<DuplicateCleanupStatus>(owned("duplicate-cleanup")),
    /** Turning it on or changing its wait and caps needs a fresh authentication. */
    updateDuplicateCleanup: (payload: DuplicateCleanupUpdate) => send<DuplicateCleanupStatus>("PUT", owned("duplicate-cleanup"), payload),
    /** Mirror Cleanup (#771, ADR-0052). */
    mirrorCleanup: () => get<MirrorCleanupStatus>(owned("mirror-cleanup")),
    /** Turning it on or making it delete more needs a fresh authentication. */
    updateMirrorCleanup: (payload: MirrorCleanupUpdate) => send<MirrorCleanupStatus>("PUT", owned("mirror-cleanup"), payload),

    /** Minimum Storage Duration of the Vault's versions (#731): a list-price estimate. */
    minimumStorage: () => get<MinimumStorageSummary>(owned("minimum-storage")),
    /** The catalog's Cost Estimate next to the CloudWatch sizes of the Vault Bucket (#737). */
    cloudUsage: () => get<VaultCloudUsage>(owned("cloud-usage")),
    /** The Vault's Billed Cost from the AWS billing export (#738), never a Cost Estimate. */
    billedCost: () => get<VaultBilledCost>(owned("billed-cost")),
    /** The Vault's Retention Lock and the versions it still holds back (#736). */
    retentionLock: () => get<RetentionLockStatus>(owned("retention-lock")),

    /** Vault Deletion of a Tombstone (#717, ADR-0047). */
    deletion: () => get<VaultDeletionStatus>(owned("vault-deletion")),
    /** A live listing of the Vault's whole prefix; fails when the listing fails. */
    previewDeletion: () => get<VaultDeletionPreview>(owned("vault-deletion/preview")),
    requestDeletion: (payload: VaultDeletionRequest) => send<VaultDeletionStatus>("POST", owned("vault-deletion"), payload),
    cancelDeletion: () => sendEmpty<VaultDeletionStatus>(owned("vault-deletion/cancel")),
    retryDeletion: () => sendEmpty<VaultDeletionStatus>(owned("vault-deletion/retry")),
  };
}

export type VaultApi = ReturnType<typeof vaultApi>;
