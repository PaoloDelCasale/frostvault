/** Hand-written response types for foundation endpoints (issue #60). */

export type VaultRole = "owner" | "operator" | "viewer";

export type AuthMethod = "oidc" | "local" | string | null;

export type MeVault = {
  id: number;
  slug: string;
  name: string;
  role: VaultRole;
  can_operate: boolean;
  delete_enabled: boolean;
  cloud_deletion_enabled: boolean;
  is_vault_owner: boolean;
};

export type MeDecommissionVault = {
  id: number;
  slug: string;
  name: string;
  decommission_state: "decommissioning" | "decommissioned";
  root_released_at?: string | null;
  root_released: boolean;
};

export type MeResponse = {
  id: number;
  username: string;
  display_name: string;
  is_admin: boolean;
  active: boolean;
  session_version: number;
  csrf_token: string;
  auth_method: AuthMethod;
  locale: string;
  locales: string[];
  vault: MeVault | null;
  decommission_vault?: MeDecommissionVault | null;
};

export type VaultListItem = {
  id: number;
  slug: string;
  name: string;
  role: VaultRole;
};

export type VaultsResponse = {
  items: VaultListItem[];
};

export type I18nCatalogResponse = {
  locale: string;
  locales: string[];
  messages: Record<string, string>;
};

export type LocaleUpdateResponse = {
  locale: string;
  message: string;
  message_key: string;
  messages: Record<string, string>;
};

export type EncryptionMode = "plain" | "crypt";

export type VaultCreationMode = "empty" | "adopt";

export type VaultCreateRequest = {
  name: string;
  slug?: string;
  encryption_mode: EncryptionMode;
  creation_mode?: VaultCreationMode;
  volume_alias?: string;
  relative_path?: string;
};

export type VaultCreateResponse = {
  id: number;
  uuid: string;
  slug: string;
  name: string;
  role: VaultRole;
  encryption_mode: EncryptionMode;
  recovery_custody_confirmed: boolean;
  recovery_export?: string;
  creation_mode?: VaultCreationMode;
};

export type VaultSelectRequest = {
  vault_id: number;
};

export type VaultSelectResponse = {
  vault_id: number;
};

export type RecoveryConfirmRequest = {
  acknowledged: boolean;
};

export type RecoveryConfirmResponse = {
  vault_id: number;
  recovery_custody_confirmed: boolean;
  recovery_custody_confirmed_at: string;
};

export type RecoveryExportRequest = {
  reason: string;
};

export type RecoveryExportResponse = {
  recovery_export: string;
};

/** Vault access / governance types (issue #68). */

export type VaultMember = {
  id: number;
  username: string;
  display_name: string;
  role: VaultRole;
  active?: boolean;
};

export type VaultMembersResponse = {
  items: VaultMember[];
};

export type UserLookupResult = {
  id: number;
  username: string;
  display_name: string;
  current_vault_role: VaultRole | null;
};

export type QuotaLimits = {
  storage_soft_limit_bytes?: number | null;
  storage_hard_limit_bytes?: number | null;
  concurrency_soft_limit?: number | null;
  concurrency_hard_limit?: number | null;
  restore_30d_soft_limit_bytes?: number | null;
  restore_30d_hard_limit_bytes?: number | null;
};

export type QuotaUsage = {
  storage_bytes?: number | null;
  concurrency?: number | null;
  restore_30d_bytes?: number | null;
  storage_unknown?: boolean;
  restore_request_unknown?: boolean;
};

export type QuotaDecision = {
  code?: string;
  severity?: "block" | "warning" | string;
  projected?: number;
  limit?: number;
};

export type QuotaEvaluation = {
  state?: string;
  allowed?: boolean;
  decisions?: QuotaDecision[];
};

export type VaultQuotasResponse = {
  vault_id?: number;
  limits: QuotaLimits | Partial<QuotaLimits>;
  usage: QuotaUsage;
  evaluation?: QuotaEvaluation;
};

export type VaultQuotaUpdatePayload = {
  storage_soft_limit_bytes: number | null;
  storage_hard_limit_bytes: number | null;
  concurrency_soft_limit: number | null;
  concurrency_hard_limit: number | null;
  restore_30d_soft_limit_bytes: number | null;
  restore_30d_hard_limit_bytes: number | null;
  reason: string;
};

export type OperatingWindow = {
  weekday: number;
  start: string;
  end: string;
};

export type OperationPolicy = {
  auto_upload: boolean;
  auto_local_cleanup: boolean;
  local_retention_days: number | null;
  stability_seconds: number;
  include_globs: string[];
  exclude_globs: string[];
  bandwidth_limit_kibps: number | null;
  operating_windows: OperatingWindow[];
};

export type GlobPreviewResponse = {
  included: string[];
  excluded: string[];
};

export type LifecycleTransition = {
  days: number;
  storage_class: string;
};

export type LifecycleGuidedProfile = {
  transitions?: LifecycleTransition[];
};

export type LifecyclePolicy = {
  id: number | string;
  name?: string;
  profile?: LifecycleGuidedProfile;
};

export type LifecycleFolderOverride = {
  folder_path: string;
  policy_id: number | string;
};

export type LifecycleResponse = {
  default_policy_id: number | string | null;
  folder_overrides: LifecycleFolderOverride[];
  policies: LifecyclePolicy[];
  guided_profiles: Record<string, LifecycleGuidedProfile>;
  warnings?: string[];
};

export type CloudDeletionSettings = {
  enabled: boolean;
  purge_delay_seconds?: number;
  delete_marker_explanation?: string;
  generated_phrase?: string;
  accepted_single_identity_risk?: string;
};

export type FilesystemFinding = {
  path: string;
  code: string;
  message: string;
  /** Present when the backend or a scan attaches remediation advice. */
  remediation?: string | null;
};

export type FilesystemCheck = {
  code: string;
  status: string;
  message: string;
  remediation?: string | null;
};

export type FilesystemHealth = {
  source_volume?: {
    alias: string | null;
    health: string;
    local_operations_allowed: boolean;
    cloud_catalog_allowed: boolean;
  };
  ok: boolean;
  uid: number | null;
  gid: number | null;
  root?: string;
  checks: FilesystemCheck[];
  findings: FilesystemFinding[];
};

export type StatsResponse = {
  states: Record<string, number>;
  storage: {
    local_bytes: number;
    cloud_bytes: number;
  };
  active_jobs: number;
  runtime: {
    last_error?: string | null;
    filesystem?: {
      findings?: FilesystemFinding[];
    };
    [key: string]: unknown;
  };
  filesystem: FilesystemHealth | null;
  delete_enabled: boolean;
};

/** Vault File row as returned by /api/files (browse or search). */
export type VaultFileListItem = {
  type: "file";
  name: string;
  path: string;
  local_exists?: number | boolean;
  local_size?: number | null;
  local_file_type?: string | null;
  cloud_exists?: number | boolean;
  cloud_size?: number | null;
  storage_class?: string | null;
  integrity?: string | null;
  availability?: string | null;
  restore_state?: string | null;
  restore_expiry?: string | null;
  state: string;
  upload_eligible?: boolean;
  recover_eligible?: boolean;
  recoverable_version_count?: number;
  cleanup_eligible?: boolean;
  lifecycle_pinned?: boolean;
  storage_class_eligible?: boolean;
};

/** Directory aggregate as returned by /api/files browse mode. */
export type DirectoryListItem = {
  type: "directory";
  name: string;
  path: string;
  item_count: number;
  total_size: number;
  local_size?: number;
  cloud_size?: number;
  state: string;
  state_counts?: Record<string, number>;
  storage_class?: string | null;
  storage_class_count?: number;
  available_actions?: Record<string, number>;
  lifecycle_pinned?: boolean;
  lifecycle_pinned_partial?: boolean;
};

export type ArchiveListItem = VaultFileListItem | DirectoryListItem;

export type FilesResponse = {
  items: ArchiveListItem[];
  total: number;
  page: number;
  directory: string;
  mode: "browse" | "search" | string;
};

export type PathHistoryEntry = {
  path: string;
  valid_from?: string | null;
  valid_to?: string | null;
};

export type ArchiveVersionSummary = {
  object_key?: string | null;
  version_number?: number;
  storage_class?: string | null;
  size?: number | null;
  [key: string]: unknown;
};

/** Path History + Archive Versions for one Vault File (/api/file-history). */
export type FileHistoryResponse = {
  vault_file_id: string;
  path: string;
  path_history: PathHistoryEntry[];
  versions: ArchiveVersionSummary[];
};

export type FilesQuery = {
  q?: string;
  state?: string;
  directory?: string;
  page?: number;
  page_size?: number;
};

/** Job / file-operation types (issue #67). */

export type FileOperationAction =
  | "upload"
  | "recover"
  | "free-space"
  | "storage-class"
  | "cloud-archive"
  | "cloud-purge"
  | "rename";

export type JobStatus =
  | "queued"
  | "uploading"
  | "verifying"
  | "retrying"
  | "downloading"
  | "restoring"
  | "cleaning"
  | "pending_approval"
  | "pending_delay"
  | "failed"
  | "cancelled"
  | "completed"
  | string;

export type JobRecord = {
  id: number;
  path: string;
  action: FileOperationAction | string;
  status: JobStatus;
  message?: string | null;
  message_key?: string | null;
  message_params?: unknown;
  group_id?: string | null;
  group_path?: string | null;
  total_bytes?: number | null;
  transferred_bytes?: number | null;
  pending_until?: string | null;
  estimated_cost_eur?: number | null;
  estimated_hours?: number | null;
  restore_tier?: string | null;
  restore_days?: number | null;
  approved_at?: string | null;
  requested_at?: string;
  updated_at?: string;
};

export type JobGroup = {
  id: string;
  path: string;
  action: FileOperationAction | string;
  status: JobStatus;
  message?: string | null;
  message_key?: string | null;
  total_bytes: number;
  transferred_bytes: number;
  item_count: number;
  completed_count: number;
  failed_count: number;
  cancelled_count: number;
  percent: number;
  updated_at?: string;
  pending_until?: string | null;
  estimated_cost_eur?: number | null;
  estimated_hours?: number | null;
  restore_tier?: string | null;
  restore_days?: number | null;
};

export type JobsResponse = {
  items: JobRecord[];
  groups: JobGroup[];
  locale?: string;
};

export type ArchiveVersionItem = {
  id: string;
  version_number: number;
  storage_class?: string | null;
  size?: number | null;
  recoverable: boolean;
  object_key?: string | null;
  created_at?: string | null;
  [key: string]: unknown;
};

export type FileVersionsResponse = {
  path: string;
  items: ArchiveVersionItem[];
  recoverable_count: number;
  default_archive_version_id: string | null;
  supported_restore_tiers: string[];
  default_restore_tier: string;
  default_restore_days: number;
};

export type RestoreEstimate = {
  tier: string;
  days: number;
  estimated_cost_eur: number;
  estimated_hours: number;
  pricing_note?: string;
  pricing_effective_at?: string | null;
  assumptions?: Record<string, unknown>;
  restore_object_irreversible?: boolean;
};

export type StorageClassOptionItem = {
  id: string;
  currency: string;
  storage_rate_eur_per_gib_month: number;
  retrieval: string;
  min_duration_days: number;
  requires_restore: boolean;
  availability_zones: string;
  restore_hours_bulk?: number;
  restore_hours_standard?: number;
  restore_rate_eur_per_gib_bulk?: number;
  restore_rate_eur_per_gib_standard?: number;
  retrieval_rate_eur_per_gib?: number;
};

export type StorageClassesResponse = {
  items: StorageClassOptionItem[];
  pricing_effective_at?: string;
  assumptions?: Record<string, unknown>;
  currency?: string;
};

export type RecoverEstimateResponse = {
  path: string;
  archive_version_id: string;
  storage_class?: string | null;
  requires_restore: boolean;
  restore_object_irreversible: boolean;
  high_impact: boolean;
  estimate: RestoreEstimate | null;
};

export type FileOperationPayload = {
  path: string;
  is_directory?: boolean;
  archive_version_id?: string;
  restore_tier?: string;
  restore_days?: number;
};

export type FileOperationResponse = {
  group_id: string;
  job_ids?: number[];
  item_count?: number;
  total_bytes?: number;
  message?: string;
  message_key?: string;
  archive_version_id?: string | null;
  restore_tier?: string | null;
  restore_days?: number | null;
  estimated_cost_eur?: number | null;
  estimated_hours?: number | null;
  quota?: unknown;
  preview?: CloudDeletionPreview;
  [key: string]: unknown;
};

export type CloudDeletionPreview = {
  object_count: number;
  version_count: number;
  delete_marker_count: number;
  byte_count: number;
  delete_marker_explanation?: string;
  [key: string]: unknown;
};

export type CloudPurgePayload = {
  path: string;
  is_directory?: boolean;
  confirmation: string;
  reason: string;
  generated_phrase: string;
};

export type JobCancelPayload = {
  group_id: string;
  action: string;
};

export type JobCancelResponse = {
  message: string;
  cancelled_count: number;
  message_key?: string;
};

/** Administration settings endpoints (issues #132–#136). */

export type SystemSettingValue = string | number | boolean | null;

export type SystemSettingItem = {
  key: string;
  environment_variable: string;
  source: string;
  mutability: "runtime_managed" | "deployment_only" | "restart_required" | string;
  restart_required: boolean;
  configured?: boolean;
  effective_value?: SystemSettingValue;
  minimum?: number;
  maximum?: number;
  minimum_length?: number;
  maximum_length?: number;
  choices?: string[];
};

export type SystemSettingsResponse = {
  revision: number;
  groups: Record<string, SystemSettingItem[]>;
};

export type SystemSettingsUpdatePayload = {
  revision: number;
  overrides: Record<string, SystemSettingValue>;
  removals: string[];
};

/** Managed OIDC configuration (issues #134 and #136). */

export type OidcConfigurationValues = {
  enabled?: boolean;
  issuer: string;
  client_id: string;
  client_secret_configured: boolean;
  scopes: string[];
  login_transaction_ttl_seconds: number;
  callback_url?: string;
  source?: string;
  version?: number;
  validation_status?: string;
};

export type OidcConfigurationResponse = {
  active: OidcConfigurationValues;
  draft: OidcConfigurationValues | null;
  configuration_status: string;
  last_validation: {
    status: string;
    validated_at?: string;
    error?: string;
  } | null;
};

export type OidcDraftPayload = {
  issuer: string;
  client_id: string;
  client_secret: string;
  scopes: string[];
  login_transaction_ttl_seconds: number;
};

/** Global administration endpoints (issue #69). */

export type AdminUser = {
  id: number;
  username: string;
  display_name: string;
  is_admin: boolean;
  active: boolean;
  vault_count: number;
  has_password: boolean;
  identity_count: number;
  created_at?: string;
};

export type AdminUsersResponse = {
  items: AdminUser[];
};

export type AdminIdentity = {
  id: number;
  issuer: string;
  subject: string;
  created_at: string;
};

export type AdminIdentitiesResponse = {
  items: AdminIdentity[];
};

export type AdminInvite = {
  id: number;
  target_user_id: number;
  target_username: string;
  created_by: number;
  created_at?: string;
  expires_at: string;
};

export type AdminInvitesResponse = {
  items: AdminInvite[];
};

export type AdminUserCreatePayload = {
  display_name: string;
  username: string;
  password: string | null;
  is_admin: boolean;
};

export type AdminUserUpdatePayload = {
  active?: boolean;
  display_name?: string;
  password?: string;
  is_admin?: boolean;
};

export type AdminVault = {
  id: number;
  slug: string;
  name: string;
  source_root: string;
  s3_bucket?: string;
  s3_prefix: string;
  enabled: boolean;
  member_count: number;
  encryption_mode?: string;
  uuid?: string;
  decommission_state?: "active" | "decommissioning" | "decommissioned";
  decommissioned_at?: string | null;
  root_released_at?: string | null;
};

export type AdminVaultsResponse = {
  items: AdminVault[];
};

export type AdminVaultCreatePayload = {
  name: string;
  slug: string;
  owner_user_id: number;
  reason: string;
  encryption_mode?: "plain" | "crypt";
  creation_mode?: "empty" | "adopt";
  volume_alias?: string;
  relative_path?: string;
};

export type AdminVaultRelocatePayload = {
  volume_alias: string;
  relative_path: string;
  reason: string;
};

export type AdminVaultRelocationResponse = {
  vault_id: number;
  source_root: string;
  relocation_state: "scan_required" | "ready";
  full_scan_required: boolean;
};

export type VaultDecommissionDisposition = "retain" | "remove" | "purge";

export type VaultDecommissionSelection = {
  local_disposition: "retain" | "remove";
  cloud_disposition: "retain" | "purge";
};

export type VaultDecommissionStartPayload = VaultDecommissionSelection & {
  confirmation: string;
  reason: string;
  preview_fingerprint: string;
};

export type VaultDecommissionCounts = {
  vault_files: number;
  local_files: number;
  local_bytes: number;
  archive_versions: number;
  cloud_bytes: number;
  delete_markers: number;
  jobs: number;
  memberships: number;
};

export type VaultDecommissionBlocker = {
  code: string;
  message: string;
  message_key?: string;
  count?: number;
};

export type VaultDecommissionPreview = VaultDecommissionSelection & {
  vault_id: number;
  vault_name: string;
  enabled: boolean;
  decommission_state: string;
  counts: VaultDecommissionCounts;
  blockers: VaultDecommissionBlocker[];
  can_start: boolean;
  fingerprint: string;
  root_identity_version?: string | null;
  root_identity_fingerprint?: string | null;
  recovery_material?: {
    encryption_mode: string;
    custody_confirmed: boolean;
    disposition: string;
  };
  records?: Record<string, string>;
};

export type VaultDecommissionJobProgress = {
  total: number;
  completed: number;
  failed: number;
  cancelled: number;
  active: number;
};

export type VaultDecommissionStatus = VaultDecommissionSelection & {
  id: number;
  vault_id: number;
  vault_name: string;
  state: string;
  decommission_state: string;
  enabled: boolean;
  local_status: string;
  cloud_status: string;
  requested_at: string;
  updated_at: string;
  completed_at?: string | null;
  decommissioned_at?: string | null;
  root_released_at?: string | null;
  root_released: boolean;
  error_code?: string | null;
  error_message?: string | null;
  preview: VaultDecommissionPreview;
  jobs: {
    local: VaultDecommissionJobProgress;
    cloud: VaultDecommissionJobProgress;
  };
  cloud_cancellable?: boolean;
  progress_percent: number;
};

export type AdminVaultMember = {
  id: number;
  username: string;
  display_name: string;
  active: boolean;
  role: string;
};

export type AdminVaultMembersResponse = {
  items: AdminVaultMember[];
};

export type AdminMembershipCreatePayload = {
  user_id: number;
  role: "operator" | "viewer";
  reason: string;
};

export type AdminOwnerTransferPayload = {
  new_owner_user_id: number;
  reason: string;
};



export type SourceVolumeInventoryItem = {
  alias: string;
  path: string;
  access: string;
  health: string;
  vault_count: number;
  source_area_count: number;
  diagnostic: string | null;
  diagnostic_code?: string | null;
};

export type SourceVolumeInventoryResponse = {
  items: SourceVolumeInventoryItem[];
};

export type SourceAreaGrant = {
  id: number;
  user_id: number;
  volume_alias: string;
  relative_path: string;
  created_at: string;
  availability: "available" | "unavailable";
  usable: boolean;
};

export type SourceAreaListResponse = {
  items: SourceAreaGrant[];
};

export type SourceAreaAssignPayload = {
  user_id: number;
  volume_alias: string;
  relative_path: string;
  reason: string;
};

export type SourceDirectoryOccupation = {
  kind: "vault";
  label?: string;
  vault_name?: string;
  owner_display_name?: string;
};

export type SourceDirectoryEntry = {
  name: string;
  relative_path: string;
  navigable: boolean;
  selectable: boolean;
  occupation: SourceDirectoryOccupation | null;
};

export type SourceDirectoryBrowseResponse = {
  volume_alias: string;
  relative_path: string;
  items: SourceDirectoryEntry[];
};
