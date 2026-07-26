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

export type EncryptionMode = "plain" | "crypt";

export type VaultCreateRequest = {
  name: string;
  slug?: string;
  encryption_mode: EncryptionMode;
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
