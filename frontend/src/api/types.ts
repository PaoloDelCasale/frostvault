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

/** Global administration endpoints (issue #69). */

export type AdminUser = {
  id: number;
  username: string;
  display_name: string;
  is_admin: boolean;
  active: boolean;
  vault_count: number;
  created_at?: string;
};

export type AdminUsersResponse = {
  items: AdminUser[];
};

export type AdminUserCreatePayload = {
  display_name: string;
  username: string;
  password: string;
  is_admin: boolean;
};

export type AdminUserUpdatePayload = {
  active?: boolean;
  display_name?: string;
  password?: string;
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

export type RecoveryExportResponse = {
  recovery_export: string;
};
